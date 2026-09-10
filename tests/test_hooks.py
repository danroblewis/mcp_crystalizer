"""The Claude Code hooks: install-hook merges into a settings file (idempotent, --uninstall removes exactly ours), the
hook maps its cwd to a workspace state dir, records prompts (UserPromptSubmit), MCP calls (PostToolUse) and the final
message (Stop), and never records a session that touches no MCP server."""
import json
from pathlib import Path

from crystal import hooks
from crystal import state as state_mod
from crystal.trace.record import hook_main
from crystal.trace.store import load_session


def test_install_merges_idempotently_and_uninstall_removes_only_ours(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "sonnet", "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo theirs"}]}]}}))
    hooks.install(settings, command="mcp-explorer hook")
    doc = json.loads(settings.read_text())
    assert doc["model"] == "sonnet"                                           # everything else kept
    assert doc["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "echo theirs"
    ours = [h for g in doc["hooks"]["PostToolUse"] for h in g["hooks"] if hooks.is_ours(h)]
    assert len(ours) == 1 and doc["hooks"]["PostToolUse"][1]["matcher"] == hooks.MATCHER
    assert hooks.installed(settings) == {"PostToolUse": True, "UserPromptSubmit": True, "Stop": True}
    hooks.install(settings, command="mcp-explorer hook")                      # twice: no duplicates
    doc2 = json.loads(settings.read_text())
    assert doc2 == doc
    hooks.uninstall(settings)
    doc3 = json.loads(settings.read_text())
    assert doc3["hooks"] == {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo theirs"}]}]}
    assert hooks.installed(settings) == {"PostToolUse": False, "UserPromptSubmit": False, "Stop": False}
    hooks.uninstall(settings)                                                 # again: harmless
    assert json.loads(settings.read_text()) == doc3


def test_install_creates_the_file_and_uses_this_python(tmp_path):
    settings = tmp_path / "claude" / "settings.json"
    hooks.install(settings)
    doc = json.loads(settings.read_text())
    cmd = doc["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert cmd.endswith("-m crystal.cli hook") and hooks.is_ours({"command": cmd})
    hooks.uninstall(settings)
    assert "hooks" not in json.loads(settings.read_text())


def test_cli_install_hook(tmp_path, capsys):
    from crystal.cli import main
    settings = tmp_path / "s.json"
    assert main(["install-hook", "--settings", str(settings)]) == 0 and "installed" in capsys.readouterr().out
    assert main(["install-hook", "--settings", str(settings), "--status"]) == 0
    assert main(["install-hook", "--settings", str(settings), "--uninstall"]) == 0
    assert main(["install-hook", "--settings", str(settings), "--status"]) == 1


def _payload(event, cwd, sid="sess-1", **kw):
    return {"hook_event_name": event, "session_id": sid, "cwd": str(cwd), "transcript_path": str(cwd / "t.jsonl"), **kw}


def test_hook_maps_cwd_to_the_workspace_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRYSTAL_WORKSPACE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    call = _payload("PostToolUse", repo, tool_name="mcp__jira__jira_get_issue", tool_input={"issue_key": "PAY-1"},
                    tool_response={"content": [{"type": "text", "text": json.dumps({"key": "PAY-1"})}]})
    assert hook_main(call) == 0
    st = state_mod.state_for(repo.resolve())
    assert (st.traces / "sess-1.jsonl").exists() and st.workspace_json.exists()
    assert sorted(p.name for p in repo.iterdir()) == []                        # nothing written into the repo
    s = load_session(st.traces / "sess-1.jsonl")
    assert s.tool_sequence == ["jira.jira_get_issue"] and s.calls[0]["output"] == {"key": "PAY-1"} and s.meta["cwd"] == str(repo)
    # a different cwd is a different workspace
    other = tmp_path / "other"
    other.mkdir()
    hook_main(_payload("PostToolUse", other, sid="sess-2", tool_name="mcp__jira__jira_get_issue", tool_input={}, tool_response={}))
    assert (state_mod.state_for(other.resolve()).traces / "sess-2.jsonl").exists() and not (st.traces / "sess-2.jsonl").exists()


def test_prompt_and_final_message_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    st = state_mod.state_for(repo.resolve())
    hook_main(_payload("UserPromptSubmit", repo, prompt="why is PAY-101 failing?"))
    hook_main(_payload("PostToolUse", repo, tool_name="Read", tool_input={"file_path": "a.py"}, tool_response={"type": "text", "file": {"content": "x" * 1000}}))
    s = load_session(st.traces / "sess-1.jsonl")
    assert s.prompts[0]["text"] == "why is PAY-101 failing?" and s.prompt == "why is PAY-101 failing?"
    assert s.calls == []                                                       # a Read before any MCP call is not a step
    hook_main(_payload("PostToolUse", repo, tool_name="mcp__slack__conversations_search_messages", tool_input={"search_query": "PAY-101"},
                       tool_response={"content": [{"type": "text", "text": "{\"messages\": {\"matches\": []}}"}]}))
    hook_main(_payload("PostToolUse", repo, tool_name="Grep", tool_input={"pattern": "PAY-101"}, tool_response="a.py:1: PAY-101"))
    hook_main(_payload("UserPromptSubmit", repo, prompt="and the logs?"))
    transcript = repo / "t.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "why?"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "x"}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "PAY-101 fails because the gateway times out."}]}}),
    ]))
    hook_main(_payload("Stop", repo, stop_hook_active=False))
    s = load_session(st.traces / "sess-1.jsonl")
    assert s.tool_sequence == ["slack.conversations_search_messages", "claude-code.Grep"]
    assert [p["text"] for p in s.prompts] == ["why is PAY-101 failing?", "and the logs?"]
    assert s.result == "PAY-101 fails because the gateway times out."
    assert [c["seq"] for c in s.calls] == [1, 2]


