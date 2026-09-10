"""Our own versions of Claude Code's tools: a flow induced from a recorded session must be able to replay
`claude-code.Read/Grep/Glob/Bash`, and Bash must refuse anything that is not a read."""
import json

import pytest

from crystal.servers.claude_code import ShellRefused, check_command, make_server


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\n\n\ndef handler():\n    raise TimeoutError('gateway')\n")
    (tmp_path / "README.md").write_text("# demo\nA line about TimeoutError.\n")
    return tmp_path


def call(mcp, name, **args):
    fn = mcp._tool_manager.get_tool(name).fn if hasattr(mcp, "_tool_manager") else None
    if fn is None:                                   # SDK layout differs; fall back to the registered callables
        fn = {t.name: t.fn for t in mcp._tools.values()}[name]  # pragma: no cover
    return json.loads(fn(**args))


def test_read_grep_glob_answer_the_transcript_argument_shapes(repo):
    mcp = make_server(repo)
    r = call(mcp, "Read", file_path="src/app.py", offset=4, limit=2)
    assert r["offset"] == 4 and "def handler" in r["content"] and r["content"].startswith("     4\t")
    assert call(mcp, "Glob", pattern="src/**/*.py")["files"][0].endswith("app.py")
    assert any("app.py" in f for f in call(mcp, "Grep", pattern="TimeoutError")["files"])
    assert call(mcp, "Grep", pattern="timeouterror", ignore_case=True)["files"]
    content = call(mcp, "Grep", pattern="TimeoutError", output_mode="content", glob="**/*.py")
    assert content["matches"][0]["line"] == 5
    assert call(mcp, "Grep", pattern="TimeoutError", output_mode="count")["count"] == 2


def test_read_refuses_outside_the_workspace(repo):
    assert "outside the workspace" in call(make_server(repo), "Read", file_path="/etc/hosts")["error"]


def test_bash_runs_reads_and_refuses_everything_else(repo):
    mcp = make_server(repo)
    ok = call(mcp, "Bash", command="ls src")
    assert ok["exit_code"] == 0 and "app.py" in ok["stdout"]
    for bad, why in [("rm -rf src", "read-only program"), ("cat README.md > /tmp/x", "redirects or chains"),
                     ("ls | wc -l", "redirects or chains"), ("git push", "read-only subcommands"),
                     ("python3 build.py", "run a script")]:
        out = call(mcp, "Bash", command=bad)
        assert "error" in out and why in out["error"], (bad, out)
        assert "CRYSTAL_ALLOW_SHELL" in out["error"] or "subcommand" in out["error"] or "script" in out["error"]


def test_check_command_allows_read_only_git_and_interpreter_flags():
    assert check_command("git log -n 5")[0] == "git"
    assert check_command("python3 -c 'print(1)'")[0] == "python3"
    with pytest.raises(ShellRefused):
        check_command("sudo reboot")


def test_allow_shell_env_lifts_the_check(monkeypatch):
    monkeypatch.setenv("CRYSTAL_ALLOW_SHELL", "1")
    assert check_command("rm -rf build") == ["rm", "-rf", "build"]


def test_registered_as_a_builtin_server(tmp_path):
    from crystal import registry
    from crystal.workspace import Workspace
    servers = registry.builtin_servers(Workspace(root=tmp_path, slug="x"))
    assert "claude-code" in servers and servers["claude-code"]["module"] == "crystal.servers.claude_code"


def test_dashed_grep_flags_are_normalised_at_import():
    """A transcript's `-i`/`-C` are not valid parameter names; import renames them to what our server declares."""
    from crystal.trace.builtin_map import normalise_args

    got = normalise_args("Grep", {"pattern": "x", "-i": True, "-C": 3, "-n": True, "output_mode": "content"})
    assert got == {"pattern": "x", "ignore_case": True, "context": 3, "output_mode": "content"}
