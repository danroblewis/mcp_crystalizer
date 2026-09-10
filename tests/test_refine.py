"""`crystal refine` with a FAKE driver: no agent is ever launched, nothing costs money.

The point of the feature is that the agent only PROPOSES. These tests pin the deterministic half: the context is
gathered from the traces, and every proposal is decided here -- a binding is replayed against the recorded episodes
and kept only when it reproduces, exactly, what each episode really sent.

Three synthetic episodes are recorded (the way the hook would), induced into a draft, and each test hands the
refiner a different proposal through `driver_fn`.
"""
import json
import shutil

import pytest
import yaml

from crystal import refine
from crystal.flow.runner import load_flow
from crystal.induce.inducer import dump_flow, induce
from crystal.trace.record import Recorder
from crystal.trace.store import load_sessions

from tests.conftest import SIM

DRAFT = "mined-jira-code"

# (session id, key, grep pattern, confluence query). `pattern` is different in every episode and appears nowhere in
# an earlier result, so the inducer cannot derive it: it is the unresolved argument refine exists to fix. `query` is
# the ticket key in two episodes and something else in the third: a template for it must be REJECTED.
EPISODES = [
    ("ep-alpha", "PAY-101", "AlphaTimeout", "PAY-101"),
    ("ep-beta", "PAY-102", "BetaTimeout", "PAY-102"),
    ("ep-gamma", "PAY-103", "GammaTimeout", "runbook rollback"),
]


def _write_episode(tdir, sid, key, pattern, query):
    rec = Recorder(sid, "scripted", trace_dir=tdir,
                   meta={"trigger": "jira_issue", "inputs": {"key": key},
                         "prompt": f"You are on call. Investigate Jira ticket {key} and find the code that raises it."})
    issue = {"key": key, "id": "16434", "fields": {"summary": f"payments-api: 5xx on {key}", "status": {"name": "Done"},
                                                   "components": [{"name": "payments-api"}],
                                                   "description": "Error: gateway timeout\nSeen in production.",
                                                   "created": "2026-04-01T10:00:00Z"}}
    rec.record("jira", "jira_get_issue", {"issue_key": key}, issue, json.dumps(issue))
    grep = {"matches": [{"file": "services/payments-api/handler.py", "line": 12, "text": pattern}]}
    rec.record("code", "grep", {"pattern": pattern, "glob": "**/*.py", "context": 2}, grep, json.dumps(grep))
    page = {"results": [{"id": "20014", "title": "payments runbook"}]}
    rec.record("confluence", "confluence_search", {"query": query, "limit": 5}, page, json.dumps(page))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A private state dir for the sim workspace holding three synthetic episodes and the draft induced from them."""
    from crystal import state as state_mod
    from crystal.flow import lifecycle as lc_mod
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))
    monkeypatch.delenv("CRYSTAL_LIFECYCLE_DB", raising=False)
    monkeypatch.setattr(lc_mod, "_default", None)
    st = state_mod.StateDir(root=SIM, dir=tmp_path / "home" / "workspaces" / state_mod.slug_of(SIM))
    st.dir.mkdir(parents=True)
    for d in (st.traces, st.flows, st.runs):
        d.mkdir()
    st.workspace_json.write_text("{}")
    (st.dir / state_mod.SEED_MARKER).write_text("")
    shutil.copy(SIM / "catalog.yaml", st.catalog)
    for args in EPISODES:
        _write_episode(st.traces, *args)
    sessions = sorted(load_sessions(st.traces), key=lambda s: [e[0] for e in EPISODES].index(s.session_id))
    flow, report = induce(sessions, DRAFT)
    (st.flows / f"{DRAFT}.yaml").write_text(dump_flow(flow))
    return {"state": st, "traces": st.traces, "flows": st.flows, "flow": flow, "report": report}


def _flow(world):
    return load_flow(DRAFT, world["flows"])


def fake_driver(block, cost=0.31):
    """A driver_fn that never launches anything: it just returns `block` as the agent's final message."""
    def driver_fn(trigger, inputs, prompt, budget=None, model=None, meta=None, **kw):
        assert trigger == "refine" and "PROPOSE" in prompt
        driver_fn.prompt = prompt
        return {"session_id": "fake-refine", "cost_usd": cost, "is_error": False, "num_turns": 2,
                "result": "Here is what I would do.\n\n```yaml\n" + block.strip() + "\n```\n", "calls": [],
                "trace_path": "", "returncode": 0, "stderr": ""}
    return driver_fn