def test_stop_without_a_trace_and_garbage_input_are_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert hook_main(_payload("Stop", repo, sid="never")) == 0
    assert not (state_mod.state_for(repo.resolve()).traces / "never.jsonl").exists()
    assert hook_main({"hook_event_name": "PreToolUse", "cwd": str(repo)}) == 0
    assert hook_main({"hook_event_name": "PostToolUse", "cwd": "/definitely/not/a/dir", "tool_name": "mcp__x__y", "session_id": "z"}) == 0


def test_hook_records_reads_only_inside_an_investigation(tmp_path):
    """A session that never calls an MCP server (a code review) leaves no trace; inside a recorded session, Read
    results are kept as short previews, never whole files."""
    big = "x" * 5000
    read = {"session_id": "sess", "tool_name": "Read", "tool_input": {"file_path": "a.py"}, "tool_response": {"type": "text", "file": {"filePath": "a.py", "content": big}}}
    assert hook_main(read, trace_dir=tmp_path) == 0 and not (tmp_path / "sess.jsonl").exists()
    mcp = {"session_id": "sess", "tool_name": "mcp__jira__jira_get_issue", "tool_input": {"issue_key": "PAY-101"},
           "tool_response": {"content": [{"type": "text", "text": json.dumps({"key": "PAY-101"})}]}}
    hook_main(mcp, trace_dir=tmp_path)
    hook_main(read, trace_dir=tmp_path)
    s = load_session(tmp_path / "sess.jsonl")
    assert s.tool_sequence == ["jira.jira_get_issue", "claude-code.Read"] and s.calls[0]["output"] == {"key": "PAY-101"}
    content = s.calls[1]["output"]["file"]["content"]
    assert len(content) < 400 and content.endswith("(5000 chars)")


def test_hook_via_cli_stdin(tmp_path, monkeypatch):
    import io
    import sys
    from crystal.cli import main
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_payload("UserPromptSubmit", repo, prompt="hello"))))
    assert main(["hook"]) == 0
    assert load_session(state_mod.state_for(repo.resolve()).traces / "sess-1.jsonl").prompt == "hello"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert main(["hook"]) == 0
