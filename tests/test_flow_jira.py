"""Regression tests for the investigate-jira-ticket flow: live against the deterministic sim, then via cassette."""
import asyncio
from pathlib import Path

import pytest

from crystal.flow.runner import FlowRunner, load_flow
from crystal.mcp_client import ServerPool
from crystal.replay.cassette import Cassette, CassettePool


def _run(pool_factory, key="PAY-101"):
    async def go():
        async with pool_factory() as pool:
            return await FlowRunner(pool).run(load_flow("investigate-jira-ticket"), {"key": key}, save=False)
    return asyncio.run(go())


def _step(rec, sid):
    return next(s for s in rec["steps"] if s["id"] == sid)


@pytest.fixture(scope="module")
def live_record():
    return _run(ServerPool)


def test_flow_completes(live_record):
    assert live_record["status"] == "ok"
    assert [s["id"] for s in live_record["steps"]][:3] == ["issue", "slack", "thread"]
    assert not any(s.get("error") for s in live_record["steps"]), [s.get("error") for s in live_record["steps"]]


def test_issue_extracts(live_record):
    ex = _step(live_record, "issue")["extracts"]
    assert ex["service"] == "payments-api"
    assert ex["error_class"] == "PaymentGatewayTimeout"
    assert ex["error_sig"].startswith("PaymentGatewayTimeout: gateway")
    assert len(ex["trace_ids"]) == 1 and len(ex["trace_ids"][0]) == 32
    assert ex["window"]["start"] < ex["created"] < ex["window"]["end"]


def test_slack_finds_exactly_the_incident_thread(live_record):
    s = _step(live_record, "slack")
    assert s["hits"] == 1 and s["attempts"][0]["hits"] == 1, s["attempts"]
    assert "after:2026-08-06" in s["args"]["search_query"]
    thread = _step(live_record, "thread")
    assert len(thread["items"]) == 1 and thread["items"][0]["hits"] == 5


def test_fanout_and_evidence(live_record):
    logs = _step(live_record, "logs")
    assert len(logs["items"]) >= 2 and sum(i["hits"] for i in logs["items"]) >= 8
    assert _step(live_record, "code")["hits"] >= 1
    assert _step(live_record, "owners")["items"][0]["extracts"]["teams"] == ["@payments"]
    assert _step(live_record, "confluence")["hits"] == 1
    assert _step(live_record, "metrics")["hits"] == 1
    assert _step(live_record, "pagerduty")["hits"] == 1
    commits = _step(live_record, "commits")["extracts"]["shas"]
    suspect = _step(live_record, "thread")["items"][0]["extracts"]["shas"]
    assert any(c.startswith(s) for c in commits for s in suspect), (commits, suspect)


def test_cassette_replay_matches_live(tmp_path, live_record):
    cpath = tmp_path / "jira.json"

    async def record_pass():
        async with CassettePool(Cassette(), live=ServerPool(), mode="record") as pool:
            r = await FlowRunner(pool).run(load_flow("investigate-jira-ticket"), {"key": "PAY-101"}, save=False)
            pool.cassette.save(cpath)
            return r
    recorded = asyncio.run(record_pass())
    async def replay_pass():
        async with CassettePool(Cassette(cpath), live=None, mode="replay") as pool:
            return await FlowRunner(pool).run(load_flow("investigate-jira-ticket"), {"key": "PAY-101"}, save=False)
    replayed = asyncio.run(replay_pass())
    assert replayed["summary"] == recorded["summary"] == live_record["summary"]
    assert [s.get("extracts") for s in replayed["steps"]] == [s.get("extracts") for s in recorded["steps"]]


def test_other_service_ticket():
    rec = _run(ServerPool, key="STF-109")
    ex = _step(rec, "issue")["extracts"]
    assert ex["service"] == "checkout-web"
    assert _step(rec, "slack")["hits"] == 1
    assert _step(rec, "owners")["items"][0]["extracts"]["teams"] == ["@storefront"]


def test_plain_text_error_results_count_as_errors_not_hits():
    """A tool that answers 'error: ...' as text (git_show on a pod hash) must not score as a hit."""
    rec = _run(ServerPool, key="PAY-101")
    sc = _step(rec, "suspect_commit")
    bad = [i for i in sc["items"] if str(i.get("result", "")).lower().startswith("error")]
    assert all(i["hits"] == 0 and i["error"] for i in bad), bad
