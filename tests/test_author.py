"""Plumbing of `crystal author` / `crystal repair` with a fake driver: no agent is launched, nothing costs money.

The fake driver writes a trace the way the hook would: meta, then a flows.run_flow call (whose output names a saved
run record), then raw tool calls the agent made on top. The run_flow call must be expanded into the flow's own
calls, the inducer must produce <base>.v<N>.yaml alongside (never overwriting), and complaints must be marked handled.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from crystal import author
from crystal.author import (author as author_cmd, author_prompt, base_name, expand_flow_calls, flow_calls, mark_handled,
                            next_version, pending_complaints, repair, repair_prompt)
from crystal.trace.record import Recorder
from crystal.trace.store import load_session

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "traces"
sys.path.insert(0, str(ROOT / "sim" / "servers"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """tmp copies of: three scripted traces, the two flows, an empty runs dir, a feedback queue."""
    tdir, fdir, rdir = tmp_path / "traces", tmp_path / "flows", tmp_path / "runs"
    tdir.mkdir(), fdir.mkdir(), rdir.mkdir()
    src = sorted(TRACES.glob("scripted-PAY-101-v*.jsonl")) + sorted(TRACES.glob("scripted-STF-109-v*.jsonl"))[:1]
    if len(src) < 3:
        pytest.skip("scripted traces missing")
    for p in src:
        shutil.copy(p, tdir / p.name)
    for p in (ROOT / "flows").glob("*.yaml"):
        if not author.VERSION_RX.match(p.stem):      # base flows only; induced versions in the repo must not shift N
            shutil.copy(p, fdir / p.name)
    monkeypatch.setattr(author, "FLOW_DIR", fdir)
    monkeypatch.setattr(author, "RUN_DIR", rdir)
    monkeypatch.setattr(author, "TRACE_DIR", tdir)
    monkeypatch.setattr(author, "FEEDBACK", tdir / "feedback.jsonl")
    import crystal.flow.runner as runner_mod
    monkeypatch.setattr(runner_mod, "FLOW_DIR", fdir)
    return {"traces": tdir, "flows": fdir, "runs": rdir, "feedback": tdir / "feedback.jsonl"}


def run_record_from_trace(path: Path, flow_name: str, run_id: str) -> dict:
    """A saved run record shaped like FlowRunner's, built from a scripted session's calls (one step per call)."""
    s = load_session(path)
    steps = []
    for i, c in enumerate(s.calls):
        steps.append({"id": f"s{i}", "title": f"s{i}", "tool": f"{c['server']}.{c['tool']}", "args": c["input"], "result": c["output"],
                      "error": None, "attempts": [], "extracts": {}, "hits": 1})
    return {"run_id": run_id, "flow": flow_name, "inputs": s.meta["inputs"], "status": "ok", "steps": steps,
            "summary": {st["id"]: {"hits": 1, "error": None, "skipped": None} for st in steps}}


def fake_driver_factory(world, extra_calls, run_flow_first=True, cost=0.42):
    """Returns a driver_fn that records: [run_flow(flow)] + extra_calls, and a run record for the flow run."""
    def driver_fn(trigger, inputs, prompt, budget="3", model=None, meta=None, **kw):
        assert "mcp__flows__run_flow" in prompt and "--" not in str(budget)
        sid = "fake-session"
        rec = Recorder(sid, "claude-code", trace_dir=world["traces"], meta={"trigger": trigger, "inputs": inputs, "prompt": prompt, **(meta or {})})
        key = inputs["key"]
        base_trace = world["traces"] / f"scripted-{key}-v0.jsonl"
        run_id = "20260909T000000-fake01"
        record = run_record_from_trace(base_trace, "investigate-jira-ticket", run_id)
        (world["runs"] / f"{run_id}.json").write_text(json.dumps(record))
        summary = {"run_id": run_id, "flow": "investigate-jira-ticket", "status": "ok", "steps": [{"id": st["id"], "hits": 1} for st in record["steps"]]}
        if run_flow_first:
            rec.record("flows", "run_flow", {"name": "investigate-jira-ticket", "inputs_json": json.dumps(inputs)}, summary, json.dumps(summary))
        rec.record("flows", "list_flows", {}, {"flows": []}, "{}")
        rec.record("claude-code", "Read", {"file_path": "x"}, "text", "text")
        for server, tool, args, out in extra_calls(key):
            rec.record(server, tool, args, out, json.dumps(out))
        return {"session_id": sid, "cost_usd": cost, "num_turns": 7, "duration_ms": 1234, "is_error": False, "result": "summary: added comments",
                "returncode": 0, "stderr": "", "trace_path": str(rec.path),
                "calls": [{"seq": i + 1, "server": "flows", "tool": "run_flow", "input": {}} for i in range(1)]}
    return driver_fn


def extra_comments(key):
    return [("jira", "jira_get_issue_comments", {"issue_key": key},
             {"comments": [{"author": "a", "created": "2026-08-06T10:00:00Z", "body": f"Resolved by rolling back deadbeef00. See {key}."}]})]


def test_prompt_lists_flows_and_orders_run_flow_first():
    flows = [{"name": "investigate-jira-ticket", "status": "candidate", "inputs": {"key": {"type": "jira_key"}},
              "steps": [{"id": "issue", "tool": "jira.jira_get_issue"}, {"id": "slack", "tool": "slack.conversations_search_messages"}]}]
    p = author_prompt("jira_issue", {"key": "PAY-108"}, flows)
    assert "PAY-108" in p and "investigate-jira-ticket [candidate]" in p and "issue(jira.jira_get_issue) -> slack(" in p
    assert p.index("Step 1") < p.index("mcp__flows__run_flow") < p.index("Step 2") < p.index("raw MCP tools")
    assert "do not ask questions" in p
    p0 = author_prompt("jira_issue", {"key": "PAY-108"}, [])
    assert "No crystallized flow" in p0 and "run_flow" not in p0


def test_repair_prompt_carries_yaml_inputs_evidence_and_complaint():
    complaint = {"run_id": "r1", "flow": "investigate-jira-ticket", "inputs": {"key": "SUP-102"}, "text": "missing the deploy diff"}
    record = {"run_id": "r1", "status": "ok", "inputs": {"key": "SUP-102"},
              "steps": [{"id": "issue", "tool": "jira.jira_get_issue", "hits": 1, "args": {"issue_key": "SUP-102"}, "extracts": {"service": "payments-api", "trace_ids": ["a" * 32] * 9}},
                        {"id": "logs", "tool": "logz.search_logs", "hits": 3, "items": [{"item": "t1", "hits": 3}]},
                        {"id": "runbook", "tool": "confluence.confluence_get_page", "hits": 0, "error": "boom", "args": {}}]}
    p = repair_prompt(complaint, {"name": "investigate-jira-ticket"}, "name: investigate-jira-ticket\nsteps: []", record)
    assert "missing the deploy diff" in p and "```yaml" in p and '"key": "SUP-102"' in p
    assert "- issue jira.jira_get_issue: hits=1" in p and "items=t1(3)" in p and "ERROR boom" in p
    assert p.count("a" * 32) == 5   # lists are capped
    assert "mcp__flows__run_flow" in p and 'name="investigate-jira-ticket"' in p


def test_flow_calls_recreate_ladders_and_fanout():
    record = {"flow": "f", "steps": [
        {"id": "issue", "tool": "jira.jira_get_issue", "args": {"issue_key": "PAY-101"}, "result": {"key": "PAY-101"}, "error": None, "attempts": []},
        {"id": "slack", "tool": "slack.conversations_search_messages", "args": {"limit": 20, "search_query": "PAY-101"}, "result": {"messages": {"matches": [1]}}, "error": None,
         "attempts": [{"rung": 0, "value": "", "skipped": "blank"}, {"rung": 1, "value": "\"Err\" in:#x", "hits": 0, "error": None}, {"rung": 2, "value": "PAY-101", "hits": 1, "error": None}]},
        {"id": "logs", "tool": "logz.search_logs", "items": [{"item": "t1", "args": {"query": "trace_id:t1"}, "result": {"hits": [1]}, "error": None},
                                                            {"item": "t2", "args": {"query": "trace_id:t2"}, "result": {"hits": []}, "error": None}]},
        {"id": "skipped", "tool": "git.git_show", "skipped": "when=false"}]}
    calls = flow_calls(record)
    assert [(c["server"], c["tool"], c["input"]) for c in calls] == [
        ("jira", "jira_get_issue", {"issue_key": "PAY-101"}),
        ("slack", "conversations_search_messages", {"limit": 20, "search_query": "\"Err\" in:#x"}),
        ("slack", "conversations_search_messages", {"limit": 20, "search_query": "PAY-101"}),
        ("logz", "search_logs", {"query": "trace_id:t1"}), ("logz", "search_logs", {"query": "trace_id:t2"})]
    assert calls[1]["output"] == {} and calls[2]["output"] == {"messages": {"matches": [1]}}
    assert all(c["from_flow"] == "f" for c in calls)


def test_next_version_never_overwrites(world):
    fdir = world["flows"]
    p, n = next_version("investigate-jira-ticket", fdir)
    assert (p.name, n) == ("investigate-jira-ticket.v2.yaml", 2)
    p.write_text("name: x\nversion: 2\n")
    (fdir / "investigate-jira-ticket.v7.yaml").write_text("name: y\nversion: 7\n")
    p, n = next_version("investigate-jira-ticket", fdir)
    assert (p.name, n) == ("investigate-jira-ticket.v8.yaml", 8)
    assert base_name("investigate-jira-ticket.v8") == "investigate-jira-ticket" and base_name("plain") == "plain"
    assert next_version("brand-new", fdir)[1] == 1


def test_author_expands_run_flow_and_induces_new_version(world):
    fdir = world["flows"]
    before = {p.name: p.read_text() for p in fdir.glob("*.yaml")}
    res = author_cmd("jira_issue", {"key": "PAY-101"}, budget="3", driver_fn=fake_driver_factory(world, extra_comments),
                     trace_dir=world["traces"], flow_dir=fdir, run_dir=world["runs"], test=False)
    assert res["ran_flow_first"] is True and res["ran_flows"] == ["investigate-jira-ticket"]
    assert res["raw_calls"] == ["jira.jira_get_issue_comments"]
    assert res["agent"]["cost_usd"] == 0.42
    # expansion: run_flow -> the flow's own calls; list_flows and Read dropped; the agent's raw call kept last
    sess = load_session(world["traces"] / "fake-session.jsonl")
    expanded, ran = expand_flow_calls(sess, world["runs"])
    seq = expanded.tool_sequence
    assert seq[0] == "jira.jira_get_issue" and seq[-1] == "jira.jira_get_issue_comments" and len(seq) > 5
    assert not any(t.startswith(("flows.", "claude-code.")) for t in seq)
    # a new version alongside, never an overwrite
    assert res["path"] == fdir / "investigate-jira-ticket.v2.yaml" and res["path"].exists()
    assert {p.name: p.read_text() for p in fdir.glob("*.yaml") if p.name in before} == before
    flow = yaml.safe_load(res["path"].read_text())
    assert flow["name"] == "investigate-jira-ticket.v2" and flow["version"] == 2 and flow["base"] == "investigate-jira-ticket"
    assert flow["status"] == "draft" and flow["authored"]["command"] == "author" and flow["authored"]["cost_usd"] == 0.42
    assert "fake-session" in flow["induced_from"] and res["sessions"] == 5
    tools = [s["tool"] for s in flow["steps"]]
    assert "jira.jira_get_issue_comments" in tools and tools[0] == "jira.jira_get_issue"
    comments = next(s for s in flow["steps"] if s["tool"] == "jira.jira_get_issue_comments")
    assert comments["args"] == {"issue_key": "{{ inputs.key }}"} and comments.get("optional") and comments["seen_in"] == "1/5 sessions"
    assert res["report"]["added_steps"] == [comments["id"]]
    assert flow["inputs"]["key"]["type"] == "jira_key"


def test_author_without_run_flow_still_induces_from_raw_calls(world):
    res = author_cmd("jira_issue", {"key": "PAY-101"}, driver_fn=fake_driver_factory(world, extra_comments, run_flow_first=False),
                     trace_dir=world["traces"], flow_dir=world["flows"], run_dir=world["runs"], test=False)
    assert res["ran_flow_first"] is False and res["ran_flows"] == []
    assert res["path"].name == "investigate-jira-ticket.v2.yaml"   # falls back to the trigger's first flow as base


def test_repair_marks_complaint_handled_without_deleting(world):
    fb = world["feedback"]
    fb.write_text(json.dumps({"ts": "t", "run_id": "run-A", "flow": "investigate-jira-ticket", "inputs": {"key": "PAY-101"}, "helpful": False, "text": "missing the comments"}) + "\n"
                  + json.dumps({"ts": "t", "kind": "feedback", "run_id": "run-B", "flow": "investigate-jira-ticket", "inputs": {"key": "STF-109"}, "helpful": True, "text": ""}) + "\n"
                  + json.dumps({"ts": "t", "kind": "feedback", "run_id": "run-C", "flow": "investigate-jira-ticket", "inputs": {"key": "STF-109"}, "helpful": False, "text": "also bad"}) + "\n")
    assert [c["run_id"] for c in pending_complaints(fb)] == ["run-A", "run-C"]
    mark_handled("run-C", {"session_id": "s"}, fb)
    assert [c["run_id"] for c in pending_complaints(fb)] == ["run-A"]
    prompts = []

    def spy(trigger, inputs, prompt, **kw):
        prompts.append(prompt)
        return fake_driver_factory(world, extra_comments, cost=0.2)(trigger, inputs, prompt, **kw)
    results = repair("--all", driver_fn=spy, feedback_path=fb, trace_dir=world["traces"], flow_dir=world["flows"], run_dir=world["runs"], test=False)
    assert len(results) == 1 and results[0]["complaint"]["run_id"] == "run-A"
    assert "missing the comments" in prompts[0] and "(run record not available)" in prompts[0] and "name: investigate-jira-ticket" in prompts[0]
    assert results[0]["path"].name == "investigate-jira-ticket.v2.yaml"
    assert yaml.safe_load(results[0]["path"].read_text())["authored"]["complaint"] == "missing the comments"
    lines = [json.loads(l) for l in fb.read_text().splitlines()]
    assert len(lines) == 5 and lines[-1]["kind"] == "handled" and lines[-1]["run_id"] == "run-A" and lines[-1]["cost_usd"] == 0.2
    assert lines[-1]["new_flow"] == "investigate-jira-ticket.v2"
    assert pending_complaints(fb) == []
    assert lines[0]["run_id"] == "run-A"   # the original complaint is still there


def test_confirmation_required(monkeypatch, capsys):
    from crystal.author import confirm
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert confirm(["--yes"], "x") is True
    assert confirm([], "x") is False and "refusing" in capsys.readouterr().out


def test_flows_server_summary_is_compact():
    flows_srv = __import__("flows")
    big = {"run_id": "r", "flow": "f", "status": "ok", "inputs": {"key": "PAY-101"}, "steps": [
        {"id": f"s{i}", "tool": "logz.search_logs", "args": {"query": "x" * 500}, "hits": 50, "error": None, "attempts": [],
         "extracts": {"lines": ["line " * 40] * 50}, "result": {"hits": [{"_source": {"message": "m" * 400, "level": "ERROR"}}] * 50}} for i in range(12)]}
    out = flows_srv.summarize_run(big)
    assert len(json.dumps(out)) <= flows_srv.MAX_CHARS and out["truncated"] is True
    assert [s["id"] for s in out["steps"]] == [f"s{i}" for i in range(12)] and out["steps"][0]["hits"] == 50
    small = {"run_id": "r", "flow": "f", "status": "ok", "inputs": {}, "steps": [
        {"id": "issue", "tool": "jira.jira_get_issue", "args": {"issue_key": "PAY-101"}, "hits": 1, "error": None, "extracts": {"service": "payments-api"},
         "attempts": [{"rung": 0, "value": "a", "hits": 0}, {"rung": 1, "value": "b", "hits": 1}], "result": {"key": "PAY-101", "fields": {"summary": "s", "description": "d"}}},
        {"id": "logs", "tool": "logz.search_logs", "items": [{"item": "t1", "args": {"query": "q"}, "hits": 1, "extracts": {}, "error": None, "result": {"hits": [{"_source": {"message": "boom"}}]}}]}]}
    out = flows_srv.summarize_run(small)
    assert out["truncated"] is False and out["steps"][0]["evidence"][0]["kind"] == "issue" and out["steps"][0]["ladder"][1]["hits"] == 1
    assert out["steps"][1]["items"][0]["evidence"][0]["message"] == "boom"
