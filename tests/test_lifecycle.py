"""Promotion lifecycle + circuit breaker: transitions, demotion signals, re-promotion, and the runner/regression hooks."""
import asyncio
import json

import pytest

from crystal.flow import runner as runner_mod
from crystal.flow.lifecycle import Lifecycle, classify_run, describe
from crystal.flow.runner import FlowRunner
from crystal.replay.cassette import Cassette
from crystal.replay.regression import regression

FLOW = {"name": "f", "status": "promoted", "trigger": {"type": "t"}, "inputs": {"key": {"type": "jira_key", "required": True, "example": "PAY-101"}},
        "steps": [{"id": "issue", "tool": "jira.jira_get_issue", "required": True, "args": {"issue_key": "{{ inputs.key }}"}},
                  {"id": "slack", "tool": "slack.conversations_search_messages", "hits": "messages.matches", "args": {"search_query": "{{ inputs.key }}"}}]}


def rec(run_id="r1", status="ok", steps=None):
    return {"run_id": run_id, "flow": "f", "status": status,
            "steps": steps if steps is not None else [{"id": "issue", "hits": 1}, {"id": "slack", "hits": 2}]}


@pytest.fixture
def lc(tmp_path):
    return Lifecycle(tmp_path / "lc.sqlite")


def test_classify_run():
    assert classify_run(rec(), FLOW) is None
    assert "status failed" in classify_run(rec(status="failed"), FLOW)
    assert classify_run(rec(steps=[{"id": "issue", "hits": 1}, {"id": "slack", "hits": 0, "error": "boom"}]), FLOW).startswith("step slack")
    assert "required step issue" in classify_run(rec(steps=[{"id": "issue", "hits": 0}, {"id": "slack", "hits": 3}]), FLOW)
    assert classify_run(rec(steps=[{"id": "issue", "hits": 1}, {"id": "slack", "hits": 0}]), FLOW) is None   # optional step, zero hits is fine
    assert classify_run(rec(steps=[{"id": "issue", "skipped": "when=false"}]), FLOW) is None


def test_first_sight_mirrors_author_status(lc):
    st = lc.sync(FLOW)
    assert (st["author_status"], st["status"], st["clean_runs"], st["failed_runs"]) == ("promoted", "promoted", 0, 0)
    assert describe(st) == {"status": "promoted", "tripped": False, "hint": ""}


def test_step_error_demotes_one_level_and_clean_runs_repromote(lc):
    st = lc.record_run(rec(steps=[{"id": "issue", "hits": 1}, {"id": "slack", "hits": 0, "error": "timeout"}]), FLOW)
    assert st["status"] == "candidate" and st["transition"] == ("promoted", "candidate") and st["outcome"] == "failure"
    assert st["failed_runs"] == 1 and st["last_failure"].startswith("step slack") and st["last_failure_run"] == "r1"
    assert describe(st)["tripped"] and "0/5" in describe(st)["hint"]
    for i in range(4):
        st = lc.record_run(rec(run_id=f"c{i}"), FLOW)
        assert st["status"] == "candidate" and st["clean_streak"] == i + 1
    st = lc.record_run(rec(run_id="c5"), FLOW)
    assert st["status"] == "promoted" and st["transition"] == ("candidate", "promoted")
    assert st["clean_streak"] == 0 and st["clean_runs"] == 5 and st["total_runs"] == 6
    kinds = [e["kind"] for e in lc.events("f")]
    assert kinds.count("transition") == 2


def test_required_zero_hits_demotes_and_failure_resets_streak(lc):
    lc.record_run(rec(), FLOW)
    lc.record_run(rec(), FLOW)
    assert lc.get("f")["clean_streak"] == 2
    st = lc.record_run(rec(steps=[{"id": "issue", "hits": 0}, {"id": "slack", "hits": 1}]), FLOW)
    assert st["status"] == "candidate" and st["clean_streak"] == 0 and "required step issue" in st["reason"]


def test_two_failures_reach_draft_and_never_below(lc):
    bad = rec(status="failed")
    assert lc.record_run(bad, FLOW)["status"] == "candidate"
    assert lc.record_run(bad, FLOW)["status"] == "draft"
    st = lc.record_run(bad, FLOW)
    assert st["status"] == "draft" and st["transition"] is None and st["failed_runs"] == 3


