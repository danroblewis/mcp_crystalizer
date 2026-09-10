"""The effective server registry: built-ins < ~/.claude.json < ~/.mcp.json < <workspace>/.mcp.json, `${VAR}` /
`${VAR:-default}` expansion, `{{workspace}}` substitution, transport selection, module inference and script loading
for the in-process transport, and the round trip to a plain mcp.json for Claude Code."""
import json
import sys
from pathlib import Path

import pytest

from crystal import workspace as ws_mod
from crystal.mcp_client import ServerPool, load_registry, transport_for
from crystal.registry import (BUILTIN_SOURCE, effective_registry, expand_env, infer_module, inprocess_target, load_script,
                              normalize, to_mcp_json, transport_of)
from tests.conftest import SIM


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp home: Path.home() and $HOME point at it, and the test-suite's overrides for ~/.mcp.json and
    ~/.claude.json are lifted so they are really read from there."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.delenv("CRYSTAL_USER_MCP", raising=False)
    monkeypatch.delenv("CRYSTAL_CLAUDE_CONFIG", raising=False)
    return h


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(r))
    return r


def write(path: Path, servers: dict) -> Path:
    path.write_text(json.dumps({"mcpServers": servers}))
    return path


def test_expand_env():
    env = {"TOKEN": "t0k", "EMPTY": ""}
    assert expand_env("${TOKEN}", env) == "t0k"
    assert expand_env("${MISSING:-dflt}", env) == "dflt" and expand_env("${EMPTY:-dflt}", env) == "dflt"
    assert expand_env("${MISSING}", env) == ""
    assert expand_env(["a-${TOKEN}", {"k": "${TOKEN}${TOKEN}"}, 3], env) == ["a-t0k", {"k": "t0kt0k"}, 3]


def test_transport_selection():
    assert transport_of({"command": "npx"}) == "stdio"
    assert transport_of({"url": "https://x/mcp"}) == "http"
    assert transport_of({"type": "http", "url": "u"}) == "http"
    assert transport_of({"type": "streamable-http", "url": "u"}) == "http"
    assert transport_of({"type": "sse", "url": "u"}) == "sse"
    assert transport_of({"transport": "sse", "url": "u"}) == "sse"
    with pytest.raises(ValueError):
        transport_of({"type": "carrier-pigeon", "url": "u"})
    # the pool: in-process only for a stdio entry that is a python module or script, and only when asked
    assert transport_for({"transport": "stdio", "module": "crystal.servers.code"}, inprocess=True) == "inprocess"
    assert transport_for({"transport": "stdio", "module": "crystal.servers.code"}, inprocess=False) == "stdio"
    assert transport_for({"transport": "stdio", "command": sys.executable, "args": [str(SIM / "servers" / "jira.py")]}, inprocess=True) == "inprocess"
    assert transport_for({"transport": "stdio", "command": "npx"}, inprocess=True) == "stdio"
    assert transport_for({"transport": "http", "url": "u"}, inprocess=True) == "http"
    assert ServerPool({"d": {"type": "sse", "url": "u"}}, inprocess=True).transport("d") == "sse"


