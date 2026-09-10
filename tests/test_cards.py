"""Flow cards (crystal/flow/cards.py): the deterministic skeleton, coverage of a run against the card's expected
outputs, the headline facts derived from a run's extracts (PAY-101 and the AUTH-122 repeat), parsing the card an
agent ends its message with (fake driver, no agent), and the step ids of every committed card."""
import asyncio
import copy
import json
from pathlib import Path

import pytest
import yaml

from crystal.author import author as author_cmd, author_prompt
from crystal.flow.cards import (AUTHORED_BY, CARD_KEYS, card_yaml, coverage, headline, merge_card, parse_card,
                                skeleton_card, step_hits, validate_card)
from crystal.flow.runner import FlowRunner, load_flow
from crystal.mcp_client import ServerPool
from tests.test_author import extra_comments, fake_driver_factory, world  # noqa: F401  (fixture)

FLOWS = Path(__file__).resolve().parent.parent / "examples" / "sim" / "flows"


def _run(flow: str, inputs: dict) -> dict:
    async def go():
        async with ServerPool() as pool:
            return await FlowRunner(pool, lifecycle=False).run(load_flow(flow), inputs, save=False)
    return asyncio.run(go())


@pytest.fixture(scope="module")
def jira_flow():
    return load_flow("investigate-jira-ticket")


@pytest.fixture(scope="module")
def pay101():
    return _run("investigate-jira-ticket", {"key": "PAY-101"})


@pytest.fixture(scope="module")
def auth122():
    return _run("investigate-jira-ticket", {"key": "AUTH-122"})


def _by(card_or_cov, name):
    return next(e for e in card_or_cov["expected_outputs" if "expected_outputs" in card_or_cov else "expected"] if e["name"] == name)


# ---------------------------------------------------------------- skeleton
def test_skeleton_groups_steps_by_evidence_type(jira_flow):
    card = skeleton_card(jira_flow)
    assert set(card) == set(CARD_KEYS) and card["authored_by"] == "skeleton"
    names = [e["name"] for e in card["expected_outputs"]]
    assert names[:3] == ["ticket", "slack discussion", "thread"] and "repeats" in names
    assert _by(card, "logs")["steps"] == ["logs", "logs_by_error"] and _by(card, "runbook")["steps"] == ["confluence", "runbook"]
    assert _by(card, "ticket")["required"] is True and _by(card, "logs")["required"] is False   # only `required:` steps
    assert "per item of" in _by(card, "logs")["description"] and "ladder" in _by(card, "slack discussion")["description"]
    assert "extracts summary" in _by(card, "ticket")["description"]
    assert validate_card(jira_flow, card) == []
    assert "PAY-101" in card["inputs_explained"]["key"] and "jira_key" in card["inputs_explained"]["key"]
    assert card["example"] == {"inputs": {"key": "PAY-101"}, "found": ""}
    assert any("first 6 items" in x for x in card["not_covered"]) and not any("no jira" in x for x in card["not_covered"])
    # the inducer's report marks the steps that had hits in every session as required
    with_report = skeleton_card(jira_flow, {"tests": {"min_hits": ["slack", "logs"]}})
    assert _by(with_report, "slack discussion")["required"] and _by(with_report, "logs")["required"]
    assert with_report["example"]["found"] == "Every traced run had hits on: logs, slack."


def test_skeleton_of_a_flow_without_steps_is_still_a_card():
    card = skeleton_card({"name": "x", "trigger": {"type": "slack_dm"}, "inputs": {"text": {"type": "string"}}, "steps": []})
    assert card["expected_outputs"] == [] and card["inputs_explained"] == {"text": "text (string)"}
    assert "slack dm" in card["use_case"] and len(card["not_covered"]) == 8


# ---------------------------------------------------------------- coverage
def test_step_hits_sums_fanouts_and_zeroes_skipped():
    run = {"steps": [{"id": "a", "hits": 3}, {"id": "b", "items": [{"hits": 2}, {"hits": 5}], "hits": 7},
                     {"id": "c", "skipped": "when=false"}, {"id": "d", "hits": None}]}
    assert [step_hits(run, s) for s in "abcd"] == [3, 7, 0, 0] and step_hits(run, "absent") == 0