def run(world, block, **kw):
    return refine.refine(DRAFT, driver_fn=fake_driver(block), flow_dir=world["flows"], trace_dir=world["traces"],
                         schemas=False, **kw)


def verdicts(report, kind="bindings"):
    return {(b["step"], b["arg"]): b for b in report[kind]}


# ---------------------------------------------------------------- the draft the tests build on
def test_the_draft_really_has_an_unresolved_argument(world):
    flow = world["flow"]
    code = next(s for s in flow["steps"] if s["tool"] == "code.grep")
    assert "pattern" in (code.get("unresolved") or {}), "the fixture must produce an unresolved argument"
    assert refine.flow_bindability(flow) < 1.0


# ---------------------------------------------------------------- 1. context gathering
def test_context_picks_the_prompts_and_the_unresolved_values(world):
    ctx = refine.gather(_flow(world), trace_dir=world["traces"], schemas=False)
    assert ctx["episodes"] == [e[0] for e in EPISODES] and not ctx["missing_episodes"]
    assert len(ctx["prompts"]) == 1, "three prompts differing only in the ticket key fold into one"
    assert ctx["prompts"][0]["episodes"] == [e[0] for e in EPISODES]

    unres = {(u["step"], u["arg"]): u for u in ctx["unresolved"]}
    ((step, arg), u), = [(k, v) for k, v in unres.items() if k[1] == "pattern"]
    assert {v["value"] for v in u["observed"]} == {"AlphaTimeout", "BetaTimeout", "GammaTimeout"}
    assert {v["value"]: v["episodes"] for v in u["observed"]}["BetaTimeout"] == ["ep-beta"]

    assert "glob" in ctx["constants"][step] and "pattern" not in ctx["constants"][step]
    assert set(ctx["extracts"]) == {s["id"] for s in _flow(world)["steps"]}, "every step's extracts are shown"
    prompt = refine.refine_prompt(ctx)
    assert "AlphaTimeout" in prompt and "ep-beta" in prompt and "unfixable" in prompt


def test_a_missing_episode_is_reported_not_fatal(world):
    (world["traces"] / "ep-gamma.jsonl").unlink()
    ctx = refine.gather(_flow(world), trace_dir=world["traces"], schemas=False)
    assert ctx["missing_episodes"] == ["ep-gamma"] and len(ctx["sessions"]) == 2


# ---------------------------------------------------------------- 2. a binding that reproduces every episode
ACCEPTED = """
name: investigate-payments-timeout
step_titles: {issue: "The Jira ticket", code: "Where the error is raised"}
inputs:
  error_marker: {type: string, description: The exception name to grep for, example: AlphaTimeout, required: true}
bindings:
  - step: code
    arg: pattern
    kind: input
    template: "{{ inputs.error_marker }}"
  - step: issue
    arg: issue_key
    kind: derive
    template: "{{ inputs.key }}"
"""


def test_a_binding_that_reproduces_every_episode_is_accepted(world):
    rep = run(world, ACCEPTED)
    b = verdicts(rep)[("code", "pattern")]
    assert b["accepted"] and b["episodes"] == 3 and b["checked"] == 3
    assert verdicts(rep)[("issue", "issue_key")]["accepted"]

    new = yaml.safe_load(open(rep["path"]))
    code = next(s for s in new["steps"] if s["id"] == "code")
    assert code["args"]["pattern"] == "{{ inputs.error_marker }}" and "unresolved" not in code
    assert new["inputs"]["error_marker"]["type"] == "string"
    assert code["title"] == "Where the error is raised"
    assert rep["bindability"]["after"] > rep["bindability"]["before"]
    assert rep["cost_usd"] == 0.31


