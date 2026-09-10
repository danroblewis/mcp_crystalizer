"""The effective server registry: servers.yaml < ~/.mcp.json < <workspace>/.mcp.json, `${VAR}` / `${VAR:-default}`
expansion, `{{workspace}}` / `{{project}}` substitution, transport selection, module inference for the in-process
transport, and the round trip to a plain mcp.json for Claude Code."""
import json
from pathlib import Path

import pytest

from crystal import PROJECT_ROOT
from crystal import workspace as ws_mod
from crystal.mcp_client import ServerPool, load_registry, transport_for
from crystal.registry import effective_registry, expand_env, infer_module, normalize, to_mcp_json, transport_of


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp home: Path.home() and $HOME point at it, and the test-suite's $CRYSTAL_USER_MCP override is lifted so
    ~/.mcp.json is really read from there."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.delenv("CRYSTAL_USER_MCP", raising=False)
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
    # the pool: in-process only for a stdio entry with a module, and only when asked
    assert transport_for({"transport": "stdio", "module": "sim.servers.jira"}, inprocess=True) == "inprocess"
    assert transport_for({"transport": "stdio", "module": "sim.servers.jira"}, inprocess=False) == "stdio"
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


def test_normalize_absolutizes_project_paths_without_resolving_symlinks(repo):
    ws = ws_mod.current()
    spec = normalize("code", {"command": ".venv/bin/python", "args": ["crystal/servers/code.py", "--root", "{{workspace}}"]},
                     PROJECT_ROOT / "servers.yaml", PROJECT_ROOT, ws)
    assert spec["command"] == str(PROJECT_ROOT / ".venv" / "bin" / "python")    # the venv's python, not its target
    assert spec["args"][0] == str(PROJECT_ROOT / "crystal" / "servers" / "code.py")
    assert spec["module"] == "crystal.servers.code"
    assert infer_module("npx", ["x.py"]) is None and infer_module("/usr/bin/python3", ["/elsewhere/x.py"]) is None


def test_normalize_remote_entries(repo, monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    ws = ws_mod.current()
    spec = normalize("ctx", {"type": "http", "url": "https://mcp.context7.com/mcp", "headers": {"Authorization": "Bearer ${API_KEY}"}},
                     Path("/x/.mcp.json"), repo, ws)
    assert spec == {"transport": "http", "_source": "/x/.mcp.json", "type": "http", "url": "https://mcp.context7.com/mcp",
                    "headers": {"Authorization": "Bearer k"}}
    with pytest.raises(ValueError):
        normalize("bad", {"type": "sse"}, Path("/x"), repo, ws)


def test_merge_order_and_sources(home, repo):
    """servers.yaml < ~/.mcp.json < <workspace>/.mcp.json, per server name; null / disabled drops an entry."""
    write(home / ".mcp.json", {"jira": {"command": "home-jira"}, "extra": {"command": "home-extra"},
                               "logz": {"command": "home-logz"}})
    write(repo / ".mcp.json", {"jira": {"command": "ws-jira"}, "deepwiki": {"type": "http", "url": "https://mcp.deepwiki.com/mcp"},
                               "logz": None, "slack": {"disabled": True}})
    reg = effective_registry()
    assert reg["jira"]["command"] == "ws-jira" and reg["jira"]["_source"] == str(repo / ".mcp.json")
    assert reg["extra"]["command"] == "home-extra" and reg["extra"]["_source"] == str(home / ".mcp.json")
    assert "logz" not in reg and "slack" not in reg
    assert reg["deepwiki"]["transport"] == "http"
    assert reg["confluence"]["_source"] == str(PROJECT_ROOT / "servers.yaml")    # untouched default
    # the generic code/git servers point at the workspace
    assert reg["code"]["args"][-2:] == ["--root", str(repo.resolve())] and reg["code"]["module"] == "crystal.servers.code"
    assert reg["git"]["args"][-2:] == ["--root", str(repo.resolve())]
    assert load_registry() == reg


def test_the_sim_is_the_project_workspace(monkeypatch):
    """With no workspace, the project's own .mcp.json pins code and git to the sim repo and keeps the in-process
    module (same command and args as the servers.yaml entry would be irrelevant here: the module is inferred)."""
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    reg = effective_registry()
    assert reg["code"]["args"] == [str(PROJECT_ROOT / "sim" / "servers" / "code.py")] and reg["code"]["module"] == "sim.servers.code"
    assert reg["git"]["module"] == "sim.servers.git" and reg["jira"]["module"] == "sim.servers.jira"
    assert reg["code"]["_source"] == str(PROJECT_ROOT / ".mcp.json")


def test_override_with_same_command_keeps_explicit_module(home, repo):
    """A .mcp.json copy of a servers.yaml entry (what `crystal mcp-config --write` produces) must not lose the
    in-process module declared in servers.yaml."""
    y = repo / "servers.yaml"
    y.write_text("servers:\n  s: { command: /bin/echo, args: [a], module: some.module, attr: srv }\n")
    write(repo / ".mcp.json", {"s": {"command": "/bin/echo", "args": ["a"]}, "t": {"command": "/bin/echo", "args": ["b"]}})
    reg = effective_registry(servers_yaml=y)
    assert reg["s"]["module"] == "some.module" and reg["s"]["attr"] == "srv" and "module" not in reg["t"]


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
    assert rel["args"][-1] == "." and rel["args"][0].startswith("/")
    # and reading the written file back yields the same effective registry (a fixed point; only the provenance
    # differs: every entry now comes from the workspace file and is run from the workspace, paths being absolute)
    (repo / ".mcp.json").write_text(json.dumps(to_mcp_json(reg, relative_to=repo.resolve())))
    again = effective_registry()
    strip = lambda r: {k: {kk: vv for kk, vv in v.items() if kk not in ("_source", "cwd")} for k, v in r.items()}  # noqa: E731
    assert strip(again) == strip(reg)
    assert all(v["_source"] == str(repo / ".mcp.json") for v in again.values())


def test_project_mcp_json_is_the_effective_sim_config(monkeypatch):
    """The committed .mcp.json is exactly what `crystal mcp-config --write` writes for the project workspace."""
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    ws = ws_mod.current()
    assert json.loads((PROJECT_ROOT / ".mcp.json").read_text()) == to_mcp_json(effective_registry(), relative_to=ws.root)


def test_cli_servers_and_mcp_config(repo, capsys):
    from crystal.cli import main
    write(repo / ".mcp.json", {"deepwiki": {"type": "http", "url": "https://mcp.deepwiki.com/mcp"}, "jira": None})
    assert main(["servers", "--workspace", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("workspace repo:") and "deepwiki       http" in out and "jira" not in out
    assert "runs/repo/" in out and "traces/repo/" in out
    assert main(["--workspace", str(repo), "mcp-config"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mcpServers"]["deepwiki"]["url"] == "https://mcp.deepwiki.com/mcp" and "jira" not in doc["mcpServers"]
    assert main(["mcp-config", "--write", "--workspace", str(repo)]) == 0
    written = json.loads((repo / ".mcp.json").read_text())
    assert written["mcpServers"]["code"]["args"][-1] == "." and "flows" in written["mcpServers"]
    assert main(["tools", "--workspace", str(repo / "missing")]) == 1