def test_coverage_on_a_real_run(jira_flow, pay101):
    cov = coverage(jira_flow, pay101)
    assert cov["found"] == cov["total"] == len(jira_flow["card"]["expected_outputs"]) and cov["missing"] == [] and cov["required_missing"] == []
    assert _by(cov, "repeats")["hits"] == 2 and _by(cov, "logs")["hits"] > 20 and _by(cov, "ticket")["required"]
    assert set(_by(cov, "logs")) == {"name", "steps", "description", "required", "found", "hits"}
    # a poorer run: no commits, thread with nothing, logs steps missing altogether
    run = copy.deepcopy(pay101)
    run["steps"] = [s for s in run["steps"] if s["id"] not in ("logs", "logs_by_error")]
    for s in run["steps"]:
        if s["id"] == "commits":
            s["hits"] = 0
        if s["id"] == "suspect_commit":
            s["skipped"] = "when=false"
            s.pop("items")
    cov = coverage(jira_flow, run)
    assert cov["missing"] == ["logs", "commits"] and cov["required_missing"] == ["logs"]
    assert cov["found"] == cov["total"] - 2
    # without a card the skeleton's groups are the yardstick
    bare = {"name": "bare", "steps": jira_flow["steps"], "inputs": jira_flow["inputs"]}
    cov = coverage(bare, run)
    assert "logs" in cov["missing"] and "commits" in cov["missing"] and "suspect commit" in cov["missing"] and cov["required_missing"] == []


# ---------------------------------------------------------------- headline
def test_headline_pay101(jira_flow, pay101):
    h = headline(jira_flow, pay101)
    assert h["ticket"] == "PAY-101" and h["what"].startswith("PaymentGatewayTimeout: gateway") and h["error_class"] == "PaymentGatewayTimeout"
    assert h["service"] == "payments-api" and h["team_owners"] == ["@payments"]
    assert h["status"] == {"jira": "Done", "pagerduty": "resolved"}
    w = h["when"]
    assert w["start"] < w["anchor"] < w["end"] and w["anchor"] == "2026-08-06T11:37:00Z"
    cb = h["changed_before"]
    assert cb["suspect_sha"].startswith("b4d28f39a3") and cb["suspect_sha"] in [c["sha"] for c in cb["commits"]]
    assert {c["subject"] for c in cb["commits"]} >= {"payments-api: tighten validation in charge"}
    assert all(set(c) == {"sha", "subject", "author", "date"} for c in cb["commits"])
    assert h["people"] == ["alice", "bob"]
    assert len(h["trace_ids"]) == 2 and all(len(t) == 32 for t in h["trace_ids"])
    assert all(p.startswith("payments-api-") for p in h["pods"]) and len(h["pods"]) == len(set(h["pods"]))
    assert [r["key"] for r in h["repeat_of"]] == ["PAY-108", "PAY-102"]   # same error class + component, newest first
    assert all(r["status"] and r["created"] and "PaymentGatewayTimeout" in r["summary"] for r in h["repeat_of"])


def test_headline_repeat_pair(jira_flow, auth122):
    h = headline(jira_flow, auth122)
    assert h["ticket"] == "AUTH-122" and h["error_class"] == "AuthTokenExpired" and h["service"] == "auth-service"
    assert h["team_owners"] == ["@identity"] and h["status"]["jira"] == "Done"
    keys = [r["key"] for r in h["repeat_of"]]
    assert "AUTH-117" in keys and "AUTH-122" not in keys
    # the thread names a pod hash and a red-herring sha before the real one; the suspect is the sha in git log
    assert h["changed_before"]["suspect_sha"].startswith("25acaf6a47")
    assert "ivan" in h["people"] and "judy" in h["people"]


def test_headline_is_robust_to_missing_pieces(jira_flow):
    assert headline(jira_flow, {"inputs": {}, "steps": []}) == {}
    skipped = {"inputs": {"key": "XY-1"}, "steps": [{"id": "issue", "tool": "jira.jira_get_issue", "skipped": "when=false"}]}
    assert headline(jira_flow, skipped) == {"ticket": "XY-1"}
    partial = {"inputs": {}, "steps": [
        {"id": "thread", "tool": "slack.conversations_replies", "hits": 2, "extracts": {"error_class": "Boom", "people": ["x", "y", "x"], "window": {"start": "a", "end": "b"}}},
        {"id": "pagerduty", "tool": "pagerduty.list_incidents", "hits": 1, "extracts": {},
         "result": {"incidents": [{"status": "triggered", "teams": [{"summary": "platform"}]}]}},
        {"id": "commits", "tool": "git.git_log", "hits": 0, "extracts": {}, "result": {"commits": []}}]}
    h = headline({"steps": []}, partial)
    assert h == {"what": "Boom", "error_class": "Boom", "when": {"start": "a", "end": "b"}, "team_owners": ["platform"],
                 "status": {"pagerduty": "triggered"}, "people": ["x", "y"]}


