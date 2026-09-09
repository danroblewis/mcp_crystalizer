"""The inducer must compile recorded traces into a flow that (a) binds every argument, (b) detects the
fan-out over trace ids and the Slack search ladder, and (c) runs end to end on a ticket it never saw.

The second half mixes the real Claude Code session in: steps align by signature rather than occurrence,
timestamp args never ladder (rounded windows still bind), the real agent's extra steps stay optional and
bound, and the 16-session flow has no unresolved bindings and runs on an untraced ticket."""
import asyncio
from pathlib import Path

import pytest

from crystal.extract.extractors import run_extractor
from crystal.flow.runner import FlowRunner
from crystal.induce.inducer import Binder, induce
from crystal.extract.catalog import load_catalog
from crystal.mcp_client import ServerPool
from crystal.trace.store import Session, load_sessions

TRACES = Path(__file__).resolve().parent.parent / "traces"
REAL_SESSION = "1b8cd469-e5eb-4e86-a59b-f9af774b604f"
TS_ARGS = {"start", "end", "from_time", "to_time", "since", "until"}


@pytest.fixture(scope="module")
def sessions():
    s = [x for x in load_sessions(TRACES, trigger="jira_issue") if x.source == "scripted"]
    if len(s) < 6:
        pytest.skip("run `uv run python -m crystal.trace.scripted PAY-101 STF-109 SUP-102 PLAT-102 PAY-102 --variants 3` first")
    return s


@pytest.fixture(scope="module")
def induced(sessions):
    flow, report = induce(sessions, "test-induced")
    return flow, report


@pytest.fixture(scope="module")
def all_sessions(sessions):
    real = [x for x in load_sessions(TRACES, trigger="jira_issue") if x.session_id == REAL_SESSION]
    if not real:
        pytest.skip("the recorded Claude Code session is missing")
    return real + sessions


@pytest.fixture(scope="module")
def induced_all(all_sessions):
    return induce(all_sessions, "test-induced-all")


@pytest.fixture(scope="module")
def real_session(all_sessions):
    return next(s for s in all_sessions if s.session_id == REAL_SESSION)


def _step(flow, sid):
    return next(s for s in flow["steps"] if s["id"] == sid)


def _call(server, tool, inp, out):
    return {"server": server, "tool": tool, "input": inp, "output": out, "is_error": False}


def _issue(key, svc, err, tid, created="2026-08-27T13:47:00Z"):
    return {"key": key, "fields": {"summary": f"{svc}: elevated 5xx after {err}", "created": created,
                                   "description": f"Alert fired on {svc} at 2026-08-27T13:35:00Z.\n\nError: {err}: boom\nSample trace: trace_id={tid}"}}


def _synthetic(sid, key, svc, err, tid, order, extra=False):
    """A tiny session over the same three tools, in the given order, optionally with one more call."""
    issue = _call("jira", "jira_get_issue", {"issue_key": key}, _issue(key, svc, err, tid))
    grep = _call("code", "grep", {"pattern": err, "glob": "**/*.py"}, {"matches": [{"file": f"services/{svc}/h.py", "line": 1}]})
    logs = _call("logz", "search_logs", {"query": f"trace_id:{tid}", "from_time": "2026-08-27T12:47:00Z", "to_time": "2026-08-27T19:47:00Z"},
                 {"total": 1, "hits": [{"_source": {"trace_id": tid, "message": "x"}}]})
    calls = [issue] + [{"grep": grep, "logs": logs}[n] for n in order]
    if extra:
        calls.append(_call("pagerduty", "list_services", {}, {"services": [{"id": "PXYZ01", "name": svc}]}))
    return Session(session_id=sid, source="scripted", meta={"trigger": "t", "inputs": {"key": key}}, calls=calls)


def test_binding_kinds_on_one_session(sessions):
    b = Binder(sessions[0], load_catalog())
    steps = b.bind_session()
    issue = steps[0]
    assert issue["tool"] == "jira.jira_get_issue" and issue["args"] == {"issue_key": "{{ inputs.key }}"}
    assert issue["kinds"]["issue_key"] == "input"
    kinds = {k for st in steps for k in st["kinds"].values()}
    assert {"window", "composite"} <= kinds
    # per session, only constants may stay unresolved (the merge proves them constant); never an ID-shaped value
    from crystal.extract import ids
    leftover = [st["raw_args"][k] for st in steps for k, kind in st["kinds"].items() if kind == "unresolved"]
    assert all(ids.type_of(str(v)) is None for v in leftover), leftover
    assert set(map(str, leftover)) <= {"**/*.py"}, leftover