def test_normalize_materializes_a_stdio_entry(repo, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    ws = ws_mod.current()
    spec = normalize("github", {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github", "--root", "{{workspace}}"],
                                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN}", "UNSET": "${NOPE}", "KEEP": "${NOPE:-x}"}},
                     Path("/x/.mcp.json"), repo, ws)
    assert spec["transport"] == "stdio" and spec["command"] == "npx" and spec["cwd"] == str(repo)
    assert spec["args"] == ["-y", "@modelcontextprotocol/server-github", "--root", str(repo.resolve())]
    assert spec["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "secret", "KEEP": "x"}   # an unresolved ${VAR} stays unset
    assert "module" not in spec and spec["_source"] == "/x/.mcp.json"


def test_normalize_python_entries_and_this_interpreter(repo, monkeypatch):
    """`${MCP_EXPLORER_PYTHON:-python}` is this interpreter unless the user sets it; relative scripts under the
    workspace become absolute (without resolving symlinks); a script is an in-process target, a module too."""
    monkeypatch.delenv("MCP_EXPLORER_PYTHON", raising=False)
    (repo / "srv").mkdir()
    (repo / "srv" / "x.py").write_text("mcp = None\n")
    ws = ws_mod.current()
    spec = normalize("x", {"command": "${MCP_EXPLORER_PYTHON:-python}", "args": ["srv/x.py", "--flag"]}, repo / ".mcp.json", repo, ws)
    assert spec["command"] == sys.executable and spec["args"] == [str(repo / "srv" / "x.py"), "--flag"]
    assert inprocess_target(spec) == repo / "srv" / "x.py"
    monkeypatch.setenv("MCP_EXPLORER_PYTHON", "/opt/py")
    assert normalize("x", {"command": "${MCP_EXPLORER_PYTHON:-python}", "args": []}, repo / ".mcp.json", repo, ws)["command"] == "/opt/py"
    mod = normalize("m", {"command": "python3", "args": ["-m", "crystal.servers.code", "--root", "{{workspace}}"]}, repo / ".mcp.json", repo, ws)
    assert mod["module"] == "crystal.servers.code" and mod["args"][-1] == str(repo.resolve())
    from crystal import PACKAGE_DIR
    assert infer_module(sys.executable, [str(PACKAGE_DIR / "servers" / "git.py")]) == "crystal.servers.git"
    assert infer_module("npx", ["x.py"]) is None and infer_module("/usr/bin/python3", ["/elsewhere/x.py"]) is None


def test_load_script_runs_the_file_once_with_its_dir_on_sys_path(tmp_path):
    d = tmp_path / "srv"
    d.mkdir()
    (d / "sibling_mod.py").write_text("VALUE = 41\n")
    (d / "one.py").write_text("from sibling_mod import VALUE\nmcp = VALUE + 1\n")
    m = load_script(d / "one.py")
    assert m.mcp == 42 and load_script(d / "one.py") is m
    jira = load_script(SIM / "servers" / "jira.py")
    assert jira.mcp.name == "sim-jira"


def test_normalize_remote_entries(repo, monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    ws = ws_mod.current()
    spec = normalize("ctx", {"type": "http", "url": "https://mcp.context7.com/mcp", "headers": {"Authorization": "Bearer ${API_KEY}"}},
                     Path("/x/.mcp.json"), repo, ws)
    assert spec == {"transport": "http", "_source": "/x/.mcp.json", "type": "http", "url": "https://mcp.context7.com/mcp",
                    "headers": {"Authorization": "Bearer k"}}
    with pytest.raises(ValueError):
        normalize("bad", {"type": "sse"}, Path("/x"), repo, ws)


def test_builtins_point_at_the_workspace(repo):
    reg = effective_registry()
    assert set(reg) == {"code", "git", "flows", "claude-code"}
    for name in ("code", "git"):
        assert reg[name]["_source"] == BUILTIN_SOURCE and reg[name]["module"] == f"crystal.servers.{name}"
        assert reg[name]["command"] == sys.executable and reg[name]["args"][:2] == ["-m", f"crystal.servers.{name}"]
        assert reg[name]["args"][-2:] == ["--root", str(repo.resolve())] and reg[name]["cwd"] == str(repo.resolve())
    assert reg["flows"]["module"] == "crystal.servers.flows"


def test_merge_order_and_sources(home, repo):
    """built-ins < ~/.claude.json < ~/.mcp.json < <workspace>/.mcp.json, per server name; null / disabled drops."""
    (home / ".claude.json").write_text(json.dumps({"numStartups": 3, "mcpServers": {"jira": {"command": "claude-jira"}, "cc": {"command": "claude-cc"},
                                                                                    "extra": {"command": "claude-extra"}}, "projects": {}}))
    write(home / ".mcp.json", {"jira": {"command": "home-jira"}, "extra": {"command": "home-extra"},
                               "logz": {"command": "home-logz"}})
    write(repo / ".mcp.json", {"jira": {"command": "ws-jira"}, "deepwiki": {"type": "http", "url": "https://mcp.deepwiki.com/mcp"},
                               "logz": None, "cc": {"disabled": True}, "git": None})
    reg = effective_registry()
    assert reg["jira"]["command"] == "ws-jira" and reg["jira"]["_source"] == str(repo / ".mcp.json")
    assert reg["extra"]["command"] == "home-extra" and reg["extra"]["_source"] == str(home / ".mcp.json")
    assert "logz" not in reg and "cc" not in reg and "git" not in reg
    assert reg["deepwiki"]["transport"] == "http"
    assert reg["code"]["_source"] == BUILTIN_SOURCE and reg["flows"]["_source"] == BUILTIN_SOURCE   # untouched built-ins
    assert reg["code"]["args"][-2:] == ["--root", str(repo.resolve())] and reg["code"]["module"] == "crystal.servers.code"
    assert load_registry() == reg
    # a ~/.claude.json without mcpServers, or unreadable, is simply not a layer
    (home / ".claude.json").write_text("{\"oauthAccount\": {}}")
    assert "extra" in effective_registry() and effective_registry()["extra"]["command"] == "home-extra"
    (home / ".claude.json").write_text("not json")
    assert "jira" in effective_registry()


def test_the_sim_workspace_overrides_code_and_git(monkeypatch):
    """examples/sim/.mcp.json pins code and git to the sim repo and adds the simulated servers, all of them python
    scripts the in-process transport loads by path."""
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))
    reg = effective_registry()
    assert reg["code"]["args"] == [str(SIM / "servers" / "code.py")] and reg["code"]["_source"] == str(SIM / ".mcp.json")
    assert reg["code"]["command"] == sys.executable and "module" not in reg["code"]
    assert inprocess_target(reg["code"]) == SIM / "servers" / "code.py" and inprocess_target(reg["jira"]) == SIM / "servers" / "jira.py"
    assert reg["flows"]["_source"] == BUILTIN_SOURCE
    assert set(reg) == {"code", "git", "flows", "claude-code", "jira", "slack", "confluence", "logz", "chronosphere", "pagerduty"}


def test_override_with_same_command_keeps_explicit_module(home, repo):
    """A .mcp.json copy of a built-in entry (what `mcp-explorer mcp-config --write` produces) must not lose the
    in-process module."""
    reg0 = effective_registry()
    write(repo / ".mcp.json", {"flows": {"command": reg0["flows"]["command"], "args": reg0["flows"]["args"]}, "t": {"command": "/bin/echo", "args": ["b"]}})
    reg = effective_registry()
    assert reg["flows"]["module"] == "crystal.servers.flows" and reg["flows"]["_source"] == str(repo / ".mcp.json") and "module" not in reg["t"]


def test_to_mcp_json_round_trip(home, repo, monkeypatch):
    monkeypatch.setenv("TOK", "abc")
    write(repo / ".mcp.json", {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"T": "${TOK}"}},
                               "deepwiki": {"type": "http", "url": "https://mcp.deepwiki.com/mcp", "headers": {"X": "1"}}})
    reg = effective_registry()
    doc = to_mcp_json(reg, only=["github", "deepwiki", "code"])
    assert set(doc["mcpServers"]) == {"github", "deepwiki", "code"}
    assert doc["mcpServers"]["github"] == {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"T": "abc"}}
    assert doc["mcpServers"]["deepwiki"] == {"type": "http", "url": "https://mcp.deepwiki.com/mcp", "headers": {"X": "1"}}
    code = doc["mcpServers"]["code"]
    assert not any(k in code for k in ("module", "cwd", "transport", "_source")) and code["args"][-1] == str(repo.resolve())
    # relative_to: paths under the workspace become relative (a stored <workspace>/.mcp.json is portable)
    rel = to_mcp_json(reg, only=["code"], relative_to=repo.resolve())["mcpServers"]["code"]
    assert rel["args"][-1] == "." and rel["command"].startswith("/")
    # and reading the written file back yields the same effective registry (a fixed point; only the provenance
    # differs: every entry now comes from the workspace file and is run from the workspace, paths being absolute)
    (repo / ".mcp.json").write_text(json.dumps(to_mcp_json(reg, relative_to=repo.resolve())))
    again = effective_registry()
    strip = lambda r: {k: {kk: vv for kk, vv in v.items() if kk not in ("_source", "cwd")} for k, v in r.items()}  # noqa: E731
    assert strip(again) == strip(reg)
    assert all(v["_source"] == str(repo / ".mcp.json") for v in again.values())


