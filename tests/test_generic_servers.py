"""The generic code and git servers (crystal/servers/) over a throwaway git repo: the same tool names and result
shapes the sim servers have (grep / glob / read_file / codeowners; git_log / git_show / git_grep / git_blame),
`--root` from the args (stdio) or from `configure(args)` (in-process), and the sim wrappers unaffected."""
import asyncio
import subprocess
from pathlib import Path

import pytest

import sys

from crystal.mcp_client import ServerPool
from crystal.servers import root_from
from tests.conftest import SIM


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    r = tmp_path_factory.mktemp("ws")
    (r / "src").mkdir()
    (r / "src" / "arena.ts").write_text("export class Arena {\n  run() { return 'fight'; }\n}\n")
    (r / "src" / "util.py").write_text("def helper():\n    return 'arena'\n")
    (r / "CODEOWNERS").write_text("# owners\n*        @org/everyone\nsrc/     @org/core\n")
    (r / "node_modules").mkdir()
    (r / "node_modules" / "junk.ts").write_text("class Arena {}\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "HOME": str(r), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "add arena"]):
        subprocess.run(cmd, cwd=r, check=True, env=env, capture_output=True)
    (r / "src" / "arena.ts").write_text("export class Arena {\n  run() { return 'fight!'; }\n}\n")
    subprocess.run(["git", "commit", "-q", "-am", "louder"], cwd=r, check=True, env=env, capture_output=True)
    return r


def _registry(repo: Path) -> dict:
    py = sys.executable
    return {"code": {"command": py, "args": ["-m", "crystal.servers.code", "--root", str(repo)], "module": "crystal.servers.code", "cwd": str(repo)},
            "git": {"command": py, "args": ["-m", "crystal.servers.git", "--root", str(repo)], "module": "crystal.servers.git", "cwd": str(repo)}}


def _exercise(repo: Path, inprocess: bool) -> dict:
    async def go():
        async with ServerPool(_registry(repo), inprocess=inprocess) as pool:
            out = {"transport": pool.transport("code")}
            out["code_tools"] = sorted(t["name"] for t in await pool.list_tools("code"))
            out["git_tools"] = sorted(t["name"] for t in await pool.list_tools("git"))
            out["grep"] = await pool.call("code", "grep", {"pattern": "class Arena"})
            out["grep_py"] = await pool.call("code", "grep", {"pattern": "arena", "glob": "**/*.py", "context": 1})
            out["glob"] = await pool.call("code", "glob", {"pattern": "src/**/*"})
            out["read"] = await pool.call("code", "read_file", {"path": "src/arena.ts", "start": 2, "end": 2})
            out["escape"] = await pool.call("code", "read_file", {"path": "../../etc/passwd"})
            out["owners"] = await pool.call("code", "codeowners", {"path": "src/arena.ts"})
            out["owners_root"] = await pool.call("code", "codeowners", {"path": "README.md"})
            out["log"] = await pool.call("git", "git_log", {"max_count": 5})
            out["log_grep"] = await pool.call("git", "git_log", {"grep": "louder"})
            out["show"] = await pool.call("git", "git_show", {"sha": out["log"]["commits"][0]["sha"]})
            out["git_grep"] = await pool.call("git", "git_grep", {"pattern": "fight"})
            out["blame"] = await pool.call("git", "git_blame", {"path": "src/arena.ts", "lines": "2,2"})
            return out
    return asyncio.run(go())


def _check(out: dict, repo: Path):
    assert out["code_tools"] == ["codeowners", "glob", "grep", "read_file"]
    assert out["git_tools"] == ["git_blame", "git_grep", "git_log", "git_show"]
    assert out["grep"] == {"matches": [{"file": "src/arena.ts", "line": 1, "text": "export class Arena {"}], "truncated": False}  # node_modules skipped
    assert out["grep_py"]["matches"][0]["file"] == "src/util.py" and len(out["grep_py"]["matches"][0]["context"]) == 2
    assert out["glob"]["files"] == ["src/arena.ts", "src/util.py"]
    assert out["read"] == {"path": "src/arena.ts", "start": 2, "end": 2, "content": "  run() { return 'fight!'; }"}
    assert out["escape"] == {"error": "not found"}
    assert out["owners"] == {"path": "src/arena.ts", "owners": ["@org/core"]}
    assert out["owners_root"] == {"path": "README.md", "owners": ["@org/everyone"]}
    assert [c["subject"] for c in out["log"]["commits"]] == ["louder", "add arena"] and out["log"]["commits"][0]["author"] == "t"
    assert [c["subject"] for c in out["log_grep"]["commits"]] == ["louder"]
    assert "louder" in out["show"] and "+  run() { return 'fight!'; }" in out["show"]
    assert out["git_grep"] == {"matches": [{"file": "src/arena.ts", "line": 2, "text": "  run() { return 'fight!'; }"}]}
    assert "fight!" in out["blame"] and out["blame"].count("\n") == 1


def test_generic_servers_stdio(repo):
    out = _exercise(repo, inprocess=False)
    assert out["transport"] == "stdio"
    _check(out, repo)


def test_generic_servers_inprocess(repo):
    out = _exercise(repo, inprocess=True)
    assert out["transport"] == "inprocess"
    _check(out, repo)


def test_sim_wrappers_are_pinned_to_the_sim_repo(repo):
    """The sim's code/git servers are separate instances over examples/sim/repo: exercising the generic servers
    over another root (above, in this process) must not move them."""
    from crystal.registry import load_script
    from crystal.servers import code as generic_code
    sim_code = load_script(SIM / "servers" / "code.py")
    sim_git = load_script(SIM / "servers" / "git.py")
    assert sim_code.REPO == SIM / "repo" and sim_code.mcp is not generic_code.mcp
    assert sim_code.mcp.name == "sim-code" and sim_git.mcp.name == "sim-git"
    assert generic_code.ROOT() == repo   # configure(args) from the in-process run above

    async def go():
        async with ServerPool(inprocess=True) as pool:   # the sim registry (workspace = examples/sim)
            return await pool.call("code", "glob", {"pattern": "src/**/*"}), await pool.call("code", "grep", {"pattern": "def "})
    files, grep = asyncio.run(go())
    assert files["files"] == [] and grep["matches"] and all(m["file"].endswith(".py") for m in grep["matches"])   # sim default glob **/*.py


def test_root_from_args_env_and_cwd(tmp_path, monkeypatch):
    assert root_from(["--root", str(tmp_path)]) == tmp_path.resolve()
    assert root_from([f"--root={tmp_path}"]) == tmp_path.resolve()
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(tmp_path))
    assert root_from([]) == tmp_path.resolve()
    monkeypatch.delenv("CRYSTAL_WORKSPACE")
    monkeypatch.chdir(tmp_path)
    assert root_from([]) == tmp_path.resolve()


def test_missing_root_reports_git_errors_cleanly(tmp_path):
    from crystal.servers.git import make_server
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession
    from crystal.mcp_client import parse_result
    empty = tmp_path / "not-a-repo"
    empty.mkdir()

    async def go():
        async with InMemoryTransport(make_server(empty)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return parse_result(await s.call_tool("git_log", {}))
    out = asyncio.run(go())
    assert out["commits"] == [] and out["error"].startswith("error:")