def test_repromotion_ladder_draft_to_candidate_uses_candidate_after(lc):
    flow = {**FLOW, "candidate_after": 3, "promote_after": 2}
    for _ in range(2):
        lc.record_run(rec(status="failed"), flow)
    assert lc.get("f")["status"] == "draft"
    for i in range(2):
        assert lc.record_run(rec(run_id=f"a{i}"), flow)["status"] == "draft"
    assert lc.record_run(rec(run_id="a2"), flow)["status"] == "candidate"
    lc.record_run(rec(run_id="b0"), flow)
    assert lc.record_run(rec(run_id="b1"), flow)["status"] == "promoted"


def test_effective_state_never_exceeds_author_intent(lc):
    flow = {**FLOW, "status": "candidate"}
    for i in range(12):
        st = lc.record_run(rec(run_id=f"x{i}"), flow)
    assert st["status"] == "candidate" and st["clean_streak"] == 12 and not describe(st)["tripped"]


def test_author_status_change_resets_effective_state(lc):
    lc.record_run(rec(status="failed"), FLOW)
    assert lc.get("f")["status"] == "candidate"
    st = lc.sync({**FLOW, "status": "draft"})            # author demotes by hand
    assert (st["author_status"], st["status"], st["clean_streak"]) == ("draft", "draft", 0)
    st = lc.sync({**FLOW, "status": "promoted"})         # author promotes a new version
    assert st["status"] == "promoted" and st["failed_runs"] == 1  # counters are history, kept


def test_feedback_counts_as_failure(lc):
    st = lc.record_feedback(FLOW, "r9", "missing the deploy diff")
    assert st["status"] == "candidate" and st["complaints"] == 1 and st["reason"] == "user: missing the deploy diff"
    assert st["failed_runs"] == 0 and st["total_runs"] == 0   # not a run
    assert st["last_failure_run"] == "r9"


def test_failed_regression_test_demotes(lc):
    st = lc.record_test(FLOW, False, "slack: 0 hits < 1")
    assert st["status"] == "candidate" and st["tests_failed"] == 1 and st["transition"] == ("promoted", "candidate")
    st = lc.record_test(FLOW, True)
    assert st["tests_passed"] == 1 and st["status"] == "candidate" and json.loads(st["last_test"])["passed"] is True


class FakePool:
    """Answers from a dict of (server, tool) -> result, or raises."""

    def __init__(self, answers):
        self.answers = answers
        self.registry = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def call_raw(self, server, tool, args=None):
        a = self.answers[(server, tool)]
        if isinstance(a, Exception):
            raise a
        return a, json.dumps(a), False


OK_ANSWERS = {("jira", "jira_get_issue"): {"key": "PAY-101", "fields": {"summary": "x"}},
              ("slack", "conversations_search_messages"): {"messages": {"matches": [{"text": "hi"}]}}}


def test_runner_records_saved_runs_only(lc, tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "RUN_DIR", tmp_path / "runs")

    async def go(pool, save):
        return await FlowRunner(pool, catalog={}, lifecycle=lc).run(FLOW, {"key": "PAY-101"}, save=save)
    r = asyncio.run(go(FakePool(OK_ANSWERS), save=False))
    assert "lifecycle" not in r and lc.get("f") is None
    r = asyncio.run(go(FakePool(OK_ANSWERS), save=True))
    assert r["lifecycle"] == {"outcome": "clean", "reason": None, "status": "promoted", "author_status": "promoted", "transition": None}
    assert lc.get("f")["clean_runs"] == 1
    saved = json.loads((tmp_path / "runs" / f"{r['run_id']}.json").read_text())
    assert saved["lifecycle"]["outcome"] == "clean"
    r = asyncio.run(go(FakePool({**OK_ANSWERS, ("slack", "conversations_search_messages"): RuntimeError("slack down")}), save=True))
    assert r["lifecycle"]["outcome"] == "failure" and r["lifecycle"]["transition"] == ("promoted", "candidate")
    assert "slack down" in lc.get("f")["last_failure"]