def test_sim_mcp_json_round_trips(monkeypatch, tmp_path):
    """The sim's committed .mcp.json, written back by mcp-config --write, gives the same effective registry."""
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))
    ws = ws_mod.current()
    reg = effective_registry()
    doc = to_mcp_json(reg, relative_to=ws.root)
    assert doc["mcpServers"]["jira"]["args"] == ["servers/jira.py"] and doc["mcpServers"]["jira"]["command"] == sys.executable
    assert "flows" in doc["mcpServers"] and doc["mcpServers"]["flows"]["args"] == ["-m", "crystal.servers.flows"]


def test_cli_servers_and_mcp_config(repo, capsys):
    from crystal.cli import main
    write(repo / ".mcp.json", {"deepwiki": {"type": "http", "url": "https://mcp.deepwiki.com/mcp"}, "git": None})
    assert main(["servers", "--workspace", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("workspace repo:") and "deepwiki       http" in out and "git " not in out
    assert "built-in" in out and "state in" in out
    assert main(["--workspace", str(repo), "mcp-config"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mcpServers"]["deepwiki"]["url"] == "https://mcp.deepwiki.com/mcp" and "git" not in doc["mcpServers"]
    assert main(["mcp-config", "--write", "--workspace", str(repo)]) == 0
    written = json.loads((repo / ".mcp.json").read_text())
    assert written["mcpServers"]["code"]["args"][-1] == "." and "flows" in written["mcpServers"]
    assert main(["tools", "--workspace", str(repo / "missing")]) == 1


def test_claude_config_project_scoped_servers(tmp_path, monkeypatch):
    """`claude mcp add` for a project stores servers under projects[<root>].mcpServers in ~/.claude.json."""
    import json
    from crystal import registry
    root = tmp_path / "proj"
    root.mkdir()
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"shared": {"command": "echo", "args": ["user"]}},
        "projects": {str(root): {"mcpServers": {"kicad": {"command": "uv", "args": ["run", "kicad-mcp"]},
                                                "shared": {"command": "echo", "args": ["project"]}}},
                     str(tmp_path / "other"): {"mcpServers": {"nope": {"command": "false"}}}},
    }))
    servers = registry.read_claude_config(cfg, root)
    assert set(servers) == {"shared", "kicad"}
    assert servers["shared"]["args"] == ["project"] and servers["kicad"]["command"] == "uv"
    assert set(registry.read_claude_config(cfg)) == {"shared"}
