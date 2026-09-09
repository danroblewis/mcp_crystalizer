"""The Slack-thread and Slack-DM flows must run end to end on inputs that were never traced and gather the
evidence: the thread, the ticket, logs per trace id, the metric, the runbook, with no step errors.

Traced: threads/DMs of INC-001..006 (scripted), INC-006 thread and INC-008 DM (Claude Code). Used here: INC-007's
thread (SUP-105), INC-009's service-only DM (PAY-108) and INC-010's key-only DM (STF-119)."""
import asyncio

import pytest

from crystal.flow.runner import FlowRunner, load_flow
from crystal.mcp_client import ServerPool


def _run(flow: str, inputs: dict) -> dict:
    async def go():
        async with ServerPool() as pool:
            return await FlowRunner(pool).run(load_flow(flow), inputs, save=False)
    return asyncio.run(go())


def _steps(rec: dict) -> dict:
    return {s["id"]: s for s in rec["steps"]}


def _assert_clean(rec: dict) -> None:
    assert rec["status"] == "ok"
    assert not any(s.get("error") for s in rec["steps"]), [(s["id"], s.get("error")) for s in rec["steps"] if s.get("error")]


@pytest.fixture(scope="module")
def thread_run():
    return _run("investigate-slack-thread", {"channel_id": "C542575C5", "thread_ts": "1787837940.000006"})  # SUP-105, never traced


def test_thread_flow_evidence(thread_run):
    _assert_clean(thread_run)
    s = _steps(thread_run)
    th = s["thread"]["extracts"]
    assert s["thread"]["hits"] == 5
    assert th["service"] == "inventory-sync" and th["error_class"] == "UpstreamSchemaDrift"
    assert th["jira_keys"] == ["SUP-105"] and len(th["trace_ids"]) == 2
    assert s["issues"].get("skipped") == "when=false"          # the thread named the ticket, no search needed
    assert s["issue"]["hits"] == 1 and s["issue"]["extracts"]["summary"].startswith("inventory-sync:")
    assert s["issue"]["extracts"]["error_sig"].startswith("UpstreamSchemaDrift: unexpected field")
    assert s["runbook"]["result"]["title"] == "inventory-sync runbook" and s["runbook"]["extracts"]["remediation"]
    assert s["metrics"]["hits"] == 1
    logs = s["logs"]["items"]
    assert len(logs) == 2 and all(i["hits"] == 8 for i in logs), [(i["item"], i["hits"]) for i in logs]
    assert s["logs_by_error"]["hits"] == 24 and s["logs_by_error"]["attempts"][0]["hits"] == 24
    assert s["pagerduty"]["hits"] == 1 and s["code"]["hits"] >= 1 and s["commits"]["hits"] == 1
    assert th["window"]["start"] < s["issue"]["extracts"]["created"] < th["window"]["end"]


def test_dm_flow_service_only():
    rec = _run("investigate-slack-dm", {"text": "is payments healthy? a customer says card charges are hanging"})  # PAY-108
    _assert_clean(rec)
    s = _steps(rec)
    dm = s["dm"]["extracts"]
    assert s["dm"]["hits"] == 1 and dm["sender"] == "dan" and dm["channel_id"].startswith("D")
    assert dm["service"] == "payments-api" and dm["error_class"] is None and dm["jira_key"] is None
    inc = s["incidents"]
    assert inc["hits"] >= 1 and "payments-api in:#incidents after:" in inc["args"]["search_query"]
    assert [a for a in inc["attempts"] if a.get("skipped")], "rungs needing a key/error class must blank out"
    th = s["thread"]["extracts"]
    assert s["thread"]["hits"] == 5 and th["jira_keys"] == ["PAY-108"] and th["error_class"] == "PaymentGatewayTimeout"
    assert s["runbook"]["result"]["title"] == "payments-api runbook"
    assert s["owners"]["items"][0]["extracts"]["teams"] == ["@payments"]
    assert s["metrics"]["hits"] == 1
    assert sum(i["hits"] for i in s["logs"]["items"]) >= 16
    assert s["logs_by_error"]["hits"] == 24
    assert s["issues"].get("skipped") == "when=false"
    assert s["issue"]["hits"] == 1 and s["issue"]["result"]["key"] == "PAY-108"


def test_dm_flow_key_only():
    rec = _run("investigate-slack-dm", {"text": "hey, are you on STF-119? support is getting pinged about it and I have nothing to tell them"})
    _assert_clean(rec)
    s = _steps(rec)
    assert s["dm"]["extracts"]["jira_key"] == "STF-119" and s["dm"]["extracts"]["service"] is None
    assert s["incidents"]["args"]["search_query"] == "STF-119 in:#incidents"
    assert s["thread"]["hits"] == 5 and s["thread"]["extracts"]["service"] == "checkout-web"
    assert s["code"]["args"]["pattern"] == "SessionExpired"
    assert s["runbook"]["result"]["title"] == "checkout-web runbook"
    assert s["metrics"]["hits"] == 1 and s["logs_by_error"]["hits"] == 24
    assert len(s["logs"]["items"]) == 2 and all(i["hits"] == 8 for i in s["logs"]["items"])
    assert s["issue"]["result"]["key"] == "STF-119" and s["issue"]["attempts"][0]["hits"] == 1


def test_dm_not_in_slack_stops_cleanly():
    rec = _run("investigate-slack-dm", {"text": "this text was never sent to anyone"})
    s = _steps(rec)
    assert rec["status"] == "ok" and s["dm"]["hits"] == 0
    assert s["history"].get("skipped") and s["thread"].get("skipped") and s["issue"].get("skipped")
    assert all(a.get("skipped") == "blank" for a in s["incidents"]["attempts"]), s["incidents"]["attempts"]
