"""The web UI: flow catalog with badges, the run dossier (headline, evidence groups, diagram, highlights, collapsed
how-panel), the agent-trace dossier, the run JSON endpoint and the "what was missing?" feedback form.

Uses starlette's TestClient without the lifespan, so no MCP server is started."""
import json
import re
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from crystal.app import diagram, dossier
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
    assert body.count('class="edge"') >= 14
    assert "3 items, 2 hit" in body                         # logs fan-out summary on the node
    # extracted values highlighted inside content, with the extracting step in the title
    assert '<mark title="extracted by issue as trace_ids; extracted by slack as trace_ids; extracted by thread as trace_ids">1589cbb8cf4f9818fe9aaf2485aad023</mark>' in body
    assert '<mark title="input key">AUTH-122</mark>' in body
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
    # values carried forward are highlighted with their source call
    assert '<mark title="carried c1 → c2' in body or 'carried inputs → c1' in body
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
    assert '<mark title="extracted by t as id">abc123def456</mark>' in out
    assert out.count("show 18 more lines") == 2     # 20 lines before, 20 after; 2 lines of context are kept on each side
    assert out.count("<details") == 2
    assert out.count('<div class="ln">') == 41
