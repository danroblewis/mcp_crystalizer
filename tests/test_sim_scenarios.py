"""Regression tests for examples/sim/scenarios.py: the cascade / repeat / escalation / deploy / still-open
archetypes, on-call rotations, cross-referencing Jira comments, the runbook -> architecture ->
postmortem web, and noise -- plus the hard rules that protect the original 10 incidents.

Structural checks load examples/sim/data/world.json directly (fast, no subprocess); the determinism and
first-11-git-commit checks regenerate the world via `examples/sim/world.py` in a subprocess, since that is the
only way to observe the real generated repo.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SIM = ROOT / "examples" / "sim"
WORLD_PATH = SIM / "data" / "world.json"
REPO_PATH = SIM / "repo"

ORIGINAL_SERVICES = ["payments-api", "checkout-web", "inventory-sync", "notifier"]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def world() -> dict:
    return json.loads(WORLD_PATH.read_text())


@pytest.fixture(scope="module")
def main_world() -> dict:
    """The pre-expansion world (10 incidents), frozen as a fixture: the byte-identical baseline for the first parts."""
    return json.loads((ROOT / "tests" / "fixtures" / "world_baseline.json").read_text())


@pytest.fixture(scope="module")
def incidents_by_id(world) -> dict:
    return {i["id"]: i for i in world["incidents"]}


@pytest.fixture(scope="module")
def new_incidents(world) -> list:
    return world["incidents"][10:]


# ---------------------------------------------------------------- hard rules: the existing part is untouched
def test_first_ten_incidents_byte_identical_to_main(world, main_world):
    assert [i["id"] for i in world["incidents"][:10]] == [i["id"] for i in main_world["incidents"]]
    for k in range(10):
        assert world["incidents"][k] == main_world["incidents"][k], f"INC-{k+1:03d} changed"


def test_first_four_services_unchanged(world, main_world):
    for name in ORIGINAL_SERVICES:
        assert world["services"][name] == main_world["services"][name]
    assert list(world["services"].keys())[:4] == ORIGINAL_SERVICES


def test_first_eight_people_unchanged(world, main_world):
    main_names = list(main_world["people"].keys())
    assert len(main_names) == 8
    assert list(world["people"].keys())[:8] == main_names
    for n in main_names:
        assert world["people"][n] == main_world["people"][n]


def test_first_ten_channels_unchanged(world, main_world):
    main_names = list(main_world["channels"].keys())
    assert len(main_names) == 10
    assert list(world["channels"].keys())[:10] == main_names
    for n in main_names:
        assert world["channels"][n] == main_world["channels"][n]


def test_first_eight_confluence_pages_unchanged(world, main_world):
    assert len(main_world["confluence_pages"]) == 8
    for i in range(8):
        assert world["confluence_pages"][i] == main_world["confluence_pages"][i]


def test_scenarios_module_is_the_only_new_generator_hook():
    """examples/sim/world.py calls examples/sim/scenarios.py in exactly one place; sim/servers edits stay additive
    (new optional fields read with .get(...) defaults, no removed or renamed fields)."""
    world_src = (SIM / "world.py").read_text()
    assert world_src.count("scenarios.extend(") == 1
    for path, needle in [
        ("servers/jira.py", 'issue.get("issuelinks", [])'),
        ("servers/jira.py", 'inc["jira"].get("comments")'),
        ("servers/pagerduty.py", 'pd.get("related_incidents", [])'),
        ("servers/confluence.py", 'p.get("links", [])'),
        ("servers/confluence.py", 'p.get("parent_id")'),
        ("servers/slack.py", 'w.get("deploys", [])'),
    ]:
        assert needle in (SIM / path).read_text(), f"{path} missing additive change {needle!r}"


# ---------------------------------------------------------------- counts
def test_counts_within_target_ranges(world):
    assert len(world["incidents"]) == 50
    assert 12 <= len(world["services"]) <= 16
    assert 6 <= len(world["teams"]) <= 8
    assert 45 <= len(world["people"]) <= 55
    assert 45 <= len(world["channels"]) <= 55
    assert 45 <= len(world["confluence_pages"]) <= 55
    assert len(world["deploys"]) == 3
    assert set(world["oncall_rotations"]) == set(world["teams"])


def test_new_incident_ids_unique_and_contiguous(world):
    ids = [i["id"] for i in world["incidents"]]
    assert len(ids) == len(set(ids))
    assert ids == [f"INC-{k:03d}" for k in range(1, 51)]


def test_jira_pagerduty_confluence_slack_ids_globally_unique(world):
    keys = [i["jira"]["key"] for i in world["incidents"]] + [j["key"] for j in world["noise"]["jira"]]
    assert len(keys) == len(set(keys))
    pd_ids = [i["pagerduty"]["id"] for i in world["incidents"]]
    assert len(pd_ids) == len(set(pd_ids))
    page_ids = [p["id"] for p in world["confluence_pages"]]
    assert len(page_ids) == len(set(page_ids))
    thread_pairs = [(i["slack"]["channel"], i["slack"]["thread_ts"]) for i in world["incidents"]]
    assert len(thread_pairs) == len(set(thread_pairs))


# ---------------------------------------------------------------- referential integrity
def test_no_dangling_incident_references(world, incidents_by_id):
    jira_keys = {i["jira"]["key"] for i in world["incidents"]}
    pd_ids = {i["pagerduty"]["id"] for i in world["incidents"]}
    page_ids = {p["id"] for p in world["confluence_pages"]}
    for inc in world["incidents"]:
        if "parent_incident" in inc:
            assert inc["parent_incident"] in incidents_by_id
        for cid in inc.get("child_incidents", []):
            assert cid in incidents_by_id
        if "repeat_of" in inc:
            assert inc["repeat_of"] in incidents_by_id
        if "deploy_id" in inc:
            assert inc["deploy_id"] in {d["id"] for d in world["deploys"]}
        if "escalation" in inc:
            assert inc["escalation"]["team_channel"] in world["channels"]
        for link in inc["jira"].get("issuelinks", []):
            assert link["key"] in jira_keys, link
        for rel in inc["pagerduty"].get("related_incidents", []):
            assert rel in pd_ids, rel
    # the original 10 incidents' own confluence.page_id predates a real per-service runbook-page id
    # scheme (only the first incident of each service happens to match); that quirk is untouched by
    # design (byte-identical), so only the new incidents are held to "page_id always resolves".
    for inc in world["incidents"][10:]:
        assert inc["confluence"]["page_id"] in page_ids


def test_no_dangling_confluence_links(world):
    page_ids = {p["id"] for p in world["confluence_pages"]}
    jira_keys = {i["jira"]["key"] for i in world["incidents"]}
    commit_shas = {i["commit"] for i in world["incidents"]}
    for p in world["confluence_pages"]:
        if p.get("parent_id") is not None:
            assert p["parent_id"] in page_ids, p["id"]
        for link in p.get("links", []):
            assert link in page_ids or link in jira_keys or link in commit_shas, (p["id"], link)


def test_new_entities_are_not_isolated_islands(world):
    """'Every new entity has at least one edge into an existing or another new entity': build an undirected
    graph over incident/jira/pagerduty/slack-thread/confluence-page/deploy ids across the FULL world (old
    entities included as edge targets, since new pages deliberately link a couple of them -- see
    docs/sim-world.md) and check that the subgraph induced by the new (post-seed-10) entities is a single
    connected component with no isolated node. (The original 10 incidents are themselves 10 disjoint stars
    with no cross-links between them -- true before this change too -- so a single component over the
    *whole* world, old part included, is not the bar; not stranding anything new is.)"""
    import collections

    adj = collections.defaultdict(set)

    def edge(a, b):
        if a is None or b is None:
            return
        adj[a].add(b)
        adj[b].add(a)

    for inc in world["incidents"]:
        edge(inc["id"], inc["jira"]["key"])
        edge(inc["id"], inc["pagerduty"]["id"])
        edge(inc["id"], (inc["slack"]["channel"], inc["slack"]["thread_ts"]))
        edge(inc["id"], inc["confluence"]["page_id"])
        edge(inc["id"], inc.get("parent_incident"))
        for c in inc.get("child_incidents", []):
            edge(inc["id"], c)
        edge(inc["id"], inc.get("repeat_of"))
        edge(inc["id"], inc.get("deploy_id"))
    for p in world["confluence_pages"]:
        edge(p.get("parent_id"), p["id"])
        for link in p.get("links", []):
            edge(p["id"], link)
    for d in world["deploys"]:
        edge(d["id"], d["sha"])

    new_incident_ids = {i["id"] for i in world["incidents"][10:]}
    # filler/reference pages are deliberately generic wiki pages, not tied to any incident (that's the
    # point of them, per examples/sim/scenarios.py's FILLER_PAGE_TITLES) -- they are exempt from "must have an edge"
    new_page_ids = {p["id"] for p in world["confluence_pages"][8:] if p.get("labels") != ["reference"]}
    new_deploy_ids = {d["id"] for d in world["deploys"]}
    new_nodes = new_incident_ids | new_page_ids | new_deploy_ids
    assert new_nodes

    for n in new_nodes:
        assert adj[n], f"{n} has no edges at all"

    start = next(iter(new_nodes))
    seen = {start}
    frontier = [start]
    while frontier:
        n = frontier.pop()
        for nb in adj[n]:
            if nb not in seen:
                seen.add(nb)
                frontier.append(nb)
    unreached_new = new_nodes - seen
    assert not unreached_new, f"{len(unreached_new)} new entities unreachable from {start}: {list(unreached_new)[:10]}"

    # and the new world is reachable from the old one (at least one deliberate cross-link), so the
    # documentation web is a single walk from any original runbook into the new material
    old_page_ids = {p["id"] for p in world["confluence_pages"][:8]}
    assert old_page_ids & seen, "no path from the original confluence pages into the new material"


# ---------------------------------------------------------------- cascading multi-service incidents
def test_cascade_groups(world, incidents_by_id):
    cascades = [i for i in world["incidents"] if "child_incidents" in i]
    assert len(cascades) == 5
    assert sum(len(c["child_incidents"]) for c in cascades) == 10

    for up in cascades:
        assert len(up["child_incidents"]) == 2
        for cid in up["child_incidents"]:
            child = incidents_by_id[cid]
            assert child["parent_incident"] == up["id"]
            caused_by = [l["key"] for l in child["jira"]["issuelinks"] if l["type"] == "is caused by"]
            assert caused_by == [up["jira"]["key"]]
            causes = [l["key"] for l in up["jira"]["issuelinks"] if l["type"] == "causes"]
            assert child["jira"]["key"] in causes
            assert set(up["trace_ids"]) & set(child["trace_ids"]), "no shared (true causal) trace id"
            assert up["pagerduty"]["id"] in child["pagerduty"]["related_incidents"]
            assert child["pagerduty"]["id"] in up["pagerduty"]["related_incidents"]
            # each child also carries its own red-herring trace id(s), not present upstream
            assert set(child["trace_ids"]) - set(up["trace_ids"])
            # narrow time window: downstream surfaces within the hour
            assert timedelta(0) < _dt(child["started_at"]) - _dt(up["started_at"]) < timedelta(hours=1)
            # downstream's own thread lives in its team's alerts channel and permalinks back upstream
            assert child["slack"]["channel"] != "#incidents"
            texts = " ".join(m["text"] for m in child["slack"]["messages"])
            assert up["id"] in texts and "sim.slack.com/archives" in texts
        # upstream thread gets a reply from each downstream owner
        up_texts = " ".join(m["text"] for m in up["slack"]["messages"])
        assert up_texts.count("sim.slack.com/archives") >= 2


# ---------------------------------------------------------------- repeat incident pairs + postmortems
def test_repeat_pairs_and_postmortems(world, incidents_by_id):
    repeats = [i for i in world["incidents"] if "repeat_of" in i]
    assert len(repeats) == 6
    postmortems = [p for p in world["confluence_pages"] if "postmortem" in p.get("labels", [])]
    assert len(postmortems) == 6

    for second in repeats:
        first = incidents_by_id[second["repeat_of"]]
        assert first["service"] == second["service"]
        assert first["error_class"] == second["error_class"]
        delta_days = (_dt(second["started_at"]) - _dt(first["started_at"])).days
        assert 21 <= delta_days <= 56
        assert first["jira"]["key"] in second["jira"]["description"]
        first_msg_texts = second["slack"]["messages"][0]["text"]
        assert first["jira"]["key"] in first_msg_texts

        pm = next(p for p in postmortems if first["jira"]["key"] in p["links"] and second["jira"]["key"] in p["links"])
        assert first["commit"] in pm["links"] and second["commit"] in pm["links"]
        assert pm["parent_id"] == first["confluence"]["page_id"] == second["confluence"]["page_id"]
        assert _dt(pm["created"]) > _dt(second["started_at"])


# ---------------------------------------------------------------- escalated-from-team-channel incidents
def test_escalations(world):
    escalations = [i for i in world["incidents"] if "escalation" in i]
    assert len(escalations) == 6
    for inc in escalations:
        esc = inc["escalation"]
        assert esc["team_channel"] in world["channels"]
        assert esc["team_channel"] != inc["slack"]["channel"]
        assert esc["permalink"] == inc["slack"]["escalated_from_permalink"]
        assert world["channels"][esc["team_channel"]]["id"] in esc["permalink"]

        team_msgs = [m for m in world["noise"]["slack"] if m["channel"] == esc["team_channel"] and m.get("thread_ts") == esc["team_thread_ts"]]
        assert len(team_msgs) >= 2
        assert any("moving this to #incidents" in m["text"].lower() for m in team_msgs)
        assert any(inc["slack"]["thread_ts"].replace(".", "") in m["text"] for m in team_msgs)

        canonical_texts = " ".join(m["text"] for m in inc["slack"]["messages"])
        assert esc["permalink"] in canonical_texts
        pre_start = datetime.fromtimestamp(float(esc["team_thread_ts"]), tz=timezone.utc)
        gap = _dt(inc["started_at"]) - pre_start
        assert timedelta(minutes=10) <= gap <= timedelta(minutes=35)


# ---------------------------------------------------------------- deploy-preceded incidents
def test_deploy_preceded_incidents(world):
    assert len(world["deploys"]) == 3
    dep_incidents = [i for i in world["incidents"] if "deploy_id" in i]
    assert len(dep_incidents) == 3
    deploys_by_id = {d["id"]: d for d in world["deploys"]}
    for inc in dep_incidents:
        d = deploys_by_id[inc["deploy_id"]]
        assert d["service"] == inc["service"]
        assert d["sha"] == inc["commit"]
        assert d["channel"] == "#deploys"
        gap = _dt(inc["started_at"]) - _dt(d["at"])
        assert timedelta(0) < gap <= timedelta(minutes=30)
        first_text = inc["slack"]["messages"][0]["text"]
        assert d["permalink"] in first_text


# ---------------------------------------------------------------- still-open incidents
def test_still_open_incidents(world):
    still_open = [i for i in world["incidents"][10:] if i["jira"]["status"] in ("Open", "In Progress")
                  and i["pagerduty"]["status"] in ("triggered", "acknowledged") and i["pagerduty"]["resolved_at"] is None]
    assert len(still_open) == 4
    for inc in still_open:
        last = inc["slack"]["messages"][-1]["text"].lower()
        assert "rollback" not in last and ("still digging" in last or "no root cause" in last)
        pm_titles = [p["title"] for p in world["confluence_pages"] if inc["jira"]["key"] in p.get("links", [])]
        assert not pm_titles


# ---------------------------------------------------------------- on-call rotation
def test_oncall_rotation_consistency(world, new_incidents):
    rotations = world["oncall_rotations"]
    new_teams = [t for t in world["teams"] if t not in ORIGINAL_SERVICES and t in rotations and len(rotations[t]) > 1]
    assert len(new_teams) == 4  # the 4 new teams get real multi-window rotations

    def person_at(team, when):
        for w in rotations[team]:
            if _dt(w["start"]) <= when < _dt(w["end"]):
                return w["person"]
        return rotations[team][-1]["person"]

    checked = 0
    for inc in new_incidents:
        team = inc["team"]
        if team not in new_teams:
            continue
        when = _dt(inc["started_at"])
        expected = person_at(team, when)
        assert inc["jira"]["assignee"] == expected, (inc["id"], inc["jira"]["assignee"], expected)
        assert inc["pagerduty"]["assignee"] == expected
        assert world["people"][expected]["team"] == team
        checked += 1
    assert checked == 40  # every new incident is on a new-team service


# ---------------------------------------------------------------- cross-referencing Jira comments
def test_cross_referencing_jira_comments(world):
    with_comments = [i for i in world["incidents"] if i["jira"].get("comments")]
    assert len(with_comments) == 15
    for inc in with_comments:
        comments = inc["jira"]["comments"]
        assert 2 <= len(comments) <= 4
        for c in comments:
            assert {"author", "created", "body"} <= set(c)
        bodies = " ".join(c["body"] for c in comments)
        assert "sim.slack.com/archives" in bodies


# ---------------------------------------------------------------- long threads with red herrings
def test_red_herring_overlay(world, incidents_by_id):
    herring_incidents = [i for i in world["incidents"] if any("false alarm" in m["text"].lower() for m in i["slack"]["messages"])]
    assert len(herring_incidents) == 8
    for inc in herring_incidents:
        texts = [m["text"] for m in inc["slack"]["messages"]]
        joined = " ".join(texts)
        assert "{{" not in joined, "unresolved sha placeholder"
        # a wrong-but-real commit sha, distinct from this incident's own (the "Wait, might be ..." message,
        # not the ordinary "Suspect <own real sha> touched ..." message every incident thread also has)
        import re
        wrong_shas = set(re.findall(r"\b[0-9a-f]{10}\b", " ".join(t for t in texts if t.startswith("Wait, might be"))))
        assert wrong_shas, "no wrong-commit mention found"
        assert inc["commit"][:10] not in wrong_shas
        # a trace id copy-pasted from a different incident's own trace_ids, absent from this one's
        all_trace_ids_by_inc = {i["id"]: set(i["trace_ids"]) for i in world["incidents"]}
        mentioned = set()
        for t in texts:
            mentioned.update(re.findall(r"\b[0-9a-f]{32}\b", t))
        foreign = mentioned - all_trace_ids_by_inc[inc["id"]]
        assert foreign, "no foreign trace id mentioned"
        for tid in foreign:
            assert any(tid in ids for oid, ids in all_trace_ids_by_inc.items() if oid != inc["id"])


# ---------------------------------------------------------------- runbook -> architecture -> postmortem web
def test_architecture_runbook_postmortem_web(world):
    arch_pages = [p for p in world["confluence_pages"] if "architecture" in p.get("labels", [])]
    assert len(arch_pages) == 6
    new_runbooks = [p for p in world["confluence_pages"] if "runbook" in p.get("labels", []) and p["title"] not in [f"{s} runbook" for s in ORIGINAL_SERVICES]]
    assert len(new_runbooks) == 11
    for rb in new_runbooks:
        assert rb["links"], f"{rb['title']} should link its architecture page"
        arch = next(p for p in arch_pages if p["id"] in rb["links"])
        assert rb["id"] in arch["links"], "architecture page should link back to the runbook"

    postmortems = [p for p in world["confluence_pages"] if "postmortem" in p.get("labels", [])]
    for pm in postmortems:
        assert any(a["id"] in pm["links"] for a in arch_pages), f"{pm['title']} should link an architecture page"

    # original 8 pages must never have gained links/parent_id (would break byte-identical)
    for p in world["confluence_pages"][:8]:
        assert "links" not in p and "parent_id" not in p


def test_noise_has_no_linked_entities(world):
    jira_keys = {i["jira"]["key"] for i in world["incidents"]}
    linked_terms = jira_keys | {i["id"] for i in world["incidents"]}
    # noise entries added by scenarios.py (post the original 30) should not name a real incident/ticket
    scenario_noise = world["noise"]["slack"][30:]
    assert len(scenario_noise) >= 8
    for m in scenario_noise:
        if "moving this to #incidents" in m["text"] or "sim.slack.com/archives" in m["text"]:
            continue  # escalation team-channel messages are intentionally linked; not "noise" proper
        assert not (linked_terms & set(m["text"].replace(",", " ").split()))


# ---------------------------------------------------------------- git repo: commit history
def test_repo_first_11_commits_match_main():
    out = subprocess.run(["git", "log", "--reverse", "--format=%H"], cwd=REPO_PATH, capture_output=True, text=True, check=True)
    shas = out.stdout.split()
    main_world = json.loads((ROOT / "tests" / "fixtures" / "world_baseline.json").read_text())
    main_shas = {i["commit"] for i in main_world["incidents"]}
    assert len(shas) >= 52
    assert shas[0] not in main_shas  # "initial services" tree commit itself isn't an incident commit
    for sha in shas[1:11]:
        assert sha in main_shas


def test_new_commits_exist_and_come_after_the_first_eleven(world):
    out = subprocess.run(["git", "log", "--format=%H"], cwd=REPO_PATH, capture_output=True, text=True, check=True)
    all_shas = set(out.stdout.split())
    reverse = subprocess.run(["git", "log", "--reverse", "--format=%H"], cwd=REPO_PATH, capture_output=True, text=True, check=True).stdout.split()
    position = {sha: i for i, sha in enumerate(reverse)}
    for inc in world["incidents"][10:]:
        assert inc["commit"] in all_shas, inc["id"]
        assert position[inc["commit"]] > 10


# ---------------------------------------------------------------- determinism
def test_regenerating_the_world_is_byte_identical(tmp_path):
    before = WORLD_PATH.read_bytes()
    before_log = subprocess.run(["git", "log", "--format=%H"], cwd=REPO_PATH, capture_output=True, text=True, check=True).stdout
    subprocess.run([sys.executable, "world.py"], cwd=SIM, check=True, capture_output=True)
    after = WORLD_PATH.read_bytes()
    after_log = subprocess.run(["git", "log", "--format=%H"], cwd=REPO_PATH, capture_output=True, text=True, check=True).stdout
    assert before == after, "regenerating examples/sim/world.py must be byte-for-byte deterministic"
    assert before_log == after_log, "regenerating the repo must produce identical commit SHAs"


# ---------------------------------------------------------------- servers expose the new fields end to end
@pytest.mark.asyncio
async def test_servers_expose_new_fields(world):
    from crystal.mcp_client import ServerPool

    cascade = next(i for i in world["incidents"] if "child_incidents" in i)
    child = next(i for i in world["incidents"] if i.get("parent_incident") == cascade["id"])
    repeat_second = next(i for i in world["incidents"] if "repeat_of" in i)
    postmortem = next(p for p in world["confluence_pages"] if "postmortem" in p.get("labels", []))

    async with ServerPool() as pool:
        issue = await pool.call("jira", "jira_get_issue", {"issue_key": cascade["jira"]["key"]})
        assert issue["fields"]["issuelinks"], "jira_get_issue should expose issuelinks"

        pd = await pool.call("pagerduty", "get_incident", {"incident_id": child["pagerduty"]["id"]})
        assert pd["related_incidents"] == [cascade["pagerduty"]["id"]]

        page = await pool.call("confluence", "confluence_get_page", {"page_id": postmortem["id"]})
        assert page["links"] and page["parent_id"]

        comments = await pool.call("jira", "jira_get_issue_comments", {"issue_key": repeat_second["jira"]["key"]})
        assert comments["comments"] == repeat_second["jira"]["comments"]

        deploy = world["deploys"][0]
        thread = await pool.call("slack", "conversations_replies", {"channel_id": world["channels"]["#deploys"]["id"], "thread_ts": deploy["thread_ts"]})
        assert thread["messages"] and thread["messages"][0]["text"] == deploy["message"]
