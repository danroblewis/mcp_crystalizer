"""Workspaces: the current directory by default, `--workspace` / $CRYSTAL_WORKSPACE overrides, slugs, and the state
dir under $MCP_EXPLORER_HOME that keeps every workspace's runs and traces apart."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from crystal import state as state_mod
from crystal import workspace as ws_mod
from crystal.flow.runner import FlowRunner, current_run_dir
from crystal.trace.record import Recorder, current_trace_dir
from crystal.trace.store import load_sessions


def test_default_workspace_is_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    ws = ws_mod.current()
    assert ws.root == tmp_path.resolve() and ws.name == tmp_path.name
    assert ws.slug == state_mod.slug_of(tmp_path.resolve())
    assert ws.mcp_json() == tmp_path.resolve() / ".mcp.json"
    assert ws.state.dir == state_mod.home() / "workspaces" / ws.slug


def test_env_and_relative_paths_resolve_against_the_cwd(tmp_path, monkeypatch):
    d = tmp_path / "My Repo.v2"
    d.mkdir()
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(d))
    ws = ws_mod.current()
    assert ws.root == d.resolve() and ws.slug.startswith("my-repo-v2-") and len(ws.slug) == len("my-repo-v2-") + 8
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRYSTAL_WORKSPACE", "My Repo.v2")
    assert ws_mod.current().root == d.resolve()
    # explicit beats env
    assert ws_mod.workspace(tmp_path).root == tmp_path.resolve()


def test_slug_is_dirname_plus_hash_of_the_absolute_path(tmp_path):
    a, b = tmp_path / "x" / "api", tmp_path / "y" / "api"
    a.mkdir(parents=True), b.mkdir(parents=True)
    sa, sb = state_mod.slug_of(a), state_mod.slug_of(b)
    assert sa.startswith("api-") and sb.startswith("api-") and sa != sb
    assert sa == state_mod.slug_of(a)                      # stable
    assert state_mod.slug_of(Path("/tmp/Weird Name!")) .startswith("weird-name-")


def test_activate_sets_env_for_children_and_rejects_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    ws = ws_mod.activate(tmp_path)
    assert os.environ["CRYSTAL_WORKSPACE"] == str(tmp_path.resolve()) and ws_mod.current() == ws
    with pytest.raises(FileNotFoundError):
        ws_mod.activate(tmp_path / "nope")


def test_split_argv_takes_the_flag_anywhere():
    assert ws_mod.split_argv(["--workspace", "w", "tools", "code"]) == ("w", ["tools", "code"])
    assert ws_mod.split_argv(["tools", "--workspace=w"]) == ("w", ["tools"])
    assert ws_mod.split_argv(["run", "f", "k=v"]) == (None, ["run", "f", "k=v"])


class FakePool:
    async def call_raw(self, server, tool, args):
        return {"key": "PAY-1", "fields": {}}, "{}", False


FLOW = {"name": "wsflow", "status": "draft", "inputs": {"key": {"type": "string", "required": True}},
        "steps": [{"id": "issue", "tool": "jira.jira_get_issue", "args": {"issue_key": "{{ inputs.key }}"}}]}


def test_runs_and_traces_live_in_the_workspace_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    a, b = tmp_path / "Some-Repo", tmp_path / "Other"
    a.mkdir(), b.mkdir()
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(a))
    sa = state_mod.state_for(a.resolve())
    assert current_run_dir() == sa.runs and current_trace_dir() == sa.traces
    assert sa.dir == tmp_path / "home" / "workspaces" / sa.slug and sa.slug.startswith("some-repo-")
    r = asyncio.run(FlowRunner(FakePool(), catalog={}, lifecycle=False).run(FLOW, {"key": "PAY-1"}, save=True))
    assert (sa.runs / f"{r['run_id']}.json").exists()
    Recorder("s1", "scripted", meta={"trigger": "t"}).record("jira", "jira_get_issue", {}, {})
    assert (sa.traces / "s1.jsonl").exists()
    # another workspace: its own state dir, and load_sessions reads only that one
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(b))
    sb = state_mod.state_for(b.resolve())
    assert current_run_dir() == sb.runs and sb.dir != sa.dir
    r2 = asyncio.run(FlowRunner(FakePool(), catalog={}, lifecycle=False).run(FLOW, {"key": "PAY-1"}, save=True))
    assert (sb.runs / f"{r2['run_id']}.json").exists() and not (sa.runs / f"{r2['run_id']}.json").exists()
    Recorder("s2", "scripted", meta={"trigger": "t"}).record("jira", "jira_get_issue", {}, {})
    assert [s.session_id for s in load_sessions(trigger="t")] == ["s2"]
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(a))
    assert [s.session_id for s in load_sessions(trigger="t")] == ["s1"]
    # nothing was written into either workspace directory
    assert sorted(p.name for p in a.iterdir()) == [] and sorted(p.name for p in b.iterdir()) == []


def test_ui_reads_the_workspace_state_dir(tmp_path, monkeypatch):
    import crystal.app.main as web
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(tmp_path))
    if hasattr(web.app.state, "workspace"):
        monkeypatch.delattr(web.app.state, "workspace")
    st = ws_mod.current().state
    assert web._run_dir() == st.runs and web._trace_dir() == st.traces and web._feedback() == st.feedback
    view = web._workspace_view()
    assert view["name"] == tmp_path.name and view["root"] == str(tmp_path.resolve()) and view["state_dir"] == str(st)


def test_driver_builds_a_workspace_command(tmp_path, monkeypatch):
    """`mcp-explorer record` hands Claude Code the effective config for the workspace (a temp mcp.json, strict) and a
    temp settings file with the recording hooks, runs it in the workspace, and the trace lands in the workspace's
    state dir."""
    from crystal.trace import driver
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"echo": {"command": "echo", "args": ["hi"]},
                                                              "git": {"disabled": True}}}))
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(repo))
    seen = {}

    class Proc:
        returncode, stdout, stderr = 0, json.dumps({"result": "done", "total_cost_usd": 0.01, "num_turns": 1}), ""

    def fake_run(cmd, cwd=None, env=None, capture_output=True, text=True):
        seen.update(cmd=cmd, cwd=cwd, env=env, config=json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text()),
                    settings=json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text()))
        return Proc()
    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    info = driver.run_agent("codebase", {"question": "q"}, "prompt", budget="1", quiet=True)
    ws = ws_mod.current()
    assert Path(seen["cwd"]) == repo.resolve() and seen["env"]["CRYSTAL_WORKSPACE"] == str(repo.resolve())
    assert seen["env"]["MCP_EXPLORER_HOME"] == str(tmp_path / "home") and not any(k.startswith("CLAUDE") for k in seen["env"])
    assert "--strict-mcp-config" in seen["cmd"]
    assert set(seen["settings"]["hooks"]) == {"PostToolUse", "UserPromptSubmit", "Stop"}
    assert "echo" in seen["config"]["mcpServers"] and "git" not in seen["config"]["mcpServers"] and "code" in seen["config"]["mcpServers"]
    assert "mcp__echo__*" in seen["cmd"] and "mcp__git__*" not in seen["cmd"]
    assert info["workspace"] == ws.slug and Path(info["trace_path"]).parent == ws.state.traces
    assert Path(info["trace_path"]).exists() and not Path(seen["cmd"][seen["cmd"].index("--mcp-config") + 1]).exists()
    assert ws.state.workspace_json.exists()
    from crystal.trace.store import load_session
    assert load_session(Path(info["trace_path"])).result == "done"   # the final message is kept even without the Stop hook
