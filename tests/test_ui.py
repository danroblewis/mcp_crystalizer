"""The web UI: flow catalog with badges, the run dossier (headline, evidence groups, diagram, highlights, collapsed
how-panel), the agent-trace dossier, the run JSON endpoint and the "what was missing?" feedback form.

Uses starlette's TestClient without the lifespan, so no MCP server is started."""
import json
import re
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from crystal.app import diagram, dossier, provenance
from crystal.app import main as ui
from crystal.flow.runner import load_flow

FIXTURE = Path(__file__).parent / "fixtures" / "run_auth122.json"
RUN_ID = json.loads(FIXTURE.read_text())["run_id"]
TRACE = "scripted-PAY-101-v0"


@pytest.fixture
def client(fresh_home, monkeypatch):
    """The UI over a private state dir: the sim's flows and traces seeded, the fixture run copied into runs/."""
    shutil.copy(FIXTURE, fresh_home.runs / f"{RUN_ID}.json")
    if hasattr(ui.app.state, "workspace"):
        monkeypatch.delattr(ui.app.state, "workspace")
    c = TestClient(ui.app)
    c.state = fresh_home
    return c


def test_index_lists_flows_with_badges_and_counters(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Investigate a Jira ticket" in body and 'href="/flows/investigate-jira-ticket"' in body
    assert "author: candidate" in body                      # author intent badge
    assert "effective: candidate" in body                   # lifecycle badge from describe()
    assert "clean runs" in body and "complaints" in body    # counters
    assert 'class="cards"' in body                          # card grid
    assert "Agent traces" in body                           # nav to /traces
    assert 'class="wsname">sim<' in body and "examples/sim" in body    # the workspace name and root at the top
    assert 'id="getting-started"' not in body


def test_run_dossier_headline_evidence_diagram_highlights(client):
    r = client.get(f"/runs/{RUN_ID}")
    assert r.status_code == 200
    body = r.text
    # headline first: what, service, when, owners, changed-before, repeat
    lede = body.split('<p class="lede">', 1)[1].split("</p>", 1)[0]
    assert "AuthTokenExpired" in lede and "auth-service" in lede
    assert "resolved after 2h" in lede
    assert "@identity" in lede
    assert "25acaf6a47" in lede
    assert "repeat of AUTH-117" in lede
    assert "<dt>Runbook says</dt>" in body
    # coverage line
    m = re.search(r"Found (\d+) of (\d+)", body)   # 9 of 9 with the flow card, 13 of 13 with the per-step fallback
    assert m and m.group(1) == m.group(2) and int(m.group(2)) >= 9 and "expected things" in body
    # evidence grouped by information type, not by step; fan-out logs in one group with a per-trace-id summary
    for g in ("g-issue", "g-incident", "g-message", "g-metric", "g-log", "g-commit", "g-code", "g-owners", "g-page"):
        assert f'id="{g}"' in body
    assert body.index('id="g-issue"') < body.index('id="g-log"')
    assert '<table class="logsum">' in body and "distinct pods" in body
    assert 'id="s-logs"' in body and body.count('id="s-logs"') == 1
    # the research diagram: an inline SVG DAG with one node per step, edges from the YAML, fan-outs stacked
    assert '<svg class="dag"' in body
    assert 'data-step="logs"' in body and 'href="#s-logs"' in body
    assert body.count('class="edge') >= 14
    assert "3 items, 2 hit" in body                         # logs fan-out summary on the node
    # an edge is a VALUE moving, and says which value -- not how many results came back
    assert body.count('class="edge carries"') >= 14         # every edge carries a labelled value
    for value_type in (">trace_id</text>", ">error_class</text>", ">service</text>", ">window</text>"):
        assert value_type in body, value_type
    assert 'aria-label="research flow: 15 steps; values moving: ' in body
    assert '<a href="#s-logs" class="edge-link" data-step="logs">' in body        # the label jumps to the consumer
    assert "trace_id 1589cbb8cf4f9818fe9…" in body and "→ query" in body          # the tip names the value and the argument
    # extracted values highlighted with their PROVENANCE: what it is, who found it, WHO USED IT and as what
    carried = ('<mark class="v used" title="trace_id, extracted by \u201cJira ticket\u201d, \u201cSlack discussion\u201d, '
               '\u201cSlack thread replies\u201d as trace_ids, used by \u201cLogs per trace id\u201d as query">'
               '1589cbb8cf4f9818fe9aaf2485aad023'
               '<a class="hop" href="#s-logs" title="used by \u201cLogs per trace id\u201d as query">9</a></mark>')
    assert carried in body
    assert '<mark class="v used" title="jira_key, run input key, used by \u201cJira ticket\u201d as issue_key' in body
    # a value nothing ever used is marked differently, and says so
    assert '<mark class="v det" title="team, extracted by \u201cOwning team (CODEOWNERS)\u201d as teams, detected, never used in a later call">@identity</mark>' in body
    assert '"v det"' in body and '"v used"' in body and body.count('class="hop"') >= 5
    # large text folds to excerpts with expanders
    assert '<details class="more"><summary>show ' in body
    # sparkline with the incident-start rule
    assert '<svg class="spark"' in body and "incident start" in body
    # feedback form reframed
    assert "What was missing?" in body and 'action="/runs/' in body


def test_how_panel_collapsed_by_default(client):
    body = client.get(f"/runs/{RUN_ID}").text
    assert '<details class="how">' in body
    assert '<details class="how" open' not in body
    how = body.split('<details class="how">', 1)[1]
    assert "How this was gathered" in how and "jira.jira_get_issue" in how and "ladder:" in how
    # tool calls live only inside the how panel
    assert "logz.search_logs" not in body.split('<details class="how">', 1)[0]


def test_run_json_and_missing_run(client):
    r = client.get(f"/runs/{RUN_ID}/json")
    assert r.status_code == 200 and json.loads(r.text)["run_id"] == RUN_ID
    assert client.get("/runs/does-not-exist").status_code == 404
    assert client.get("/runs").status_code == 200 and RUN_ID in client.get("/runs").text


def test_feedback_queues_complaint_and_demotes(client):
    r = client.post(f"/runs/{RUN_ID}/feedback", data={"text": "no deploy diff", "helpful": "no"}, follow_redirects=False)
    assert r.status_code == 303 and "Recorded" in r.headers["location"]
    lines = [json.loads(l) for l in client.state.feedback.read_text().splitlines()]
    assert lines[-1]["run_id"] == RUN_ID and lines[-1]["helpful"] is False
    assert "effective: draft" in client.get("/").text or "tripped" in client.get("/").text


def test_trace_list_and_trace_dossier(client):
    r = client.get("/traces")
    assert r.status_code == 200 and f'href="/traces/{TRACE}"' in r.text
    r = client.get(f"/traces/{TRACE}")
    assert r.status_code == 200
    body = r.text
    assert "Agent trace" in body and "jira_issue" in body
    assert '<p class="lede">' in body and "PAY-101" in body
    assert '<svg class="dag"' in body and 'data-step="c1"' in body
    assert "calls</span>" in body and "returned results" in body
    # values carried forward are highlighted with where they came from AND where they went
    assert ('<mark class="v used" title="trace_id, extracted by \u201cslack: conversations replies\u201d as trace_id, '
            'used by \u201clogz: search logs\u201d as query">') in body
    assert 'class="hop" href="#s-c' in body
    # the trace diagram labels its edges with those values too, not with result counts
    assert body.count('class="edge carries"') >= 6 and 'class="edge-link"' in body
    assert ">trace_id</text>" in body and ">error_class</text>" in body
    assert 'id="g-log"' in body and 'id="g-issue"' in body
    assert '<details class="how">' in body
    assert client.get("/traces/nope").status_code == 404
    assert client.get(f"/traces/{TRACE}/json").status_code == 200


def test_flow_edges_from_yaml_templates():
    flow = load_flow("investigate-jira-ticket")
    edges = set(diagram.flow_edges(flow))
    assert ("inputs", "issue") in edges and ("issue", "slack") in edges and ("slack", "thread") in edges
    assert ("thread", "logs") in edges and ("slack", "logs") in edges and ("issue", "logs") in edges   # forEach expression
    assert ("code", "owners") in edges                                                                   # forEach on an extract
    assert ("thread", "suspect_commit") in edges
    assert not any(a == "catalog" for a, _ in edges)


def test_marker_excerpt_folds_unmatched_lines():
    m = dossier.Marker({"abc123def456": ["extracted by t as id"]})
    text = "\n".join(["line %d" % i for i in range(20)] + ["hit abc123def456 here"] + ["tail %d" % i for i in range(20)])
    out = str(m.excerpt(text))
    assert '<mark class="v det" title="extracted by t as id">abc123def456</mark>' in out
    assert out.count("show 18 more lines") == 2     # 20 lines before, 20 after; 2 lines of context are kept on each side
    assert out.count("<details") == 2
    assert out.count('<div class="ln">') == 41


def test_the_toggle_dims_the_values_nothing_used(client):
    """The noisy majority of marks are detections. Hiding them is a per-run toggle with no JavaScript: a checkbox
    and two CSS rules, so it works in a page with scripting off."""
    body = client.get(f"/runs/{RUN_ID}").text
    assert '<input type="checkbox" id="dimdet" class="vtog">' in body
    assert 'for="dimdet"' in body and "values nothing used" in body
    css = body.split("<style>", 1)[1].split("</style>", 1)[0]
    dim = css.split(".vtog:checked~* mark.det,", 1)[1]
    assert dim.startswith("main:has(.vtog:checked) mark.det{")
    rule = dim.split("{", 1)[1].split("}", 1)[0]
    assert "background:transparent" in rule and "border-bottom-color:transparent" in rule
    assert "mark.used" not in rule                       # only the detections are dimmed
    assert "dimdet" not in body.split("<script>", 1)[1]  # no JavaScript involved
    # and the two states are legended, with a live sample of each mark
    key = body.split('<div class="vkey">', 1)[1].split("</div>", 1)[0]
    assert 'class="v used"' in key and 'class="v det"' in key and "superscript" in key


def test_provenance_finds_the_call_that_used_a_value(client):
    """The deterministic half: a value is CARRIED when it turns up in a later call's rendered args, and the
    argument it lands in names the edge. Anything else was merely detected."""
    record = {"inputs": {"key": "PAY-101"},
              "steps": [{"id": "a", "title": "Ticket", "tool": "jira.jira_get_issue", "args": {"issue_key": "PAY-101"},
                         "extracts": {"trace_ids": ["9f2b1c0a4e5d6f708192a3b4c5d6e7f8"], "pods": ["pay-api-77c9-xyzab"]}},
                        {"id": "b", "title": "Logs", "tool": "logz.search_logs",
                         "items": [{"item": "9f2b1c0a4e5d6f708192a3b4c5d6e7f8",
                                    "args": {"query": "trace_id:9f2b1c0a4e5d6f708192a3b4c5d6e7f8"}}]}]}
    prov = provenance.analyse(record, None)
    trace = prov.values["9f2b1c0a4e5d6f708192a3b4c5d6e7f8"]
    assert trace.used and trace.kind == "trace_id"
    assert [(u.step, u.arg) for u in trace.uses] == [("b", "query")]
    assert "used by “Logs” as query" in trace.title()
    pod = prov.values["pay-api-77c9-xyzab"]
    assert not pod.used and "never used" in pod.title()
    # the input reached the first call; a value never carried produces no edge
    assert prov.edges()[("inputs", "a")][0]["label"] == "jira_key"
    assert prov.edges()[("a", "b")] == [{"label": "trace_id", "name": "trace_ids", "value_type": "ids:trace_id",
                                         "values": ["9f2b1c0a4e5d6f708192a3b4c5d6e7f8"], "args": ["query"]}]
    assert ("a", "b") in prov.edges() and len(prov.edges()) == 2
    # reported in the dataflow miner's own vocabulary, so the page and the miner agree about the run
    edge = [e for e in prov.call_edges if e.target == "logz.search_logs"][0]
    assert (edge.source, edge.value_type, edge.arg, edge.src_call, edge.tgt_call) == \
        ("jira.jira_get_issue", "ids:trace_id", "query", 0, 1)


def test_value_labels_come_from_the_flow_yaml_too():
    """An edge whose value this run never produced is still named: the YAML says what was meant to travel."""
    flow = load_flow("investigate-jira-ticket")
    refs = set(diagram.template_value_refs(flow))
    assert ("issue", "slack", "error_sig") in refs and ("issue", "logs", "window") in refs
    assert ("slack", "thread", "channel_id") in refs and ("slack", "thread", "thread_ts") in refs
    assert ("code", "owners", "files") in refs                       # a forEach expression
    assert ("inputs", "issue", "key") in refs
    assert not any(a == "catalog" for a, _, _ in refs)
    assert provenance.display_type(provenance.value_type_of(flow, "issue", "trace_ids", ""), "trace_ids") == "trace_id"
    assert provenance.display_type(provenance.value_type_of(flow, "inputs", "key", ""), "key") == "jira_key"


def test_a_versioned_flow_opens_from_the_catalog_and_by_its_own_name(tmp_path):
    """`refine`/`author` write `<name>.v<N>.yaml` while the flow's `name:` stays unversioned. The catalog must link
    to something that opens, and a link built from the bare name (a run record, `refined_from`) must resolve to the
    newest version instead of 500ing, which is what happened on a real machine."""
    import yaml as _yaml
    from crystal.flow.runner import flow_path, list_flows, load_flow

    d = tmp_path / "flows"
    d.mkdir()
    for v in (1, 2):
        (d / f"probe.v{v}.yaml").write_text(_yaml.safe_dump(
            {"name": "probe", "title": f"Probe v{v}", "status": "draft", "inputs": {}, "steps": []}))
    assert flow_path("probe", d).name == "probe.v2.yaml"          # newest version wins
    assert load_flow("probe", d)["title"] == "Probe v2"
    slugs = sorted(f["slug"] for f in list_flows(d))
    assert slugs == ["probe.v1", "probe.v2"]                       # each version addressable on its own
    assert load_flow("probe.v1", d)["title"] == "Probe v1"


def test_candidates_page_shows_the_dataflow_and_can_fall_back_to_sequences(tmp_path, monkeypatch):
    """The page must show how information moved, not the old tool-chain list, and induce with the same miner."""
    from starlette.testclient import TestClient
    from crystal.app.main import app
    from crystal import state as state_mod

    with TestClient(app) as client:
        page = client.get("/candidates")
        assert page.status_code == 200
        body = page.text
        assert "How information moved" in body and 'name="miner" value="dataflow"' in body
        seq = client.get("/candidates?miner=sequences")
        assert seq.status_code == 200 and "Tool-call sequences" in seq.text
        assert 'name="miner" value="sequences"' in seq.text


def test_candidates_page_analyses_in_the_background_instead_of_holding_the_request(tmp_path, monkeypatch):
    """A workspace with thousands of episodes must not mine inside the request: behind a tunnel that just times
    out (a real 524). The page shows progress and a JSON endpoint reports it."""
    from starlette.testclient import TestClient
    from crystal.app import routes_import
    from crystal.app.main import app

    monkeypatch.setattr(routes_import, "cache_status",
                        lambda *a, **k: {"episodes": 900, "cached": 120, "missing": 780})
    called = []
    monkeypatch.setattr(routes_import, "mine_dataflow", lambda *a, **k: called.append(1) or [])
    with TestClient(app) as client:
        page = client.get("/candidates")
        assert page.status_code == 200
        assert "Analysing this workspace" in page.text and "120" in page.text and "900" in page.text
        status = client.get("/candidates/analysis").json()
        assert status["episodes"] == 900 and status["missing"] == 780
        # the sequence view needs no analysis and stays available
        assert "Tool-call sequences" in client.get("/candidates?miner=sequences").text


def test_candidates_are_mined_once_until_the_index_changes(monkeypatch):
    """Mining a large warm workspace takes seconds; reloading the page must not repeat it, and a new import must."""
    from starlette.testclient import TestClient
    from crystal.app import routes_import
    from crystal.app.main import app

    calls = []
    monkeypatch.setattr(routes_import, "cache_status", lambda *a, **k: {"episodes": 3, "cached": 3, "missing": 0})
    monkeypatch.setattr(routes_import, "mine_dataflow", lambda *a, **k: calls.append(1) or [])
    routes_import._MINED.clear()
    stamps = [(1, 1.0), (1, 1.0), (2, 2.0)]
    monkeypatch.setattr(routes_import, "_index_stamp", lambda st: stamps[min(len(calls), 2)] if False else stamps.pop(0))
    with TestClient(app) as client:
        assert client.get("/candidates").status_code == 200
        assert client.get("/candidates").status_code == 200      # same index: served from the memo
        assert len(calls) == 1
        assert client.get("/candidates").status_code == 200      # index changed: mined again
        assert len(calls) == 2
    routes_import._MINED.clear()
