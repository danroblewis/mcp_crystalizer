"""Capturing flows from past sessions: the transcript parser (tool_use/tool_result pairing, string and content-block
results, injected text, the hook's Read/Bash rule), episode splitting, idempotent import into a temp state dir, the
mining of common behaviours over the sim traces (the jira -> slack -> ... sequence is the top candidate) and
`candidates induce` producing a flow that runs in-process against the sim."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from crystal import state as state_mod
from crystal.cli import main as cli_main
from crystal.flow.runner import FlowRunner, load_flow
from crystal.induce.mining import episodes, induce_candidate, load_episodes, mine, write_candidate_flow
from crystal.mcp_client import ServerPool
from crystal.trace.store import load_session, load_sessions
from crystal.trace.transcripts import import_transcripts, importable, parse_transcript, prompt_inputs
from tests.conftest import SIM

FIXTURE = Path(__file__).parent / "fixtures" / "transcript.jsonl"
FIXTURE_CWD = "/Users/dev/acme-api"
SID = "fixture-session-0001"


def _transcripts_dir(tmp_path: Path, cwd: Path | str, name: str = SID, project: str = "proj") -> Path:
    """A transcripts tree like ~/.claude/projects with the fixture rewritten to `cwd` and session id `name`."""
    base = tmp_path / "transcripts"
    d = base / project
    d.mkdir(parents=True, exist_ok=True)
    text = FIXTURE.read_text().replace(FIXTURE_CWD, str(cwd)).replace(SID, name)
    (d / f"{name}.jsonl").write_text(text)
    return base


# ---------------------------------------------------------------- parser
def test_parse_pairs_calls_with_results_and_prompts():
    tx = parse_transcript(FIXTURE)
    assert tx.session_id == SID and tx.cwd == FIXTURE_CWD and tx.lines == 24
    assert [p["text"][:12] for p in tx.prompts] == ["Investigate ", "now check pa"]      # meta and injected text are not prompts
    tools = [(c.server, c.tool) for c in tx.calls]
    assert tools == [("jira", "jira_get_issue"), ("claude-code", "Bash"), ("slack", "conversations_search_messages"),
                     ("pagerduty", "list_incidents"), ("claude-code", "Read"), ("pagerduty", "get_incident")]
    assert tx.skipped_tools == 2                       # the Bash before the first MCP call, and the Edit
    jira, bash, slack, pd, read, pd2 = tx.calls
    assert jira.output["fields"]["components"][0]["name"] == "payments"       # content blocks -> JSON parsed
    assert jira.output_text.startswith("{") and jira.tool_use_id == "toolu_02" and jira.ts == "2026-08-27T13:50:20.000Z"
    assert pd.output == {"incidents": [{"id": "Q1PAY", "title": "payments 5xx above 2%", "status": "resolved"}]}   # string result -> JSON parsed
    assert bash.output == "services/payments/gateway.py:42:        raise PaymentGatewayTimeout(upstream)"    # string result, kept as text
    assert read.output.startswith("     1\tclass PaymentGatewayTimeout")
    assert pd2.is_error and pd2.output == "Error: incident Q1PAY not found"
    assert [c.prompt_index for c in tx.calls] == [0, 0, 0, 1, 1, 1]
    assert jira.prompt.startswith("Investigate Jira ticket PAY-108") and pd.prompt.startswith("now check pagerduty")
    assert tx.servers == {"pagerduty": 2, "jira": 1, "slack": 1}
    assert tx.episode_prompts() == [0, 1]
    assert tx.results_by_prompt[0].startswith("PAY-108 is a PaymentGatewayTimeout") and tx.result.startswith("PagerDuty incident Q1PAY")
    assert prompt_inputs(tx.first_prompt) == {"prompt": tx.first_prompt, "jira_key": "PAY-108"}


def test_preview_keeps_claude_code_results_short(tmp_path):
    big = "x" * 5000
    text = FIXTURE.read_text().replace("services/payments/gateway.py:42:        raise PaymentGatewayTimeout(upstream)", big)
    p = tmp_path / "t.jsonl"
    p.write_text(text)
    tx = parse_transcript(p)
    bash = next(c for c in tx.calls if c.tool == "Bash")
    assert len(bash.output) < 400 and bash.output.endswith("(5000 chars)")


# ---------------------------------------------------------------- import + episodes
def test_import_writes_hook_format_episodes_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    ws = tmp_path / "acme-api"
    ws.mkdir()
    other = tmp_path / "gone-project"                       # a cwd that no longer exists
    base = _transcripts_dir(tmp_path, ws)
    _transcripts_dir(tmp_path, other, name="fixture-session-0002", project="gone")
    rep = import_transcripts(base, workspace_root=ws, dry_run=True)
    assert rep["found"] == 2 and rep["with_mcp"] == 2 and rep["imported"] == 1 and rep["episodes"] == 2
    assert {r["status"] for r in rep["rows"]} == {"would import", "other workspace"}
    st = state_mod.state_for(ws)
    assert not st.dir.exists()                              # a dry run writes nothing

    rep = import_transcripts(base, workspace_root=ws)
    assert rep["imported"] == 1 and rep["skipped"] == 0 and rep["episodes"] == 2 and rep["servers"] == {"pagerduty": 4, "jira": 2, "slack": 2}
    files = sorted(p.name for p in st.traces.glob("*.jsonl"))
    assert files == [f"{SID}-e1.jsonl", f"{SID}-e2.jsonl", f"{SID}.jsonl"]
    whole = load_session(st.traces / f"{SID}.jsonl")
    assert whole.source == "transcript" and whole.meta["trigger"] == "prompt" and whole.meta["episodes"] == 2
    assert whole.meta["inputs"] == {"prompt": whole.prompts[0]["text"], "jira_key": "PAY-108"}
    assert whole.meta["cwd"] == str(ws) and whole.meta["cwd_exists"] is True and whole.meta["claude_session_id"] == SID
    assert len(whole.prompts) == 2 and whole.prompt.startswith("Investigate Jira ticket PAY-108")
    assert [f"{c['server']}.{c['tool']}" for c in whole.calls] == ["jira.jira_get_issue", "claude-code.Bash", "slack.conversations_search_messages",
                                                                    "pagerduty.list_incidents", "claude-code.Read", "pagerduty.get_incident"]
    assert [c["seq"] for c in whole.calls] == [1, 2, 3, 4, 5, 6] and whole.calls[0]["tool_use_id"] == "toolu_02"
    assert whole.calls[0]["output"]["key"] == "PAY-108" and whole.calls[3]["output"]["incidents"][0]["id"] == "Q1PAY"
    assert whole.calls[5]["is_error"] is True and whole.result.startswith("PagerDuty incident Q1PAY")
    raw = json.loads((st.traces / f"{SID}.jsonl").read_text().splitlines()[0])
    assert {"ts", "session_id", "source", "kind"} <= set(raw) and raw["kind"] == "meta"       # the hook's record shape
    e1, e2 = load_session(st.traces / f"{SID}-e1.jsonl"), load_session(st.traces / f"{SID}-e2.jsonl")
    assert e1.meta["parent_session"] == SID and e1.meta["episode"] == "e1" and e1.meta["agent"] == "main" and e1.meta["prompt"].startswith("Investigate Jira ticket")
    assert e2.meta["parent_session"] == SID and e2.meta["prompt"] == "now check pagerduty for the incident on the payments service"
    assert e2.meta["inputs"] == {"prompt": e2.meta["prompt"]}
    assert [c["tool"] for c in e1.calls] == ["jira_get_issue", "Bash", "conversations_search_messages"] and [c["seq"] for c in e1.calls] == [1, 2, 3]
    assert [c["tool"] for c in e2.calls] == ["list_incidents", "Read", "get_incident"]
    assert e1.result.startswith("PAY-108 is a") and e2.result.startswith("PagerDuty incident")
    # mining sees the episodes, never the whole session as well
    assert sorted(e.session_id for e in episodes(load_sessions(st.traces))) == [f"{SID}-e1", f"{SID}-e2"]

    before = {p.name: p.read_text() for p in st.traces.glob("*.jsonl")}
    rep = import_transcripts(base, workspace_root=ws)
    assert rep["imported"] == 0 and rep["skipped"] == 1 and any(r["status"] == "already imported" for r in rep["rows"])
    assert {p.name: p.read_text() for p in st.traces.glob("*.jsonl")} == before                 # nothing rewritten
    rep = import_transcripts(base, workspace_root=ws, force=True)
    assert rep["imported"] == 1 and sorted(p.name for p in st.traces.glob("*.jsonl")) == files
    assert [r for r in importable(base, ws)][0]["status"] == "already imported"

    # --all: every project into its own workspace; a missing cwd still imports, flagged
    rep = import_transcripts(base, all_projects=True)
    assert rep["imported"] == 1 and rep["skipped"] == 1
    gone = state_mod.state_for(other)
    row = next(r for r in rep["rows"] if r["session_id"] == "fixture-session-0002")
    assert row["status"] == "imported" and row["cwd_exists"] is False and row["workspace"] == gone.slug
    s2 = load_session(gone.traces / "fixture-session-0002.jsonl")
    assert s2.meta["cwd_exists"] is False and len(s2.calls) == 6
    assert not (ws / ".mcp-explorer").exists() and sorted(p.name for p in ws.iterdir()) == []   # the workspace dir is untouched


def test_cli_import_table(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_EXPLORER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM))   # main() activates a workspace via os.environ; restore it
    ws = tmp_path / "acme-api"
    ws.mkdir()
    base = _transcripts_dir(tmp_path, ws)
    assert cli_main(["--workspace", str(ws), "import", "--transcripts", str(base), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would import" in out and "pagerduty=2" in out and "1 transcripts found" in out and "would import 1" in out
    assert not state_mod.state_for(ws).traces.glob("*.jsonl").__next__ or not list(state_mod.state_for(ws).traces.glob("*.jsonl"))
    assert cli_main(["--workspace", str(ws), "import", "--transcripts", str(base)]) == 0
    assert "imported" in capsys.readouterr().out and len(list(state_mod.state_for(ws).traces.glob("*.jsonl"))) == 3
    assert cli_main(["--workspace", str(ws), "import", "--transcripts", str(tmp_path / "nowhere")]) == 1


# ---------------------------------------------------------------- mining
@pytest.fixture(scope="module")
def sim_candidates():
    eps = load_episodes(SIM / "traces")
    if len(eps) < 30:
        pytest.skip("the sim traces are missing (run examples/sim/world.py)")
    return eps, mine(eps)


def test_mining_top_candidate_is_the_jira_sequence(sim_candidates):
    eps, cands = sim_candidates
    assert len(cands) >= 5
    top = cands[0]
    assert top.rank == 1
    assert top.seq[:3] == ("jira.jira_get_issue", "slack.conversations_search_messages", "slack.conversations_replies")
    assert top.support >= 10 and top.length >= 10
    assert "logz.search_logs*" in top.display                 # consecutive logz calls collapsed into one fan-out marker
    assert top.saving > top.support * top.length              # the fan-out covers more calls than steps
    assert all(sid.startswith("scripted-") for sid in top.episode_ids)
    assert any("Jira" in p or "PAY-" in p or "ticket" in p.lower() for p in top.prompts) or not top.prompts
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True) and all(c.support >= 2 and c.length >= 2 for c in cands)
    # closed: no reported pattern is a sub-window of a longer one with the same support
    for c in cands:
        for d in cands:
            if d is not c and d.support == c.support and d.length > c.length:
                assert not any(d.seq[i:i + c.length] == c.seq for i in range(d.length - c.length + 1)), (c.seq, d.seq)


def test_mining_needs_two_episodes_sharing_two_calls():
    from crystal.trace.store import Session

    def sess(sid, tools):
        return Session(session_id=sid, source="scripted", meta={"trigger": "t", "inputs": {}},
                       calls=[{"server": t.split(".")[0], "tool": t.split(".")[1], "input": {}, "output": {}} for t in tools])
    eps = episodes([sess("a", ["x.one", "x.two", "x.two", "claude-code.Bash", "y.three"]), sess("b", ["x.one", "x.two", "y.three"]), sess("c", ["z.only"])])
    assert [e.tokens for e in eps] == [["x.one", "x.two", "y.three"], ["x.one", "x.two", "y.three"], ["z.only"]]
    assert eps[0].fanout == [False, True, False] and eps[0].spans == [(0, 1), (1, 3), (3, 4)]
    cands = mine(eps)
    assert len(cands) == 1 and cands[0].seq == ("x.one", "x.two", "y.three") and cands[0].support == 2
    assert cands[0].display == ["x.one", "x.two*", "y.three"] and cands[0].saving == 4 + 3
    assert mine([eps[0]]) == []


def test_candidates_induce_writes_a_runnable_draft_flow(fresh_home, capsys):
    cands = mine(load_episodes(fresh_home.traces))
    top = cands[0]
    out, report = write_candidate_flow(top, "mined-jira-ticket", fresh_home.flows)
    assert out == fresh_home.flows / "mined-jira-ticket.yaml" and out.exists()
    flow = load_flow("mined-jira-ticket", fresh_home.flows)
    assert flow["status"] == "draft" and flow["name"] == "mined-jira-ticket"
    assert len(flow["induced_from"]) == top.support and flow["mined"]["support"] == top.support
    assert flow["inputs"]["key"]["type"] == "jira_key" and flow["steps"][0]["tool"] == "jira.jira_get_issue"
    assert flow["steps"][0]["args"] == {"issue_key": "{{ inputs.key }}"}
    assert {st["tool"] for st in flow["steps"]} == set(top.seq)
    assert report["candidate"]["rank"] == 1 and report["unresolved"] == {}

    async def go():
        async with ServerPool() as pool:
            return await FlowRunner(pool).run(flow, {"key": "STF-116"}, save=False)   # a ticket no episode traced
    rec = asyncio.run(go())
    assert rec["status"] == "ok"
    optional = {st["id"] for st in flow["steps"] if st.get("optional")}
    errors = [(st["id"], st.get("error")) for st in rec["steps"] if st.get("error") and st["id"] not in optional]
    assert not errors, errors
    steps = {st["id"]: st for st in rec["steps"]}
    assert steps[flow["steps"][0]["id"]]["hits"] >= 1
    for sid in flow["tests"][0]["expect"]:                      # every step that always hit in the traces hits again
        assert (steps[sid].get("hits") or 0) >= 1, sid

    # the CLI path: list, then induce by rank
    assert cli_main(["candidates", "--top", "3"]) == 0
    listing = capsys.readouterr().out
    assert listing.splitlines()[1].strip().startswith("1") and "jira.jira_get_issue -> slack.conversations_search_messages" in listing
    assert cli_main(["candidates", "induce", "1", "--name", "mined-cli"]) == 0
    assert "induced mined-cli from candidate #1" in capsys.readouterr().out and (fresh_home.flows / "mined-cli.yaml").exists()
    assert cli_main(["candidates", "induce", "999"]) == 1


def test_induce_candidate_slices_each_episode_to_the_span(sim_candidates):
    eps, cands = sim_candidates
    c = next(c for c in cands if c.length == 2 and c.seq[0] == "confluence.confluence_search")
    flow, report = induce_candidate(c, "mined-two")
    assert [st["tool"] for st in flow["steps"]] == list(c.seq) and report["sessions"] == c.support


# ---------------------------------------------------------------- UI
@pytest.fixture
def client(fresh_home, monkeypatch, tmp_path):
    import crystal.app.main as ui
    monkeypatch.setenv("MCP_EXPLORER_TRANSCRIPTS", str(_transcripts_dir(tmp_path, SIM)))
    if hasattr(ui.app.state, "workspace"):
        monkeypatch.delattr(ui.app.state, "workspace")
    return TestClient(ui.app)


def test_import_page_lists_and_imports(client, fresh_home):
    body = client.get("/import").text
    assert "Import past sessions" in body and SID[:12] in body and "importable" in body and "Investigate Jira ticket PAY-108" in body
    r = client.post("/import", data={}, follow_redirects=False)
    assert r.status_code == 303 and "Imported%201%20session%20%282%20episodes%29" in r.headers["location"]
    assert (fresh_home.traces / f"{SID}.jsonl").exists() and (fresh_home.traces / f"{SID}-e2.jsonl").exists()
    body = client.get("/import").text
    assert "already imported" in body and f'href="/traces/{SID}"' in body
    assert client.get(f"/traces/{SID}-e1").status_code == 200        # an episode renders as a trace dossier


def test_candidates_page_and_induce(client, fresh_home):
    body = client.get("/candidates").text
    assert "Common behaviours" in body and "jira.jira_get_issue" in body and "Induce as flow" in body and 'id="c1"' in body
    assert "episodes</span>" in body and 'href="/candidates"' in body      # nav link in base.html
    r = client.post("/candidates/1/induce", data={"name": "Mined From UI"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/flows/mined-from-ui"
    assert (fresh_home.flows / "mined-from-ui.yaml").exists()
    assert client.get("/flows/mined-from-ui").status_code == 200
    assert client.post("/candidates/999/induce", data={"name": "x"}, follow_redirects=False).headers["location"].startswith("/candidates?msg=")