def test_the_new_version_never_touches_the_original(world):
    before = (world["flows"] / f"{DRAFT}.yaml").read_text()
    rep = run(world, ACCEPTED)
    assert (world["flows"] / f"{DRAFT}.yaml").read_text() == before
    assert rep["path"].endswith("investigate-payments-timeout.v1.yaml")
    new = yaml.safe_load(open(rep["path"]))
    assert new["status"] == "draft" and new["refined_from"] == DRAFT and new["name"] == "investigate-payments-timeout"
    assert new["refined"]["accepted"] == ["issue.issue_key", "code.pattern"] or set(new["refined"]["accepted"]) == {
        "issue.issue_key", "code.pattern"}


# ---------------------------------------------------------------- 3. a binding that fails one episode
PARTIAL = """
name: investigate-payments-timeout
bindings:
  - step: confluence
    arg: query
    kind: derive
    template: "{{ inputs.key }}"
"""


def test_a_binding_that_fails_one_episode_is_rejected_naming_it(world):
    rep = run(world, PARTIAL)
    b = verdicts(rep)[("confluence", "query")]
    assert not b["accepted"]
    assert "ep-gamma" in b["reason"] and "runbook rollback" in b["reason"] and "PAY-103" in b["reason"]
    assert b["episodes"] == 2 and b["checked"] == 3, "two episodes agreed, which is not enough"


def test_a_rejected_binding_leaves_the_argument_exactly_as_the_draft_had_it(world):
    draft_arg = next(s for s in _flow(world)["steps"] if s["id"] == "code")["args"]["pattern"]
    rep = run(world, """
name: investigate-payments-timeout
bindings:
  - {step: code, arg: pattern, kind: derive, template: "{{ issue.error_class }}"}
""")
    assert not verdicts(rep)[("code", "pattern")]["accepted"]
    new = yaml.safe_load(open(rep["path"]))
    assert next(s for s in new["steps"] if s["id"] == "code")["args"]["pattern"] == draft_arg


# ---------------------------------------------------------------- 4. inputs
def test_an_unreferenced_input_is_dropped(world):
    rep = run(world, """
name: investigate-payments-timeout
inputs:
  error_marker: {type: string, example: AlphaTimeout}
  since: {type: string, description: how far back to look, example: 7d}
bindings:
  - {step: code, arg: pattern, kind: input, template: "{{ inputs.error_marker }}"}
""")
    by = {i["name"]: i for i in rep["inputs"]}
    assert by["error_marker"]["accepted"]
    assert not by["since"]["accepted"] and "no binding template references it" in by["since"]["reason"]
    assert "since" not in yaml.safe_load(open(rep["path"]))["inputs"]


def test_an_input_that_is_the_same_in_every_episode_is_a_constant_not_a_parameter(world):
    rep = run(world, """
name: investigate-payments-timeout
inputs:
  file_glob: {type: string, example: "**/*.py"}
bindings:
  - {step: code, arg: glob, kind: input, template: "{{ inputs.file_glob }}"}
""")
    by = {i["name"]: i for i in rep["inputs"]}
    assert not by["file_glob"]["accepted"] and "constant, not a parameter" in by["file_glob"]["reason"]
    assert not verdicts(rep)[("code", "glob")]["accepted"]
    assert "file_glob" not in (yaml.safe_load(open(rep["path"])).get("inputs") or {})


def test_an_input_whose_only_binding_was_rejected_is_dropped(world):
    rep = run(world, """
name: investigate-payments-timeout
inputs:
  wrong: {type: string}
bindings:
  - {step: confluence, arg: query, kind: derive, template: "{{ inputs.wrong }}-x"}
""")
    by = {i["name"]: i for i in rep["inputs"]}
    assert not by["wrong"]["accepted"] and "rejected" in by["wrong"]["reason"]