# ---------------------------------------------------------------- the agent's card
AGENT_MESSAGE = """Summary: the flow found the thread and the logs; I added the ticket comments by hand.

```json
{"not": "a card"}
```

Here is the card:

```yaml
card:
  use_case: Reach for this when a Jira key lands in your lap and you want the incident's story before touching prod.
  inputs_explained:
    key: The Jira key, e.g. PAY-108.
    bogus: not an input
  expected_outputs:
    - name: ticket
      steps: [issue, jira_get_issue_comments]
      description: The ticket and its comments (who rolled back what).
      required: true
    - name: logs
      steps: [logs, nope]
      description: Log lines per trace id.
      required: yes
    - name: ghost
      steps: [not_a_step]
      description: dropped
  not_covered: [deploy announcements]
  example:
    inputs: {key: PAY-108}
    found: An in-progress ticket with an acknowledged PagerDuty incident and no rollback yet.
  authored_by: agent
```
"""


def test_parse_card_from_agent_message():
    card = parse_card(AGENT_MESSAGE)
    assert card["use_case"].startswith("Reach for this") and len(card["expected_outputs"]) == 3
    assert parse_card("no card here") is None and parse_card("") is None and parse_card(None) is None
    assert parse_card("```yaml\n- just\n- a list\n```") is None and parse_card("```yaml\nkey: [unclosed\n```") is None
    bare = parse_card("text\n```yaml\nuse_case: bare mapping without the card key\n```\n")
    assert bare == {"use_case": "bare mapping without the card key"}
    # the last yaml block wins
    two = parse_card("```yaml\nuse_case: first\n```\nlater:\n```yaml\nuse_case: second\n```")
    assert two["use_case"] == "second"


def test_merge_card_validates_step_ids(jira_flow):
    flow = {"name": "f", "inputs": {"key": {"type": "jira_key"}},
            "steps": [{"id": "issue", "tool": "jira.jira_get_issue"}, {"id": "logs", "tool": "logz.search_logs"}, {"id": "jira_get_issue_comments", "tool": "jira.jira_get_issue_comments"}]}
    card, warnings = merge_card(flow, parse_card(AGENT_MESSAGE))
    assert card["authored_by"] == "agent" and validate_card(flow, card) == []
    assert [(e["name"], e["steps"], e["required"]) for e in card["expected_outputs"]] == [("ticket", ["issue", "jira_get_issue_comments"], True), ("logs", ["logs"], True)]
    assert card["inputs_explained"] == {"key": "The Jira key, e.g. PAY-108."} and card["not_covered"] == ["deploy announcements"]
    assert card["example"]["found"].startswith("An in-progress ticket")
    assert any("'nope'" in w for w in warnings) and any("'ghost'" in w and "dropped" in w for w in warnings) and any("'bogus'" in w for w in warnings)
    # nothing parseable: the skeleton, flagged
    card, warnings = merge_card(flow, None)
    assert card["authored_by"] == "skeleton" and card["use_case"].startswith("f.") and warnings == ["no card found in the agent's message; keeping the skeleton"]
    # a card whose steps all miss keeps the skeleton's expected outputs
    card, warnings = merge_card(flow, {"use_case": "x", "expected_outputs": [{"name": "a", "steps": ["zzz"]}]})
    assert card["use_case"] == "x" and [e["name"] for e in card["expected_outputs"]] == ["ticket", "logs", "ticket comments"]
    assert any("keeping the skeleton's" in w for w in warnings)
    assert validate_card(jira_flow, {"expected_outputs": [{"name": "a", "steps": ["nope"]}], "authored_by": "robot"}) == [
        "expected output 'a' names unknown step 'nope'", "authored_by must be one of skeleton, agent, human"]


def test_author_prompt_asks_for_the_card():
    flows = [{"name": "investigate-jira-ticket", "status": "candidate", "inputs": {"key": {"type": "jira_key"}},
              "steps": [{"id": "issue", "tool": "jira.jira_get_issue"}, {"id": "slack", "tool": "slack.conversations_search_messages"}]}]
    p = author_prompt("jira_issue", {"key": "PAY-108"}, flows)
    assert p.index("Step 3") < p.index("END your message with a flow card") < p.index("```yaml") < p.index("authored_by: skeleton")
    assert "Step ids you may reference: issue, slack" in p and "jira_get_issue -> issue" in p
    p0 = author_prompt("jira_issue", {"key": "PAY-108"}, [])
    assert "flow card" in p0 and "expected_outputs: []" in p0 and "key: key (string), e.g. PAY-108" in p0