def test_no_unresolved_and_inputs(induced):
    flow, report = induced
    assert report["unresolved"] == {}
    assert flow["inputs"]["key"]["type"] == "jira_key"


def test_ladder_and_fanout(induced):
    flow, report = induced
    slack = _step(flow, "slack")
    rungs = slack["args"]["search_query"]["ladder"]
    assert len(rungs) >= 2 and all("{{" in r for r in rungs)
    assert any("issue.error" in r and "after:" in r for r in rungs)
    logs = _step(flow, "logs")
    assert logs["forEach"].startswith("(") and "trace_id" in logs["forEach"]
    assert logs["args"]["query"] == "trace_id:{{ item }}"
    assert "{{ issue.window.start }}" == logs["args"]["from_time"]


def test_catalog_attr_and_window_bindings(induced):
    flow, _ = induced
    assert "catalog.service[issue.service].pagerduty_service_id" in _step(flow, "pagerduty")["args"]["service_ids"]
    assert "catalog.service[issue.service].repo_path" in _step(flow, "commits")["args"]["path"]
    win = _step(flow, "issue")["extract"]["window"]
    assert win["using"] == "window" and win["before"] == "1h" and win["after"] == "6h"


def test_induced_flow_runs_on_unseen_ticket(induced):
    flow, _ = induced
    flow["name"] = "test-induced"

    async def go():
        async with ServerPool() as pool:
            return await FlowRunner(pool).run(flow, {"key": "STF-116"}, save=False)  # never traced
    rec = asyncio.run(go())
    assert rec["status"] == "ok"
    s = {st["id"]: st for st in rec["steps"]}
    assert s["issue"]["extracts"]["service"] == "checkout-web"
    assert s["slack"]["hits"] == 1 and s["thread"]["hits"] == 5
    assert sum(i["hits"] for i in s["logs"]["items"]) >= 8
    assert s["pagerduty"]["hits"] == 1 and s["metrics"]["hits"] == 1
    assert not any(st.get("error") for st in rec["steps"]), [st.get("error") for st in rec["steps"]]


# ---------------------------------------------------------------- (a) alignment by signature
def test_steps_align_by_signature_not_occurrence():
    a = _synthetic("a", "X-1", "alpha", "AlphaError", "a" * 32, ["grep", "logs"])
    b = _synthetic("b", "X-2", "beta", "BetaError", "b" * 32, ["logs", "grep"])
    c = _synthetic("c", "X-3", "gamma", "GammaError", "c" * 32, ["grep", "logs"], extra=True)
    flow, report = induce([a, b, c], "t", catalog={})
    ids_ = [st["id"] for st in flow["steps"]]
    assert ids_ == ["issue", "code", "logs", "list_services"], ids_
    assert not any(st.get("optional") for st in flow["steps"] if st["id"] in ("issue", "code", "logs"))
    assert report["alignment"]["code"]["sessions"] == 3 and report["alignment"]["logs"]["sessions"] == 3
    extra = _step(flow, "list_services")
    assert extra["optional"] and extra["seen_in"] == "1/3 sessions"
    assert _step(flow, "code")["args"]["pattern"] == "{{ issue.error_class }}"
    assert _step(flow, "logs")["args"]["query"] == "trace_id:{{ issue.trace_id }}"
    assert _step(flow, "logs")["args"]["from_time"] == "{{ issue.window.start }}"
    assert report["unresolved"] == {}


def test_real_session_aligns_with_scripted(induced_all):
    flow, report = induced_all
    ids_ = [st["id"] for st in flow["steps"]]
    assert len(ids_) == len(set(ids_))
    # the real agent's late thread read, reordered git_log and trace-id log searches all land in the shared steps
    for sid in ("thread", "logs", "logs_2", "commits", "metrics", "pagerduty"):
        assert report["alignment"][sid]["sessions"] == 16, (sid, report["alignment"][sid])
    assert "logs_3" not in ids_ and report["alignment"]["logs"]["local_ids"].get("logs_3") == 1
    logs = _step(flow, "logs")
    assert logs["args"]["query"] == "trace_id:{{ item }}"
    for ref in ("issue.trace_id", "thread.trace_ids", "logs_2.trace_ids"):
        assert ref in logs["forEach"]
    assert ids_.index("logs_2") < ids_.index("logs")   # references point backwards after the fan-out union