def test_regression_offline_records_pass_and_fail(lc, tmp_path):
    cdir = tmp_path / "cassettes"
    c = Cassette()
    for (server, tool), out in OK_ANSWERS.items():
        args = {"issue_key": "PAY-101"} if tool == "jira_get_issue" else {"search_query": "PAY-101"}
        c.put(server, tool, args, out, json.dumps(out))
    c.save(cdir / "f.json")
    rep = regression(FLOW, mode="offline", cassette_dir=cdir, lifecycle=lc)
    assert rep["passed"] and rep["cases"][0]["summary"] == {"issue": {"hits": 1, "error": None, "skipped": None}, "slack": {"hits": 1, "error": None, "skipped": None}}
    assert lc.get("f")["tests_passed"] == 1 and rep["lifecycle"]["status"] == "promoted"
    strict = {**FLOW, "tests": [{"inputs": {"key": "PAY-101"}, "expect": {"slack": {"min_hits": 5}}}]}
    rep = regression(strict, mode="offline", cassette_dir=cdir, lifecycle=lc)
    assert not rep["passed"] and "1 hits < 5" in rep["cases"][0]["reason"]
    assert rep["lifecycle"]["transition"] == ("promoted", "candidate")
    missing = {**FLOW, "tests": [{"inputs": {"key": "PAY-999"}}]}
    rep = regression(missing, mode="offline", cassette_dir=cdir, lifecycle=lc)
    assert not rep["passed"] and rep["cases"][0]["reason"] == "run status failed" and rep["misses"] == 1  # required step errored on the miss
    assert lc.get("f")["status"] == "draft" and lc.get("f")["tests_failed"] == 2


def test_ui_feedback_demotes_and_queues(tmp_path, monkeypatch):
    """The 'This didn't help' endpoint appends to feedback.jsonl and trips the breaker for the run's flow."""
    import app.main as web
    from crystal.flow import lifecycle as lc_mod
    monkeypatch.setenv("CRYSTAL_LIFECYCLE_DB", str(tmp_path / "lc.sqlite"))
    monkeypatch.setattr(lc_mod, "_default", None)
    monkeypatch.setattr(web, "RUN_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "FEEDBACK", tmp_path / "feedback.jsonl")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "r1.json").write_text(json.dumps({"run_id": "r1", "flow": "investigate-jira-ticket", "inputs": {"key": "PAY-101"}, "steps": []}))
    resp = asyncio.run(web.feedback("r1", text="missing the deploy diff", helpful="no"))
    assert resp.status_code == 303 and "demoted+candidate" in resp.headers["location"]
    st = lc_mod.get_lifecycle().get("investigate-jira-ticket")
    assert st["status"] == "draft" and st["author_status"] == "candidate" and st["complaints"] == 1
    assert st["last_failure"] == "user: missing the deploy diff" and st["last_failure_run"] == "r1"
    line = json.loads((tmp_path / "feedback.jsonl").read_text().splitlines()[-1])
    assert line["kind"] == "feedback" and line["helpful"] is False and line["flow"] == "investigate-jira-ticket"
    resp = asyncio.run(web.feedback("r1", text="", helpful="yes"))
    assert "Thanks" in resp.headers["location"] and lc_mod.get_lifecycle().get("investigate-jira-ticket")["complaints"] == 1


# ---------------------------------------------------------------- review fixes
def test_passing_regression_tests_never_repromote(lc):
    """A passing test replays the same recorded responses every time: it is recorded but is not live evidence,
    so a tripped flow climbs back only on clean live runs."""
    lc.record_run(rec(status="failed"), FLOW)
    assert lc.get("f")["status"] == "candidate"
    for _ in range(6):
        st = lc.record_test(FLOW, True)
    assert st["status"] == "candidate" and st["transition"] is None and st["tests_passed"] == 6
    assert st["clean_streak"] == 0 and st["clean_runs"] == 0 and st["total_runs"] == 1
    assert "clean live runs" in describe(st)["hint"]
    for i in range(5):
        st = lc.record_run(rec(run_id=f"c{i}"), FLOW)
    assert st["status"] == "promoted" and st["transition"] == ("candidate", "promoted")
    assert st["clean_runs"] == 5 and lc.get("f")["tests_passed"] == 6


def test_regression_without_cases_leaves_lifecycle_untouched(lc, tmp_path):
    no_example = {**FLOW, "inputs": {"key": {"type": "jira_key", "required": True}}}
    rep = regression(no_example, mode="offline", cassette_dir=tmp_path, lifecycle=lc)
    assert not rep["passed"] and rep["cases"] == [] and "no test cases" in rep["error"] and "lifecycle" not in rep
    assert lc.get("f") is None


def test_regression_fails_when_every_step_returns_nothing(lc, tmp_path):
    """No `tests:`, no `required:`: the default expectation is still that the flow found something."""
    optional = {**FLOW, "steps": [{**FLOW["steps"][0], "required": False}, FLOW["steps"][1]]}
    empty = {("jira", "jira_get_issue"): {}, ("slack", "conversations_search_messages"): {"messages": {"matches": []}}}
    rep = regression(optional, mode="live", cassette_dir=tmp_path, lifecycle=lc, live_pool_factory=lambda: FakePool(empty))
    assert not rep["passed"] and rep["cases"][0]["reason"] == "every step returned zero hits"
    assert lc.get("f")["status"] == "candidate" and lc.get("f")["tests_failed"] == 1
    rep = regression(optional, mode="live", cassette_dir=tmp_path / "b", lifecycle=lc, live_pool_factory=lambda: FakePool(OK_ANSWERS))
    assert rep["passed"]


def test_runner_saves_the_run_when_the_lifecycle_store_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "RUN_DIR", tmp_path / "runs")

    class Locked:
        def record_run(self, record, flow):
            raise RuntimeError("database is locked")
    r = asyncio.run(FlowRunner(FakePool(OK_ANSWERS), catalog={}, lifecycle=Locked()).run(FLOW, {"key": "PAY-101"}, save=True))
    assert r["lifecycle"] == {"error": "RuntimeError: database is locked"} and r["status"] == "ok"
    saved = json.loads((tmp_path / "runs" / f"{r['run_id']}.json").read_text())
    assert saved["steps"][0]["hits"] == 1