# ---------------------------------------------------------------- 5. unfixable
def test_unfixable_is_recorded_and_leaves_the_argument_alone(world):
    draft_arg = next(s for s in _flow(world)["steps"] if s["id"] == "code")["args"]["pattern"]
    rep = run(world, """
name: investigate-payments-timeout
bindings:
  - step: code
    arg: pattern
    kind: unfixable
    reason: nothing in the ticket or an earlier result carries the exception name; the agent knew it from the codebase
""")
    b = verdicts(rep)[("code", "pattern")]
    assert b["unfixable"] and not b["accepted"] and "nothing in the ticket" in b["reason"]
    new = yaml.safe_load(open(rep["path"]))
    assert next(s for s in new["steps"] if s["id"] == "code")["args"]["pattern"] == draft_arg
    assert "code.pattern" in new["refined"]["unfixable"]
    assert "unfixable" in refine.format_report(rep, DRAFT)


def test_unfixable_without_a_reason_is_not_a_proposal(world):
    rep = run(world, """
name: investigate-payments-timeout
bindings:
  - {step: code, arg: pattern, kind: unfixable}
""")
    b = verdicts(rep)[("code", "pattern")]
    assert not b["accepted"] and not b.get("unfixable") and "without a reason" in b["reason"]


# ---------------------------------------------------------------- 6. name and card
def test_a_bad_flow_name_is_rejected_and_the_draft_name_is_kept(world):
    rep = run(world, 'name: "Investigate Payments!"\n')
    assert not rep["name"]["accepted"] and "kebab-case" in rep["name"]["reason"]
    assert rep["name"]["used"] == DRAFT and rep["path"].endswith(f"{DRAFT}.v2.yaml")


def test_a_name_already_taken_is_rejected(world):
    (world["flows"] / "already-here.yaml").write_text("name: already-here\nsteps: []\n")
    rep = run(world, "name: already-here\n")
    assert not rep["name"]["accepted"] and "already exists" in rep["name"]["reason"]
    assert rep["name"]["used"] == DRAFT


def test_the_name_option_overrides_the_agents(world):
    rep = run(world, "name: investigate-payments-timeout\n", name="chosen-by-hand")
    assert rep["name"]["used"] == "chosen-by-hand" and rep["path"].endswith("chosen-by-hand.v1.yaml")


def test_a_card_naming_an_unknown_step_is_rejected(world):
    original = _flow(world)["card"]
    rep = run(world, """
name: investigate-payments-timeout
card:
  use_case: Find the code behind a payments timeout ticket.
  expected_outputs:
    - {name: ticket, steps: [issue], description: the ticket, required: true}
    - {name: dashboards, steps: [grafana], description: a dashboard, required: false}
""")
    assert not rep["card"]["accepted"] and "unknown step 'grafana'" in rep["card"]["reason"]
    assert yaml.safe_load(open(rep["path"]))["card"] == original


def test_a_good_card_is_attached_and_marked_authored_by_agent(world):
    rep = run(world, """
name: investigate-payments-timeout
card:
  use_case: Find the code behind a payments timeout ticket.
  inputs_explained: {key: the Jira key of the ticket}
  expected_outputs:
    - {name: ticket, steps: [issue], description: the ticket and its window, required: true}
  not_covered: [no metrics, no pagerduty]
  example: {inputs: {key: PAY-101}, found: the handler that raises the timeout}
""")
    assert rep["card"]["accepted"]
    card = yaml.safe_load(open(rep["path"]))["card"]
    assert card["authored_by"] == "agent" and card["not_covered"] == ["no metrics", "no pagerduty"]


def test_a_title_for_an_unknown_step_is_dropped(world):
    rep = run(world, 'name: investigate-payments-timeout\nstep_titles: {issue: "The ticket", nope: "Nothing"}\n')
    by = {t["step"]: t for t in rep["step_titles"]}
    assert by["issue"]["accepted"] and not by["nope"]["accepted"] and by["nope"]["reason"] == "no such step"


