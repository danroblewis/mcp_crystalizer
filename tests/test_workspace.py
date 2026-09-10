"""Workspaces: resolution ($CRYSTAL_WORKSPACE, --workspace, the project as the sim), slugs, and the runs/<slug>/ and
traces/<slug>/ namespaces that keep sim runs and real-repo runs apart while the sim keeps its historical top-level
directories."""
import asyncio
import json
from pathlib import Path

import pytest

from crystal import PROJECT_ROOT
from crystal import workspace as ws_mod
from crystal.flow import runner as runner_mod
from crystal.flow.runner import FlowRunner, current_run_dir
from crystal.trace import record as record_mod
from crystal.trace.record import Recorder, current_trace_dir
from crystal.trace.store import load_sessions


def test_default_workspace_is_the_project_and_slug_sim(monkeypatch):
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    ws = ws_mod.current()
    assert ws.root == PROJECT_ROOT and ws.slug == "sim" and ws.is_project
    assert ws.namespaced(PROJECT_ROOT / "runs") == PROJECT_ROOT / "runs"      # the sim keeps the top-level dirs
    assert ws.mcp_json() == PROJECT_ROOT / ".mcp.json"


def test_env_and_relative_paths_resolve_against_the_project(tmp_path, monkeypatch):
    d = tmp_path / "My Repo.v2"
    d.mkdir()
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(d))
    ws = ws_mod.current()
    assert ws.root == d.resolve() and ws.slug == "my-repo-v2" and not ws.is_project
    assert ws.namespaced(PROJECT_ROOT / "runs") == PROJECT_ROOT / "runs" / "my-repo-v2"
    # relative: from the project root, not the cwd
    monkeypatch.setenv("CRYSTAL_WORKSPACE", "workspaces/x")
    assert ws_mod.current().root == (PROJECT_ROOT / "workspaces" / "x").resolve()
    # explicit beats env
    assert ws_mod.workspace(d).root == d.resolve()


def test_a_directory_called_sim_never_shares_the_sim_namespace(tmp_path):
    d = tmp_path / "sim"
    d.mkdir()
    slug = ws_mod.workspace(d).slug
    assert slug.startswith("sim-") and slug != "sim"


def test_activate_sets_env_for_children_and_rejects_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    ws = ws_mod.activate(tmp_path)
    import os
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


def test_runs_and_traces_are_namespaced_by_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "RUN_DIR", tmp_path / "runs")
    monkeypatch.setattr(record_mod, "TRACE_DIR", tmp_path / "traces")
    repo = tmp_path / "Some-Repo"
    repo.mkdir()
    # sim: top level
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    assert current_run_dir() == tmp_path / "runs" and current_trace_dir() == tmp_path / "traces"
    r = asyncio.run(FlowRunner(FakePool(), catalog={}, lifecycle=False).run(FLOW, {"key": "PAY-1"}, save=True))
    assert (tmp_path / "runs" / f"{r['run_id']}.json").exists()
    Recorder("s1", "scripted", meta={"trigger": "t"}).record("jira", "jira_get_issue", {}, {})
    assert (tmp_path / "traces" / "s1.jsonl").exists()
    # another workspace: runs/<slug>/ and traces/<slug>/, and load_sessions reads only that namespace
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(repo))
    assert current_run_dir() == tmp_path / "runs" / "some-repo" and current_trace_dir() == tmp_path / "traces" / "some-repo"
    r2 = asyncio.run(FlowRunner(FakePool(), catalog={}, lifecycle=False).run(FLOW, {"key": "PAY-1"}, save=True))
    assert (tmp_path / "runs" / "some-repo" / f"{r2['run_id']}.json").exists()
    Recorder("s2", "scripted", meta={"trigger": "t"}).record("jira", "jira_get_issue", {}, {})
    assert (tmp_path / "traces" / "some-repo" / "s2.jsonl").exists()
    assert [s.session_id for s in load_sessions(trigger="t")] == ["s2"]
    monkeypatch.delenv("CRYSTAL_WORKSPACE")
    assert [s.session_id for s in load_sessions(trigger="t")] == ["s1"]


def test_ui_reads_the_workspace_namespace(tmp_path, monkeypatch):
    import app.main as web
    monkeypatch.setattr(web, "RUN_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(tmp_path))
    if hasattr(web.app.state, "run_dir"):
        monkeypatch.delattr(web.app.state, "run_dir")
    slug = ws_mod.current().slug
    assert web._run_dir() == tmp_path / "runs" / slug and web._trace_dir() == tmp_path / "traces" / slug


def test_driver_builds_a_workspace_command(tmp_path, monkeypatch):
    """The driver hands Claude Code the effective config for the workspace (a temp mcp.json, strict), runs it in the
    workspace, and carries the project's hook settings there; the trace goes under traces/<slug>/."""
    from crystal.trace import driver
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"echo": {"command": "echo", "args": ["hi"]},
                                                              "jira": {"disabled": True}}}))
    monkeypatch.setattr(record_mod, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(driver, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(repo))
    seen = {}

    class Proc:
        returncode, stdout, stderr = 0, json.dumps({"result": "done", "total_cost_usd": 0.01, "num_turns": 1}), ""

    def fake_run(cmd, cwd=None, env=None, capture_output=True, text=True):
        seen.update(cmd=cmd, cwd=cwd, env=env, config=json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text()))
        return Proc()
    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    info = driver.run_agent("codebase", {"question": "q"}, "prompt", budget="1", quiet=True)
    slug = ws_mod.current().slug
    assert Path(seen["cwd"]) == repo.resolve() and seen["env"]["CRYSTAL_WORKSPACE"] == str(repo.resolve())
    assert seen["env"]["CRYSTAL_PROJECT_DIR"] == str(PROJECT_ROOT) and not any(k.startswith("CLAUDE") for k in seen["env"])
    assert "--strict-mcp-config" in seen["cmd"] and str(driver.SETTINGS) in seen["cmd"]
    assert "echo" in seen["config"]["mcpServers"] and "jira" not in seen["config"]["mcpServers"]
    assert "mcp__echo__*" in seen["cmd"] and "mcp__jira__*" not in seen["cmd"]
    assert info["workspace"] == slug and Path(info["trace_path"]).parent == tmp_path / "traces" / slug
    assert Path(info["trace_path"]).exists() and not Path(seen["cmd"][seen["cmd"].index("--mcp-config") + 1]).exists()