def test_flows_server_run_flow_is_not_a_live_run(tmp_path, monkeypatch):
    """The authoring agent's run_flow saves a run record (the expansion needs it) but never feeds the breaker:
    an exploratory run with made-up inputs is neither evidence for nor against the flow."""
    import sys
    from crystal.flow import lifecycle as lc_mod
    sys.path.insert(0, str(runner_mod.PROJECT_ROOT / "sim" / "servers"))
    flows_srv = __import__("flows")
    monkeypatch.setenv("CRYSTAL_LIFECYCLE_DB", str(tmp_path / "lc.sqlite"))
    monkeypatch.setattr(lc_mod, "_default", None)
    monkeypatch.setattr(runner_mod, "RUN_DIR", tmp_path / "runs")
    monkeypatch.setattr(flows_srv, "load_flow", lambda name: dict(FLOW))
    monkeypatch.setattr(flows_srv, "load_registry", lambda: {"jira": {}, "slack": {}, "flows": {}})
    bad = {**OK_ANSWERS, ("slack", "conversations_search_messages"): RuntimeError("slack down")}
    monkeypatch.setattr(flows_srv, "ServerPool", lambda registry: FakePool(bad))
    out = json.loads(asyncio.run(flows_srv.run_flow_tool("f", '{"key": "PAY-101"}')))
    assert out["flow"] == "f" and out["steps"][1]["error"].startswith("slack down") and out["lifecycle"] is None
    assert (tmp_path / "runs" / f"{out['run_id']}.json").exists()
    assert lc_mod.get_lifecycle().get("f") is None


def test_ui_feedback_keeps_author_status_when_yaml_is_unreadable(tmp_path, monkeypatch):
    """A complaint filed while the flow YAML is mid-edit must not re-declare the flow a draft."""
    import app.main as web
    from crystal.flow import lifecycle as lc_mod
    monkeypatch.setenv("CRYSTAL_LIFECYCLE_DB", str(tmp_path / "lc.sqlite"))
    monkeypatch.setattr(lc_mod, "_default", None)
    monkeypatch.setattr(web, "RUN_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "FEEDBACK", tmp_path / "feedback.jsonl")
    lc_mod.get_lifecycle().sync({"name": "no-such-flow", "status": "promoted"})
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "r1.json").write_text(json.dumps({"run_id": "r1", "flow": "no-such-flow", "inputs": {}, "steps": []}))
    asyncio.run(web.feedback("r1", text="nope", helpful="no"))
    st = lc_mod.get_lifecycle().get("no-such-flow")
    assert st["author_status"] == "promoted" and st["status"] == "candidate" and st["complaints"] == 1
