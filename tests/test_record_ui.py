"""The /record page: launching a recorded Claude Code session from the UI, following it, inducing a flow from it.

The driver is faked (crystal.trace.driver.run_agent is monkeypatched): it writes a trace the way the hook would,
pauses so the tests can observe a run in progress, then finishes with a cost. No agent is launched, nothing costs
money: the driver's own plumbing is tested through the `driver.launch` seam, and tests/conftest.py sets
CRYSTAL_NO_AGENT so the real driver refuses to spawn anything, whatever is mocked (the last test checks that)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from crystal.app import main as ui
from crystal.app import routes_record as rr
from crystal.trace import driver
from crystal.trace.record import Recorder
from crystal.trace.store import load_session

SEED_TRACE = "scripted-PAY-101-v0"   # a seeded sim session whose calls the fake replays


class FakeDriver:
    """Records meta + the first `pause_after` calls, blocks until released, then the rest and the result."""

    def __init__(self, trace_dir: Path, pause_after: int = 2, cost: float = 0.37, fail: bool = False):
        self.trace_dir, self.pause_after, self.cost, self.fail = trace_dir, pause_after, cost, fail
        self.started, self.release = threading.Event(), threading.Event()
        self.calls: list[dict] = []
        self.kwargs: dict = {}

    def __call__(self, trigger, inputs, prompt, budget="3", model=None, meta=None, session_id=None, on_start=None, **kw):
        self.kwargs = {"trigger": trigger, "inputs": inputs, "prompt": prompt, "budget": budget, "model": model, "meta": meta, **kw}
        sid = session_id
        rec = Recorder(sid, "claude-code", trace_dir=self.trace_dir,
                       meta={"trigger": trigger, "inputs": inputs, "prompt": prompt, "model": model or "default", **(meta or {})})
        rec.prompt(prompt)
        if on_start:
            on_start({"session_id": sid, "trace_path": str(rec.path), "pid": os.getpid(), "cmd": ["claude"]})
        src = load_session(self.trace_dir / f"{SEED_TRACE}.jsonl").calls
        for c in src[:self.pause_after]:
            rec.record(c["server"], c["tool"], c["input"], c["output"], c.get("output_text", ""))
        self.started.set()
        assert self.release.wait(10), "test never released the fake driver"
        if self.fail:
            raise RuntimeError("claude exploded")
        for c in src[self.pause_after:]:
            rec.record(c["server"], c["tool"], c["input"], c["output"], c.get("output_text", ""))
        rec.result("Summary: the incident was AuthTokenExpired in auth-service.")
        return {"session_id": sid, "cost_usd": self.cost, "num_turns": 7, "duration_ms": 1234, "is_error": False,
                "result": "Summary: the incident was AuthTokenExpired in auth-service.", "returncode": 0, "stderr": "",
                "trace_path": str(rec.path), "calls": [], "workspace": "sim", "mcp_config": {}}


@pytest.fixture
def client(fresh_home, monkeypatch):
    if hasattr(ui.app.state, "workspace"):
        monkeypatch.delattr(ui.app.state, "workspace")
    monkeypatch.setattr(driver, "claude_path", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(rr, "_THREADS", {})
    c = TestClient(ui.app)
    c.state = fresh_home
    return c


@pytest.fixture
def fake(client, monkeypatch):
    f = FakeDriver(client.state.traces)
    monkeypatch.setattr(driver, "run_agent", f)
    return f


FORM = {"question": "why did PAY-101 page us?", "trigger": "jira_issue", "budget": "2", "model": "", "confirm": "yes"}


def _job_of(response) -> str:
    assert response.status_code == 303, response.text
    loc = response.headers["location"]
    assert loc.startswith("/record/")
    return loc.rsplit("/", 1)[1]


def _finish(fake: FakeDriver, job: str) -> None:
    fake.release.set()
    t = rr._THREADS.get(job)
    if t:
        t.join(10)
    assert not (t and t.is_alive())


def test_form_page_and_checkbox_required(client, fake):
    r = client.get("/record")
    assert r.status_code == 200 and 'name="confirm"' in r.text and "costs money" in r.text
    assert 'value="prompt"' in r.text and 'value="2.0"' in r.text          # defaults: trigger, budget
    r = client.post("/record", data={**FORM, "confirm": ""}, follow_redirects=False)
    assert r.status_code == 400 and "tick the confirmation box" in r.text
    assert 'value="jira_issue"' in r.text and "why did PAY-101 page us?" in r.text   # the form keeps what was typed
    assert not list((client.state.dir / "records").glob("*.json")) and not fake.started.is_set()


def test_refuses_without_claude(client, fake, monkeypatch):
    monkeypatch.setattr(driver, "claude_path", lambda: None)
    r = client.get("/record")
    assert "not installed" in r.text and "npm install -g @anthropic-ai/claude-code" in r.text and "disabled" in r.text
    r = client.post("/record", data=FORM, follow_redirects=False)
    assert r.status_code == 409 and "not installed" in r.text and not fake.started.is_set()


def test_bad_budget_and_trigger(client, fake):
    assert client.post("/record", data={**FORM, "budget": "lots"}, follow_redirects=False).status_code == 400
    assert client.post("/record", data={**FORM, "budget": "0"}, follow_redirects=False).status_code == 400
    assert client.post("/record", data={**FORM, "trigger": "../x"}, follow_redirects=False).status_code == 400
    assert client.post("/record", data={**FORM, "question": "  "}, follow_redirects=False).status_code == 400
    assert not fake.started.is_set()


def test_start_follow_finish_and_induce(client, fake):
    r = client.post("/record", data=FORM, follow_redirects=False)
    job = _job_of(r)
    rec = json.loads((client.state.dir / "records" / f"{job}.json").read_text())
    assert rec["status"] == "running" and rec["trigger"] == "jira_issue" and rec["budget"] == 2.0 and rec["model"] is None
    assert rec["trace_path"].endswith(f"{rec['session_id']}.jsonl") and rec["prompt"].startswith("why did")   # no template takes {question}: verbatim
    assert fake.started.wait(10)
    assert fake.kwargs["budget"] == 2.0 and fake.kwargs["trigger"] == "jira_issue" and fake.kwargs["inputs"] == {"question": FORM["question"]}
    assert fake.kwargs["meta"]["command"] == "record-ui" and fake.kwargs["meta"]["job"] == job
    # in progress: the job page and the status endpoint tail the trace
    s = client.get(f"/record/{job}/status").json()
    assert s["status"] == "running" and s["done"] is False and s["n_calls"] == 2 and s["cost_usd"] is None
    assert [c["tool"] for c in s["calls"]] == ["jira_get_issue", "conversations_search_messages"]
    assert s["trace_url"] == f"/traces/{rec['session_id']}"
    page = client.get(f"/record/{job}").text
    assert "jira.jira_get_issue" in page and 'http-equiv="refresh"' in page and f"/record/{job}/status" in page
    assert "Induce a flow" not in page
    # done
    _finish(fake, job)
    s = client.get(f"/record/{job}/status").json()
    assert s["done"] and s["status"] == "done" and s["cost_usd"] == 0.37 and s["num_turns"] == 7 and s["n_calls"] > 2
    assert s["result"].startswith("Summary:")
    page = client.get(f"/record/{job}").text
    assert f'href="/traces/{rec["session_id"]}"' in page and "$0.3700" in page and "Summary: the incident" in page
    assert f'action="/record/{job}/induce"' in page and 'http-equiv="refresh"' not in page
    assert client.get(f"/traces/{rec['session_id']}").status_code == 200        # the dossier renders the new session
    assert (client.state.dir / "records" / ".lock").exists() is False
    # the list shows it with its cost
    lst = client.get("/record").text
    assert job in lst and "$0.3700" in lst and "why did PAY-101 page us?" in lst
    # induce: this session + the seeded jira_issue sessions -> jira-issue.v1.yaml, a draft
    r = client.post(f"/record/{job}/induce", follow_redirects=False)
    assert r.status_code == 303 and "Induced+jira-issue.v1" in r.headers["location"]
    p = client.state.flows / "jira-issue.v1.yaml"
    assert p.exists()
    import yaml
    flow = yaml.safe_load(p.read_text())
    assert flow["status"] == "draft" and flow["version"] == 1 and flow["base"] == "jira-issue"
    assert flow["authored"]["session"] == rec["session_id"] and flow["authored"]["command"] == "record-ui"
    assert rec["session_id"] in flow["induced_from"] and len(flow["induced_from"]) > 1
    page = client.get(f"/record/{job}").text
    assert 'href="/flows/jira-issue.v1"' in page and "Induce again" in page
    assert client.get("/flows/jira-issue.v1").status_code == 200
    # a second induce never overwrites: v2
    client.post(f"/record/{job}/induce", follow_redirects=False)
    assert (client.state.flows / "jira-issue.v2.yaml").exists() and p.read_text() == p.read_text()


def test_lock_blocks_a_second_run(client, fake):
    job = _job_of(client.post("/record", data=FORM, follow_redirects=False))
    assert fake.started.wait(10)
    r = client.post("/record", data={**FORM, "question": "another"}, follow_redirects=False)
    assert r.status_code == 409 and "already in progress" in r.text and job in r.text
    assert client.get("/record").text.count("A run is in progress") == 1
    assert len(list((client.state.dir / "records").glob("*.json"))) == 1
    _finish(fake, job)
    r = client.post("/record", data={**FORM, "question": "another"}, follow_redirects=False)
    assert r.status_code == 303   # the lock is released when the run ends
    _finish(fake, _job_of(r))


def test_driver_failure_is_an_error_job_and_releases_the_lock(client, monkeypatch):
    f = FakeDriver(client.state.traces, fail=True)
    monkeypatch.setattr(driver, "run_agent", f)
    job = _job_of(client.post("/record", data=FORM, follow_redirects=False))
    assert f.started.wait(10)
    _finish(f, job)
    s = client.get(f"/record/{job}/status").json()
    assert s["status"] == "error" and s["done"] and "claude exploded" in s["error"] and s["n_calls"] == 2
    assert "claude exploded" in client.get(f"/record/{job}").text
    assert not (client.state.dir / "records" / ".lock").exists()


def test_history_survives_a_restart_and_stale_locks_recover(client, fake):
    """A job left `running` by a server that died is shown as interrupted, and its lock no longer blocks."""
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    dead_pid = int(dead.stdout)
    d = client.state.dir / "records"
    d.mkdir(exist_ok=True)
    (d / "20260101T000000-old001.json").write_text(json.dumps({"job": "20260101T000000-old001", "session_id": "s-old", "trigger": "prompt",
                                                              "question": "old", "prompt": "old", "budget": 1, "model": None, "status": "running",
                                                              "started": "2026-01-01T00:00:00+00:00", "finished": None, "server_pid": dead_pid,
                                                              "trace_path": str(client.state.traces / "s-old.jsonl"), "cost_usd": None}))
    (d / ".lock").write_text(json.dumps({"job": "20260101T000000-old001", "server_pid": dead_pid}))
    lst = client.get("/record").text
    assert "interrupted" in lst and "A run is in progress" not in lst
    job = _job_of(client.post("/record", data=FORM, follow_redirects=False))
    assert fake.started.wait(10)
    _finish(fake, job)
    assert client.get("/record/20260101T000000-old001/status").json()["status"] == "interrupted"
    assert client.get("/record/nope/status").status_code == 404 and client.get("/record/nope").status_code == 404


def test_build_prompt_uses_the_trigger_template_when_it_fits():
    assert rr.build_prompt("prompt", "where is the retry loop?") == "where is the retry loop?"
    assert "where is the retry loop?" in rr.build_prompt("codebase", "where is the retry loop?") and "new to this repository" in rr.build_prompt("codebase", "x")
    assert rr.build_prompt("jira_issue", "PAY-1") == "PAY-1"     # its template needs {key}: verbatim question


def test_driver_on_start_and_budget_flag(fresh_home, monkeypatch):
    """run_agent with the spawn seam patched: the on_start callback gets the session id, trace path and pid before
    the process ends; the budget travels as --max-budget-usd; the final message is kept without a Stop hook."""
    seen, launched = {}, {}

    def fake_launch(cmd, cwd, env, on_start=None):
        launched.update(cmd=cmd, cwd=cwd)
        if on_start:
            on_start({"pid": 999, "cmd": cmd})
        return {"returncode": 0, "stdout": json.dumps({"result": "done: nothing to see", "total_cost_usd": 0.012, "num_turns": 1, "duration_ms": 5}),
                "stderr": "", "pid": 999}
    monkeypatch.setattr(driver, "launch", fake_launch)
    monkeypatch.delenv("CRYSTAL_NO_AGENT")
    info = driver.run_agent("prompt", {"question": "q"}, "q", budget=2.0, quiet=True, on_start=seen.update)
    assert seen["session_id"] == info["session_id"] and seen["trace_path"] == info["trace_path"] and seen["pid"] == 999
    assert Path(info["trace_path"]).exists() and info["cost_usd"] == 0.012 and info["num_turns"] == 1 and not info["is_error"]
    cmd = launched["cmd"]
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--max-budget-usd") + 1] == "2.0" and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--session-id") + 1] == info["session_id"]
    sess = load_session(Path(info["trace_path"]))
    assert sess.result == "done: nothing to see" and sess.meta["trigger"] == "prompt"


def test_run_agent_refuses_under_crystal_no_agent(fresh_home, monkeypatch):
    """The suite-wide guard: with CRYSTAL_NO_AGENT set (tests/conftest.py) neither run_agent nor launch spawns
    anything, whatever else is mocked."""
    assert os.environ.get("CRYSTAL_NO_AGENT")
    spawned = []
    monkeypatch.setattr(driver.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    before = set(fresh_home.traces.glob("*.jsonl"))
    with pytest.raises(RuntimeError, match="agent launches disabled"):
        driver.run_agent("prompt", {"question": "q"}, "q", budget=1, quiet=True)
    with pytest.raises(RuntimeError, match="agent launches disabled"):
        driver.launch(["claude", "-p", "q"], fresh_home.root, {})
    assert not spawned and set(fresh_home.traces.glob("*.jsonl")) == before    # nothing spawned, no trace written


def test_ui_launch_with_the_real_driver_is_refused_by_the_guard(client):
    """A POST /record that reaches the unpatched driver ends as an error job under CRYSTAL_NO_AGENT: the UI path
    cannot spend money in the suite either."""
    job = _job_of(client.post("/record", data=FORM, follow_redirects=False))
    t = rr._THREADS.get(job)
    if t:
        t.join(10)
    s = client.get(f"/record/{job}/status").json()
    assert s["status"] == "error" and "agent launches disabled" in s["error"] and s["n_calls"] == 0
    assert not (client.state.dir / "records" / ".lock").exists()
