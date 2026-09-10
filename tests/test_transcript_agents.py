"""Subagents in transcripts: a session whose prompt fans out to two subagents (their transcripts under
`<session-id>/subagents/agent-<id>.jsonl`) is parsed with every call tagged by agent, split into one episode per
(prompt, agent), and when the hook already recorded the session its trace is reattributed (joined on tool_use_id)
instead of being imported twice."""
import json
from pathlib import Path

from crystal import state as state_mod
from crystal.cli import main as cli_main
from crystal.induce.mining import episodes
from crystal.trace.record import Recorder
from crystal.trace.store import load_session, load_sessions
from crystal.trace.transcripts import hook_trace_of, import_transcripts, parse_transcript, write_episodes, write_session

AG1, AG2 = "a1111111111111111", "a2222222222222222"
SID = "agent-session-0001"


def _line(typ, content, ts, sid, agent=None, **kw):
    rec = {"type": typ, "message": {"role": typ, "content": content}, "timestamp": ts, "cwd": kw.pop("cwd"), "sessionId": sid,
           "uuid": kw.pop("uuid", ts), "isSidechain": agent is not None}
    if agent:
        rec["agentId"] = agent
    rec.update(kw)
    return json.dumps(rec)


def _use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _res(tid, content, is_error=False):
    return {"tool_use_id": tid, "type": "tool_result", "content": content, "is_error": is_error}


def _j(o):
    return [{"type": "text", "text": json.dumps(o)}]


