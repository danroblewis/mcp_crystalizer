"""The inducer must compile recorded traces into a flow that (a) binds every argument, (b) detects the
fan-out over trace ids and the Slack search ladder, and (c) runs end to end on a ticket it never saw."""
import asyncio
from pathlib import Path

import pytest

from crystal.flow.runner import FlowRunner
from crystal.induce.inducer import Binder, induce
from crystal.extract.catalog import load_catalog
from crystal.mcp_client import ServerPool
from crystal.trace.store import load_sessions

TRACES = Path(__file__).resolve().parent.parent / "traces"


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


def _step(flow, sid):
    return next(s for s in flow["steps"] if s["id"] == sid)


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