def test_author_merges_the_agents_card_into_the_new_version(world):   # noqa: F811
    inner = fake_driver_factory(world, extra_comments)

    def driver_fn(trigger, inputs, prompt, **kw):
        assert "END your message with a flow card" in prompt
        info = inner(trigger, inputs, prompt, **kw)
        info["result"] = AGENT_MESSAGE
        return info
    res = author_cmd("jira_issue", {"key": "PAY-101"}, driver_fn=driver_fn, trace_dir=world["traces"], flow_dir=world["flows"], run_dir=world["runs"], test=False)
    flow = yaml.safe_load(res["path"].read_text())
    card = flow["card"]
    assert card["authored_by"] == "agent" and validate_card(flow, card) == []
    assert card["use_case"].startswith("Reach for this")
    assert [e["steps"] for e in card["expected_outputs"]] == [["issue", "jira_get_issue_comments"], ["logs"]]
    assert res["report"]["card"]["authored_by"] == "agent" and any("'nope'" in w for w in res["report"]["card"]["warnings"])
    assert card["inputs_explained"] == {"key": "The Jira key, e.g. PAY-108."}
    # the fake driver's plain prose ("summary: added comments") leaves the skeleton, flagged
    res = author_cmd("jira_issue", {"key": "PAY-101"}, driver_fn=inner, trace_dir=world["traces"], flow_dir=world["flows"], run_dir=world["runs"], test=False)
    flow = yaml.safe_load(res["path"].read_text())
    assert flow["card"]["authored_by"] == "skeleton" and res["report"]["card"]["warnings"] == ["no card found in the agent's message; keeping the skeleton"]
    assert validate_card(flow, flow["card"]) == [] and _by(flow["card"], "ticket")["required"]


# ---------------------------------------------------------------- committed cards and the CLI
def test_every_committed_card_has_valid_step_ids():
    seen = {}
    for p in sorted(FLOWS.glob("*.yaml")):
        f = yaml.safe_load(p.read_text())
        card = f.get("card")
        assert card, f"{p.name}: no card"
        assert validate_card(f, card) == [], (p.name, validate_card(f, card))
        assert set(card) == set(CARD_KEYS) and card["authored_by"] in AUTHORED_BY, p.name
        assert card["expected_outputs"] and all(e["description"] for e in card["expected_outputs"]), p.name
        assert set(card["inputs_explained"]) == set(f.get("inputs") or {}), p.name
        seen[f["name"]] = card["authored_by"]
    assert {k: v for k, v in seen.items() if v == "human"} == {n: "human" for n in ("investigate-jira-ticket", "investigate-slack-thread", "investigate-slack-dm")}
    assert all(v == "skeleton" for k, v in seen.items() if k.startswith("induced-") or ".v" in k), seen


def test_cli_card_prints_and_writes_once(tmp_path, capsys):
    from crystal.cli import cmd_card
    p = tmp_path / "mini.yaml"
    p.write_text("name: mini   # a comment that must survive\ntrigger: {type: jira_issue}\ninputs:\n  key: {type: jira_key, example: PAY-1}\n"
                 "steps:\n  - id: issue\n    tool: jira.jira_get_issue\n    required: true\n    args: {issue_key: '{{ inputs.key }}'}\n")
    assert cmd_card([str(p)]) == 0
    out = capsys.readouterr().out
    assert "card by: skeleton" in out and "ticket [required] steps: issue" in out and "not in the YAML" in out
    assert "card:" not in p.read_text()
    assert cmd_card([str(p), "--write"]) == 0 and "wrote skeleton card" in capsys.readouterr().out
    text = p.read_text()
    assert "# a comment that must survive" in text and text.count("\ncard:\n") == 1
    f = yaml.safe_load(text)
    assert f["card"]["authored_by"] == "skeleton" and validate_card(f, f["card"]) == []
    assert cmd_card([str(p), "--write"]) == 0 and "already has a card" in capsys.readouterr().out
    assert p.read_text() == text
    assert cmd_card([]) == 1
    assert card_yaml(f["card"]).startswith("card:\n  use_case:")