# ---------------------------------------------------------------- 7. malformed proposals
def test_a_message_without_a_yaml_block_changes_nothing(world):
    def driver_fn(trigger, inputs, prompt, **kw):
        return {"session_id": "fake", "cost_usd": 0.01, "result": "I could not work it out.", "is_error": False}
    rep = refine.refine(DRAFT, driver_fn=driver_fn, flow_dir=world["flows"], trace_dir=world["traces"], schemas=False)
    assert not rep["proposal_found"] and not rep["bindings"]
    assert rep["bindability"]["after"] == rep["bindability"]["before"]


def test_a_binding_on_a_step_that_does_not_exist_is_rejected_on_sight(world):
    rep = run(world, """
name: investigate-payments-timeout
bindings:
  - {step: grafana, arg: q, kind: derive, template: "{{ inputs.key }}"}
  - {step: code, arg: pattern, kind: derive, extract: {step: logs, name: x, spec: {from: a}}, template: "{{ code.x }}"}
""")
    reasons = [b["reason"] for b in rep["bindings"]]
    assert any("no step 'grafana'" in r for r in reasons)
    assert any("unknown step 'logs'" in r for r in reasons)


def test_an_extract_from_a_later_step_is_rejected(world):
    rep = run(world, """
name: investigate-payments-timeout
bindings:
  - {step: issue, arg: issue_key, kind: derive, template: "{{ code.files | first }}",
     extract: {step: code, name: files, spec: {from: "matches[*].file", all: true}}}
""")
    assert "does not run before" in verdicts(rep)[("issue", "issue_key")]["reason"]


# ---------------------------------------------------------------- 8. a proposed extract that does work
def test_a_derive_binding_may_add_the_extract_it_needs(world):
    rep = run(world, """
name: investigate-payments-timeout
bindings:
  - step: confluence
    arg: query
    kind: derive
    template: "{{ issue.ticket_key }}"
    extract: {step: issue, name: ticket_key, spec: {from: key}}
""")
    b = verdicts(rep)[("confluence", "query")]
    # ep-gamma searched for "runbook rollback", not its ticket key, so even a correct extract cannot be kept
    assert not b["accepted"] and "ep-gamma" in b["reason"]
    new = yaml.safe_load(open(rep["path"]))
    assert "ticket_key" not in (next(s for s in new["steps"] if s["id"] == "issue").get("extract") or {})


def test_an_accepted_binding_carries_its_extract_into_the_new_version(world):
    rep = run(world, """
name: investigate-payments-timeout
bindings:
  - step: code
    arg: pattern
    kind: input
    template: "{{ inputs.error_marker }}"
inputs: {error_marker: {type: string}}
""")
    assert verdicts(rep)[("code", "pattern")]["accepted"]
    new = yaml.safe_load(open(rep["path"]))
    assert new["inputs"]["error_marker"]["required"] is True


# ---------------------------------------------------------------- 9. guards
def test_crystal_no_agent_refuses_before_anything_is_launched(world):
    with pytest.raises(RuntimeError, match="CRYSTAL_NO_AGENT"):
        refine.refine(DRAFT, flow_dir=world["flows"], trace_dir=world["traces"], schemas=False)


def test_preflight_refuses_when_claude_is_not_installed(monkeypatch):
    monkeypatch.delenv("CRYSTAL_NO_AGENT", raising=False)
    monkeypatch.setattr(refine, "claude_path", lambda: None)
    with pytest.raises(RuntimeError, match="not installed"):
        refine.preflight()


def test_a_flow_with_no_surviving_episodes_cannot_be_refined(world):
    for p in world["traces"].glob("*.jsonl"):
        p.unlink()
    with pytest.raises(RuntimeError, match="no supporting episodes"):
        run(world, ACCEPTED)