def write_agent_session(base: Path, cwd: Path, sid: str = SID) -> Path:
    """A session whose second prompt fans out to two subagents (in <sid>/subagents/), one with a meta.json naming
    the Agent tool_use, one without (matched by its prompt). Main makes one MCP call under each prompt."""
    d = base / "proj"
    (d / sid / "subagents").mkdir(parents=True)
    c = str(cwd)
    main = [
        _line("user", "Look at PAY-101 quickly", "2026-09-10T02:00:00.000Z", sid, cwd=c),
        _line("assistant", [_use("t_m1", "mcp__jira__jira_get_issue", {"issue_key": "PAY-101"})], "2026-09-10T02:00:05.000Z", sid, cwd=c),
        _line("user", [_res("t_m1", _j({"key": "PAY-101", "fields": {"summary": "payments down"}}))], "2026-09-10T02:00:06.000Z", sid, cwd=c),
        _line("assistant", [{"type": "text", "text": "PAY-101 is payments down."}], "2026-09-10T02:00:07.000Z", sid, cwd=c),
        _line("user", "Now fan out: check slack and pagerduty in parallel", "2026-09-10T02:01:00.000Z", sid, cwd=c),
        _line("assistant", [_use("t_ag1", "Agent", {"description": "slack sweep", "subagent_type": "general-purpose", "prompt": "Search slack for PaymentGatewayTimeout"}),
                            _use("t_ag2", "Agent", {"description": "pagerduty sweep", "subagent_type": "general-purpose", "prompt": "List pagerduty incidents for PXYZ01"})],
              "2026-09-10T02:01:05.000Z", sid, cwd=c),
        _line("user", [_res("t_ag1", "launched"), _res("t_ag2", "launched")], "2026-09-10T02:01:06.000Z", sid, cwd=c),
        _line("assistant", [_use("t_m2", "mcp__jira__jira_get_issue_comments", {"issue_key": "PAY-101"})], "2026-09-10T02:01:10.000Z", sid, cwd=c),
        _line("user", [_res("t_m2", _j({"comments": []}))], "2026-09-10T02:01:11.000Z", sid, cwd=c),
        _line("assistant", [{"type": "text", "text": "Both sweeps are done; here is the summary."}], "2026-09-10T02:03:00.000Z", sid, cwd=c),
    ]
    (d / f"{sid}.jsonl").write_text("\n".join(main) + "\n")
    a1 = [
        _line("user", "Search slack for PaymentGatewayTimeout", "2026-09-10T02:01:07.000Z", sid, AG1, cwd=c),
        _line("assistant", [_use("t_a1_1", "mcp__slack__conversations_search_messages", {"search_query": "PaymentGatewayTimeout"})], "2026-09-10T02:01:20.000Z", sid, AG1, cwd=c),
        _line("user", [_res("t_a1_1", _j({"messages": {"matches": [{"ts": "1787484120.000005", "text": "seen it"}]}}))], "2026-09-10T02:01:21.000Z", sid, AG1, cwd=c),
        _line("assistant", [_use("t_a1_2", "mcp__slack__conversations_replies", {"channel_id": "C1", "ts": "1787484120.000005"})], "2026-09-10T02:01:30.000Z", sid, AG1, cwd=c),
        _line("user", [_res("t_a1_2", _j({"messages": [{"text": "reply"}]}))], "2026-09-10T02:01:31.000Z", sid, AG1, cwd=c),
        _line("assistant", [{"type": "text", "text": "Found the thread."}], "2026-09-10T02:01:40.000Z", sid, AG1, cwd=c),
    ]
    (d / sid / "subagents" / f"agent-{AG1}.jsonl").write_text("\n".join(a1) + "\n")
    (d / sid / "subagents" / f"agent-{AG1}.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "slack sweep", "toolUseId": "t_ag1"}))
    a2 = [
        _line("user", "List pagerduty incidents for PXYZ01", "2026-09-10T02:01:08.000Z", sid, AG2, cwd=c),
        _line("assistant", [_use("t_a2_1", "Bash", {"command": "date"})], "2026-09-10T02:01:15.000Z", sid, AG2, cwd=c),
        _line("user", [_res("t_a2_1", "Wed Sep 10")], "2026-09-10T02:01:16.000Z", sid, AG2, cwd=c),
        _line("assistant", [_use("t_a2_2", "mcp__pagerduty__list_incidents", {"service_ids": ["PXYZ01"]})], "2026-09-10T02:01:25.000Z", sid, AG2, cwd=c),
        _line("user", [_res("t_a2_2", _j({"incidents": [{"id": "Q1"}]}))], "2026-09-10T02:01:26.000Z", sid, AG2, cwd=c),
        _line("assistant", [{"type": "text", "text": "One incident, Q1."}], "2026-09-10T02:01:50.000Z", sid, AG2, cwd=c),
    ]
    (d / sid / "subagents" / f"agent-{AG2}.jsonl").write_text("\n".join(a2) + "\n")
    return d / f"{sid}.jsonl"


def test_two_sidechains_become_separate_episodes(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    ws = tmp_path / "acme-api"
    ws.mkdir()
    p = write_agent_session(tmp_path / "transcripts", ws)
    tx = parse_transcript(p)
    assert len(tx.prompts) == 2 and sorted(tx.agents) == [AG1, AG2]
    assert tx.agents[AG1]["prompt_index"] == 1 and tx.agents[AG1]["tool_use_id"] == "t_ag1" and tx.agents[AG1]["description"] == "slack sweep"
    assert tx.agents[AG2]["prompt_index"] == 1 and tx.agents[AG2]["tool_use_id"] is None      # no meta.json: matched by its prompt
    assert tx.agents[AG2]["prompt"] == "List pagerduty incidents for PXYZ01" and tx.agents[AG2]["result"] == "One incident, Q1."
    # calls interleave by time, each tagged with its agent and the main prompt it ran under
    assert [(c.agent, c.tool, c.prompt_index) for c in tx.calls] == [
        ("main", "jira_get_issue", 0), ("main", "jira_get_issue_comments", 1), (AG2, "Bash", 1), (AG1, "conversations_search_messages", 1),
        (AG2, "list_incidents", 1), (AG1, "conversations_replies", 1)]
    assert tx.episode_keys() == [(0, "main"), (1, "main"), (1, AG1), (1, AG2)]
    assert tx.episode_numbers() == {(0, "main"): "e1", (1, "main"): "e2", (1, AG1): f"e2-a{AG1}", (1, AG2): f"e2-a{AG2}"}

    st = state_mod.state_for(ws).ensure()
    write_session(tx, st.traces)
    eps = write_episodes(tx, st.traces)
    assert [e.name for e in eps] == [f"{SID}-e1.jsonl", f"{SID}-e2.jsonl", f"{SID}-e2-a{AG1}.jsonl", f"{SID}-e2-a{AG2}.jsonl"]
    whole = load_session(st.traces / f"{SID}.jsonl")
    assert whole.meta["episodes"] == 4 and set(whole.meta["agents"]) == {AG1, AG2} and [c["agent"] for c in whole.calls][:3] == ["main", "main", AG2]
    e2 = load_session(st.traces / f"{SID}-e2.jsonl")
    assert [c["tool"] for c in e2.calls] == ["jira_get_issue_comments"] and e2.meta["agent"] == "main"
    s1 = load_session(st.traces / f"{SID}-e2-a{AG1}.jsonl")
    assert [c["tool"] for c in s1.calls] == ["conversations_search_messages", "conversations_replies"]
    assert s1.meta["agent"] == AG1 and s1.meta["agent_description"] == "slack sweep" and s1.meta["parent_session"] == SID
    assert s1.meta["prompt"] == "Search slack for PaymentGatewayTimeout" and s1.meta["parent_prompt"].startswith("Now fan out")
    assert s1.prompt == "Search slack for PaymentGatewayTimeout" and s1.result == "Found the thread."
    s2 = load_session(st.traces / f"{SID}-e2-a{AG2}.jsonl")
    assert [c["tool"] for c in s2.calls] == ["Bash", "list_incidents"] and s2.meta["inputs"] == {"prompt": "List pagerduty incidents for PXYZ01"}
    # mining: the whole session is represented by its four episodes only
    assert sorted(e.session_id for e in episodes(load_sessions(st.traces))) == sorted(e.stem for e in eps)


def test_import_reattributes_a_hook_trace_instead_of_importing_twice(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    ws = tmp_path / "acme-api"
    ws.mkdir()
    base = tmp_path / "transcripts"
    tx = parse_transcript(write_agent_session(base, ws))
    st = state_mod.state_for(ws).ensure()
    # what the hook recorded for this session: the same calls under the parent session id, interleaved, unattributed,
    # with the outputs the hook saw (richer than what a transcript keeps)
    hook = Recorder(SID, "claude-code", trace_dir=st.traces, meta={"transcript_path": str(tx.path), "cwd": str(ws)})
    hook.prompt("Look at PAY-101 quickly")
    for c in tx.calls:
        hook.record(c.server, c.tool, c.input, {"hook": True, "tool": c.tool}, "hook output", extra={"tool_use_id": c.tool_use_id})
    hook.record("fetch", "fetch", {"url": "x"}, {}, "", extra={"tool_use_id": "t_not_in_transcript"})
    hook.result("done")
    before = (st.traces / f"{SID}.jsonl").read_text()
    assert hook_trace_of(st.traces, SID) is not None and hook_trace_of(st.traces, "nope") is None

    rep = import_transcripts(base, workspace_root=ws, dry_run=True)
    assert rep["rows"][0]["status"] == "would reattribute hook trace" and rep["reattributed"] == 1 and rep["imported"] == 0
    assert (st.traces / f"{SID}.jsonl").read_text() == before
    rep = import_transcripts(base, workspace_root=ws)
    assert rep["reattributed"] == 1 and rep["imported"] == 0 and rep["episodes"] == 4
    assert rep["rows"][0]["status"] == "reattributed hook trace (6 joined, 1 unjoined)"
    whole = load_session(st.traces / f"{SID}.jsonl")
    assert whole.source == "claude-code" and whole.meta["reattributed"] is True and whole.meta["episodes"] == 4 and set(whole.meta["agents"]) == {AG1, AG2}
    assert whole.meta["transcript_joined"] == 6 and whole.meta["transcript_unjoined"] == 1
    assert [c.get("agent") for c in whole.calls] == ["main", "main", AG2, AG1, AG2, AG1, None]     # nothing removed; the stray call stays untagged
    assert [c.get("prompt_index") for c in whole.calls][:6] == [0, 1, 1, 1, 1, 1]
    assert whole.calls[0]["output"] == {"hook": True, "tool": "jira_get_issue"} and whole.prompts[0]["text"] == "Look at PAY-101 quickly" and whole.result == "done"
    s1 = load_session(st.traces / f"{SID}-e2-a{AG1}.jsonl")
    assert s1.source == "claude-code" and s1.meta["from_hook_trace"] == f"{SID}.jsonl" and s1.meta["agent"] == AG1
    assert [c["tool"] for c in s1.calls] == ["conversations_search_messages", "conversations_replies"]
    assert s1.calls[0]["output"] == {"hook": True, "tool": "conversations_search_messages"}      # the hook's output, not the transcript's
    assert s1.prompt == "Search slack for PaymentGatewayTimeout" and s1.result == "Found the thread."
    assert sorted(e.session_id for e in episodes(load_sessions(st.traces))) == sorted([f"{SID}-e1", f"{SID}-e2", f"{SID}-e2-a{AG1}", f"{SID}-e2-a{AG2}"])
    # idempotent: a second import leaves it alone; --reattribute (or --force) redoes the join
    after = (st.traces / f"{SID}.jsonl").read_text()
    rep = import_transcripts(base, workspace_root=ws)
    assert rep["rows"][0]["status"] == "hook trace (reattributed)" and rep["skipped"] == 1 and (st.traces / f"{SID}.jsonl").read_text() == after
    assert cli_main(["--workspace", str(ws), "import", "--transcripts", str(base), "--reattribute"]) == 0
    assert "reattributed hook trace (6 joined" in capsys.readouterr().out
    assert len(list(st.traces.glob(f"{SID}-e*.jsonl"))) == 4