# ---------------------------------------------------------------- (b) timestamp args never ladder
def test_timestamp_args_never_ladder(induced_all):
    flow, report = induced_all
    for st in flow["steps"]:
        for k, v in st["args"].items():
            if k in TS_ARGS:
                assert not (isinstance(v, dict) and "ladder" in v), (st["id"], k, v)
                assert "{{" in str(v), (st["id"], k, v)
    assert _step(flow, "metrics")["args"]["start"] == "{{ issue.window.start }}"
    alts = report["window_alternatives"]["metrics"]["start"]
    assert alts["{{ issue.window.start }}"] == 15 and len(alts) == 2
    assert all("issue.window" in t for t in alts)   # the alternative is a rounded window, not a literal
    assert report["ladders"] == {"slack": {"search_query": 4}, "confluence": {"query": 2}, "metrics": {"query": 2},
                                 "logs_2": {"query": 2}, "commit": {"sha": 2}}


def test_rounded_window_binds_in_real_session(real_session):
    b = Binder(real_session, load_catalog())
    steps = b.bind_session()
    st = {s["id"]: s for s in steps}
    for sid, k in (("metrics", "start"), ("metrics", "end"), ("logs", "from_time"), ("logs", "to_time"),
                   ("pagerduty", "since"), ("pagerduty", "until"), ("commits", "since"), ("commits_2", "since")):
        assert st[sid]["kinds"][k] == "window", (sid, k, st[sid]["kinds"][k])
    assert not any(k == "unresolved" and str(s_["raw_args"][a]).endswith("Z") for s_ in steps for a, k in s_["kinds"].items())
    specs = list(b.extracts["issue"].values())
    metrics_win = next(sp for sp in specs if sp.get("using") == "window" and sp.get("round") == "1h" and sp.get("before") == "30m")
    assert metrics_win["after"] == "2h" and metrics_win["from"] == "fields.created"
    assert any(sp.get("round") == "1d" and sp.get("after") == "1d" for sp in specs)
    w = run_extractor(metrics_win, real_session.calls[0]["output"])
    assert (w["start"], w["end"]) == ("2026-08-27T12:30:00Z", "2026-08-27T15:00:00Z")


# ---------------------------------------------------------------- (c) the real agent's extra steps
def test_real_agent_extra_steps_are_optional_and_bound(induced_all):
    flow, report = induced_all
    for sid in ("jira_get_issue_comments", "list_services", "pagerduty_incident", "commits_2"):
        st = _step(flow, sid)
        assert st["optional"] and st["seen_in"] == "1/16 sessions", st
    assert _step(flow, "jira_get_issue_comments")["args"] == {"issue_key": "{{ inputs.key }}"}
    assert _step(flow, "pagerduty_incident")["args"]["incident_id"] == "{{ pagerduty.pd_incident_ids | first }}"
    assert _step(flow, "pagerduty")["extract"]["pd_incident_ids"] == {"from": "incidents[*].id", "all": True}
    c2 = _step(flow, "commits_2")
    assert "issue.service" in c2["args"]["path"]
    assert c2["args"]["since"] == "{{ issue.window_7d_r1d.start }}" and c2["args"]["until"] == "{{ issue.iso_ts }}"
    assert _step(flow, "issue")["extract"]["window_7d_r1d"] == {"from": "fields.created", "using": "window", "before": "7d", "round": "1d"}
    assert _step(flow, "commit")["args"]["sha"]["ladder"] == ["{{ thread.sha_shorts | first }}", "{{ jira_get_issue_comments.sha_shorts | first }}"]
    assert "position_programs" in report and "kinds" in report


# ---------------------------------------------------------------- (d) all 16 sessions: nothing unresolved, runs unseen
def test_all_sessions_no_unresolved_and_runs_on_unseen_ticket(induced_all):
    flow, report = induced_all
    assert report["unresolved"] == {}
    assert report["sessions"] == 16
    assert not any(st.get("unresolved") for st in flow["steps"])
    win = _step(flow, "issue")["extract"]["window"]
    assert win["before"] == "1h" and win["after"] == "6h" and "round" not in win

    async def go():
        async with ServerPool() as pool:
            return await FlowRunner(pool).run(flow, {"key": "STF-116"}, save=False)  # never traced
    rec = asyncio.run(go())
    assert rec["status"] == "ok"
    s = {st["id"]: st for st in rec["steps"]}
    assert s["issue"]["extracts"]["service"] == "checkout-web"
    assert s["slack"]["hits"] == 1 and s["thread"]["hits"] == 5
    assert sum(i["hits"] for i in s["logs"]["items"]) >= 8
    assert s["pagerduty"]["hits"] == 1 and s["metrics"]["hits"] == 1
    assert s["pagerduty_incident"]["hits"] == 1 and s["commits"]["hits"] >= 1
    assert not any(st.get("error") for st in rec["steps"]), [st.get("error") for st in rec["steps"]]