def test_the_cli_refuses_without_yes(world, monkeypatch, capsys):
    monkeypatch.delenv("CRYSTAL_NO_AGENT", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(refine, "claude_path", lambda: "/usr/bin/claude")
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert refine.refine_main([DRAFT]) == 2


def test_the_report_table_names_every_proposal(world):
    rep = run(world, ACCEPTED)
    table = refine.format_report(rep, DRAFT)
    assert "bind code.pattern" in table and "accepted" in table and "bindability" in table and "COST: $0.3100" in table


# ---------------------------------------------------------------- 10. the "Refine with an agent" button
UI_PROPOSAL = """
name: payments-incident-dossier
step_titles: {issue: "The Jira ticket"}
inputs: {nothing: {type: string}}
bindings:
  - {step: issue, arg: issue_key, kind: derive, template: "{{ inputs.key }}"}
  - {step: slack, arg: limit, kind: unfixable, reason: the agent picked 20 by habit}
"""

UI_FLOW = "induced-jira-ticket"      # a seeded sim draft with induced_from episodes


@pytest.fixture
def client(fresh_home, monkeypatch):
    from starlette.testclient import TestClient

    from crystal.app import main as ui
    from crystal.app import routes_record as rr
    from crystal.trace import driver
    if hasattr(ui.app.state, "workspace"):
        monkeypatch.delattr(ui.app.state, "workspace")
    monkeypatch.setattr(driver, "claude_path", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(rr, "_THREADS", {})
    monkeypatch.setattr(driver, "run_agent", lambda trigger, inputs, prompt, **kw: {
        "session_id": kw.get("session_id") or "fake", "cost_usd": 0.12, "num_turns": 3, "duration_ms": 10,
        "is_error": False, "returncode": 0, "stderr": "", "trace_path": "", "calls": [],
        "result": "Proposal:\n```yaml\n" + UI_PROPOSAL.strip() + "\n```"})
    c = TestClient(ui.app)
    c.state = fresh_home
    return c


def _wait(job):
    from crystal.app import routes_record as rr
    t = rr._THREADS.get(job)
    if t:
        t.join(60)
    assert not (t and t.is_alive())


def test_the_flow_page_offers_refine_and_needs_the_checkbox(client):
    r = client.get(f"/flows/{UI_FLOW}")
    assert r.status_code == 200 and "Refine with an agent" in r.text and 'name="confirm"' in r.text
    assert "replayed" in r.text and "costs money" in r.text
    r = client.post(f"/flows/{UI_FLOW}/refine", data={"confirm": "", "budget": "2"}, follow_redirects=False)
    assert r.status_code == 303 and "tick+the+box" in r.headers["location"]


def test_refining_a_flow_that_was_not_induced_is_refused(client):
    r = client.post("/flows/investigate-jira-ticket/refine", data={"confirm": "yes", "budget": "2"},
                    follow_redirects=False)
    assert r.status_code == 303 and "not+induced" in r.headers["location"]


def test_the_button_runs_the_job_and_shows_the_accepted_rejected_table(client):
    r = client.post(f"/flows/{UI_FLOW}/refine", data={"confirm": "yes", "budget": "2"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    job = r.headers["location"].rsplit("/", 1)[1]
    _wait(job)
    page = client.get(f"/record/{job}")
    assert page.status_code == 200, page.text
    assert "What the agent proposed" in page.text
    assert "payments-incident-dossier" in page.text and "unfixable" in page.text
    assert "dropped" in page.text, "the input nothing references is dropped"
    from crystal.app import routes_record as rr
    from crystal import workspace as ws_mod
    rec = rr.load_job(ws_mod.current(), job)
    assert rec["status"] == "done" and rec["cost_usd"] == 0.12
    view = rec["refine"]
    assert view["flow"] == "payments-incident-dossier" and view["from"] == UI_FLOW
    assert (client.state.flows / "payments-incident-dossier.v1.yaml").exists()
    assert (client.state.flows / f"{UI_FLOW}.yaml").exists(), "the original is untouched"
    new = yaml.safe_load((client.state.flows / "payments-incident-dossier.v1.yaml").read_text())
    assert new["status"] == "draft" and new["refined_from"] == UI_FLOW
    assert "nothing" not in (new.get("inputs") or {})
