"""The dataflow miner: episodes as sets of typed information edges, mined into bindable candidates.

Everything here is deterministic and offline: the sim's recorded traces, synthetic in-memory sessions, and one
in-process run of an induced flow against the sim servers (CRYSTAL_INPROCESS=1, CRYSTAL_NO_AGENT=1 from conftest).
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from crystal import state as state_mod
from crystal.induce import dataflow as df
from crystal.induce.dataflow import CONSTANT, PROMPT, Edge, EdgeCache, EpisodeGraph
from crystal.induce.mining import bindability, episodes as make_episodes, induce_candidate
from crystal.trace.store import Session

from tests.conftest import SIM, SIM_STATE

JIRA_ROOT = Edge(PROMPT, "ids:jira_key", "jira.jira_get_issue")


# ---------------------------------------------------------------- the sim's investigation graph
@pytest.fixture(scope="module")
def sim_candidates():
    graphs = df.episode_graphs(SIM_STATE.traces)
    return graphs, df.mine(graphs)


def test_top_candidate_is_the_investigation_graph_rooted_at_the_ticket(sim_candidates):
    _, cands = sim_candidates
    top = cands[0]
    assert JIRA_ROOT in top.edges, top.display
    assert top.rooted and top.root_value_type == "ids:jira_key"
    assert top.support == 15, top.support          # the sim's 15 scripted jira sessions (5 tickets x 3 variants)
    assert set(top.servers) >= {"jira", "slack", "logz", "chronosphere", "code", "git"}, top.servers
    # one graph, not three: every edge hangs together through the tool nodes
    assert len(df._components(set(top.edges))) == 1, top.display


def test_top_candidate_is_bound_by_construction(sim_candidates):
    """Every edge is a binding the inducer really made, so `induce` can derive the arguments again. A LOW score
    here is not a weak candidate -- it means the edge extraction claimed a binding the inducer does not make, i.e.
    a bug in crystal.induce.dataflow._sources_of."""
    _, cands = sim_candidates
    assert bindability(cands[0]) == pytest.approx(1.0)


def test_induced_flow_runs_against_the_sim_on_an_unseen_ticket(sim_candidates):
    from crystal.flow.runner import FlowRunner
    from crystal.mcp_client import ServerPool
    _, cands = sim_candidates
    flow, report = induce_candidate(cands[0], "mined-dataflow-jira")
    assert not report.get("unresolved"), report["unresolved"]
    assert flow["inputs"]["key"]["type"] == "jira_key"

    async def go():
        async with ServerPool() as pool:
            return await FlowRunner(pool).run(flow, {"key": "STF-116"}, save=False)   # not in any trace

    rec = asyncio.run(go())
    assert rec["status"] == "ok"
    errors = {s["id"]: s.get("error") for s in rec["steps"] if s.get("error")}
    assert not errors, errors
    assert sum(s.get("hits") or 0 for s in rec["steps"]) > 0


def test_view_and_rendering(sim_candidates):
    _, cands = sim_candidates
    view = cands[0].view()
    assert json.dumps(view)          # the --json shape is serialisable
    assert str(JIRA_ROOT) in view["edges"]
    assert view["root_value_type"] == "ids:jira_key"
    assert view["dataflow"][0].startswith("prompt --ids:jira_key--> jira.jira_get_issue")
    assert "prompt --ids:jira_key--> jira.jira_get_issue" in df.format_candidates(cands[:1])


# ---------------------------------------------------------------- synthetic episodes
def _call(server, tool, input, output):
    return {"kind": "call", "server": server, "tool": tool, "input": input, "output": output}


TICKET = {"key": "ZZ-42", "summary": "checkout fails", "description": "CheckoutBoomError: no capacity"}
LOGS = {"hits": [{"message": "CheckoutBoomError at services/checkout/pay.py", "path": "services/checkout/pay.py"}]}


def _graph(session_id, calls, inputs=None):
    s = Session(session_id=session_id, source="test", meta={"inputs": inputs or {"key": "ZZ-42"}}, calls=calls)
    ep = make_episodes([s])[0]
    return EpisodeGraph(ep, df.extract_call_edges(ep, {}))


def _ticket_call():
    return _call("jira", "get_issue", {"issue_key": "ZZ-42"}, TICKET)


def _logs_call():
    return _call("logz", "search_logs", {"query": "CheckoutBoomError"}, LOGS)


def _read_call():
    return _call("code", "read_file", {"path": "services/checkout/pay.py"}, {"text": "def pay(): ..."})


def test_edges_are_typed_and_sourced():
    g = _graph("s1", [_ticket_call(), _logs_call(), _read_call()])
    assert Edge(PROMPT, "ids:jira_key", "jira.get_issue") in g.edges
    assert Edge("jira.get_issue", "ids:error_class", "logz.search_logs") in g.edges
    assert Edge("logz.search_logs", "copy:paths", "code.read_file") in g.edges
    ce = next(c for c in g.call_edges if c.target == "code.read_file")
    assert ce.arg == "path" and ce.src_call == 1 and ce.tgt_call == 2


def test_order_independence_is_one_candidate():
    """The same task with its steps in a different order: one candidate with support 2, not two candidates."""
    ordered = _graph("ordered", [_ticket_call(), _logs_call(), _read_call()])
    # the reads happen before the log search this time; both still bind to what came before them
    swapped_calls = [_ticket_call(), _call("code", "read_file", {"path": "CheckoutBoomError"}, {"text": "x"}), _logs_call()]
    swapped = _graph("swapped", swapped_calls)
    shared = ordered.edges & swapped.edges
    assert Edge("jira.get_issue", "ids:error_class", "logz.search_logs") in shared
    assert Edge("jira.get_issue", "ids:error_class", "code.read_file") in swapped.edges

    # the identical episode in a different recorded order: same edge set, one candidate
    a = _graph("a", [_ticket_call(), _logs_call(), _read_call()])
    b = _graph("b", [_ticket_call(), _logs_call(), _read_call()])
    b.episode.calls[1], b.episode.calls[2] = b.episode.calls[2], b.episode.calls[1]
    cands = df.mine([a, b])
    assert len(cands) == 1, [c.display for c in cands]
    assert cands[0].support == 2 and JIRA_ROOT.source == PROMPT
    assert Edge("jira.get_issue", "ids:error_class", "logz.search_logs") in cands[0].edges


def test_a_gap_does_not_break_a_pattern():
    """An unrelated call between two bound calls: not on the graph, so support is unchanged."""
    plain = [_graph("p1", [_ticket_call(), _logs_call()]), _graph("p2", [_ticket_call(), _logs_call()])]
    base = df.mine(plain)
    noise = _call("shell", "run", {"script": "for i in range(3):\n    print(i)\n"}, {"out": "0\n1\n2\n"})
    gapped = [_graph("g1", [_ticket_call(), _logs_call()]),
              _graph("g2", [_ticket_call(), noise, _logs_call()])]
    got = df.mine(gapped)
    assert [c.support for c in got] == [c.support for c in base] == [2]
    assert got[0].edges == base[0].edges
    # and the slice of the gapped episode skips the unrelated call
    calls = next(idx for g, idx in got[0].occurrences if g.session_id == "g2")
    assert calls == (0, 2)


def test_unbindable_material_forms_nothing():
    """Calls whose arguments are all agent-authored (a script, a command, prose) or bare constants: no edges to
    mine, so no candidates -- where the sequence miner would happily report the chain."""
    def authored(sid):
        return _graph(sid, [_call("shell", "run", {"script": "import os\nprint(os.getcwd())\n", "timeout": 30},
                                  {"out": "/tmp"}),
                            _call("web", "fetch", {"url": "https://example.com/docs/whatever", "limit": 20},
                                  {"text": "hello"})], inputs={})
    eps = [authored("u1"), authored("u2")]
    assert all(e.source == CONSTANT for g in eps for e in g.edges), [str(e) for e in eps[0].edges]
    assert df.mine(eps) == []


# ---------------------------------------------------------------- cache
def _write_trace(path, calls, inputs):
    rows = [{"kind": "meta", "session_id": path.stem, "source": "test", "inputs": inputs}]
    rows += [{"session_id": path.stem, "source": "test", **c} for c in calls]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_cache_is_used_on_a_second_pass_and_invalidated_by_a_change(tmp_path):
    traces, cdir = tmp_path / "traces", tmp_path / "dataflow"
    traces.mkdir()
    _write_trace(traces / "one.jsonl", [_ticket_call(), _logs_call()], {"key": "ZZ-42"})

    cold = EdgeCache(dir=cdir, fingerprint="fp")
    first = df.build_graphs(df.load_episodes(traces), {}, cache=cold)
    assert (cold.hits, cold.misses, cold.writes) == (0, 1, 1)
    assert (cdir / "one.json").exists()

    warm = EdgeCache(dir=cdir, fingerprint="fp")
    second = df.build_graphs(df.load_episodes(traces), {}, cache=warm)
    assert (warm.hits, warm.misses) == (1, 0)
    assert first[0].edges == second[0].edges and first[0].call_edges == second[0].call_edges

    # the trace changes -> size and mtime change -> the entry is stale
    _write_trace(traces / "one.jsonl", [_ticket_call(), _logs_call(), _read_call()], {"key": "ZZ-42"})
    again = EdgeCache(dir=cdir, fingerprint="fp")
    third = df.build_graphs(df.load_episodes(traces), {}, cache=again)
    assert (again.hits, again.misses) == (0, 1)
    assert Edge("logz.search_logs", "copy:paths", "code.read_file") in third[0].edges

    # so does a different catalog / workspace fingerprint
    other = EdgeCache(dir=cdir, fingerprint="other-fp")
    df.build_graphs(df.load_episodes(traces), {}, cache=other)
    assert (other.hits, other.misses) == (0, 1)


def test_cache_key_survives_a_touch_that_changes_nothing(tmp_path):
    traces, cdir = tmp_path / "traces", tmp_path / "dataflow"
    traces.mkdir()
    p = traces / "one.jsonl"
    _write_trace(p, [_ticket_call(), _logs_call()], {"key": "ZZ-42"})
    c1 = EdgeCache(dir=cdir, fingerprint="fp")
    df.build_graphs(df.load_episodes(traces), {}, cache=c1)
    c2 = EdgeCache(dir=cdir, fingerprint="fp")
    df.build_graphs(df.load_episodes(traces), {}, cache=c2)
    assert c2.hits == 1


# ---------------------------------------------------------------- the sequence miner is still there
def test_sequence_miner_still_works():
    from crystal.induce.mining import candidates as seq_candidates
    cands = seq_candidates(SIM_STATE.traces, limit=3)
    assert cands and cands[0].length >= 2 and cands[0].support >= 2
    assert isinstance(cands[0].summary(), str)


def test_cli_defaults_to_dataflow_and_accepts_sequences(capsys, monkeypatch):
    from crystal import cli
    monkeypatch.setenv("CRYSTAL_WORKSPACE", str(SIM_STATE.root))
    assert cli.main(["candidates", "--top", "1", "--fast"]) == 0
    out = capsys.readouterr().out
    assert "dataflow" in out and "--ids:jira_key-->" in out
    assert cli.main(["candidates", "--top", "1", "--fast", "--sequences"]) == 0
    assert "sequence" in capsys.readouterr().out


# ---------------------------------------------------------------- the trace index
def _counting_loader(monkeypatch):
    """Record every trace file the store is asked to parse."""
    from crystal.trace import store as trace_store
    seen: list[str] = []
    real = trace_store.load_session

    def spy(path):
        seen.append(Path(path).stem)
        return real(path)

    monkeypatch.setattr(trace_store, "load_session", spy)
    return seen


def _two_traces(tmp_path):
    traces = tmp_path / "traces"
    traces.mkdir()
    _write_trace(traces / "one.jsonl", [_ticket_call(), _logs_call()], {"key": "ZZ-42"})
    _write_trace(traces / "two.jsonl", [_ticket_call(), _logs_call(), _read_call()], {"key": "ZZ-42"})
    return traces, tmp_path / "dataflow" / "index.json"


def test_the_index_is_written_once_and_reused(tmp_path, monkeypatch):
    traces, idx_path = _two_traces(tmp_path)
    seen = _counting_loader(monkeypatch)

    cold = df.build_index(traces, path=idx_path)
    assert (cold.parsed, cold.reused, cold.dropped) == (2, 0, 0)
    assert sorted(seen) == ["one", "two"]
    assert idx_path.exists()
    blob = json.loads(idx_path.read_text())
    assert blob["v"] == df.INDEX_VERSION and blob["dir"] == str(traces)
    assert set(blob["traces"]) == {"one", "two"}
    assert blob["traces"]["one"]["nodes"] == ["jira.get_issue", "logz.search_logs"]
    assert blob["traces"]["two"]["n_calls"] == 3

    seen.clear()
    warm = df.build_index(traces, path=idx_path)
    assert (warm.parsed, warm.reused, warm.dropped) == (0, 2, 0)
    assert seen == [], "a warm index parses nothing"
    assert [e.session_id for e in warm.episodes()] == ["one", "two"]


def test_a_changed_trace_reparses_only_that_one(tmp_path, monkeypatch):
    traces, idx_path = _two_traces(tmp_path)
    df.build_index(traces, path=idx_path)
    seen = _counting_loader(monkeypatch)

    # a touch that changes only the mtime is a change: the trace could have been rewritten byte for byte
    p = traces / "one.jsonl"
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 10))
    idx = df.build_index(traces, path=idx_path)
    assert seen == ["one"] and (idx.parsed, idx.reused) == (1, 1)

    # and so is altering its content
    seen.clear()
    _write_trace(traces / "two.jsonl", [_ticket_call(), _logs_call()], {"key": "ZZ-42"})
    idx = df.build_index(traces, path=idx_path)
    assert seen == ["two"] and (idx.parsed, idx.reused) == (1, 1)
    assert idx.entries["two"].nodes == ["jira.get_issue", "logz.search_logs"]
    assert json.loads(idx_path.read_text())["traces"]["two"]["n_calls"] == 2


def test_a_deleted_trace_is_dropped_from_the_index(tmp_path, monkeypatch):
    traces, idx_path = _two_traces(tmp_path)
    df.build_index(traces, path=idx_path)
    (traces / "two.jsonl").unlink()
    seen = _counting_loader(monkeypatch)

    idx = df.build_index(traces, path=idx_path)
    assert (idx.parsed, idx.reused, idx.dropped) == (0, 1, 1)
    assert seen == []
    assert set(json.loads(idx_path.read_text())["traces"]) == {"one"}
    assert [e.stem for e in idx.episodes()] == ["one"]


def test_mining_from_the_index_matches_mining_from_full_episodes(sim_candidates):
    """The whole point of the index: the same candidates, byte for byte, without reading a trace."""
    from crystal.extract.catalog import load_catalog
    catalog = load_catalog()
    graphs, lite_cands = sim_candidates
    assert all(isinstance(g, df.LiteGraph) for g in graphs), "the sim is mined from the index"
    full = df.mine(df.build_graphs(df.load_episodes(SIM_STATE.traces), catalog, None, None))
    assert json.dumps([c.view() for c in full], sort_keys=True) == \
           json.dumps([c.view() for c in lite_cands], sort_keys=True)


def test_a_warm_index_loads_no_trace_until_a_candidate_is_induced(sim_candidates):
    """Mining reads nothing; `slice_sessions` reads exactly the episodes of the candidate being induced."""
    graphs = df.episode_graphs(SIM_STATE.traces)
    assert graphs and all(g.episode is None for g in graphs)   # even a cold pass releases what it parsed
    cands = df.mine(graphs)
    top = cands[0].sample(3)
    assert len(top.slice_sessions()) == 3
    assert sum(1 for g in graphs if g.episode is not None) == 3, "only the sampled episodes were parsed"


def test_induced_flow_from_the_index_runs_on_an_unseen_ticket():
    """The lazy load has to give the inducer the same calls a full parse would: induce off index-mined candidates
    and run the flow in process against the sim on a ticket no trace mentions."""
    from crystal.flow.runner import FlowRunner
    from crystal.mcp_client import ServerPool
    cands = df.mine(df.episode_graphs(SIM_STATE.traces))
    flow, report = induce_candidate(cands[0], "mined-from-index")
    assert not report.get("unresolved"), report["unresolved"]
    assert flow["inputs"]["key"]["type"] == "jira_key"

    async def go():
        async with ServerPool() as pool:
            return await FlowRunner(pool).run(flow, {"key": "STF-116"}, save=False)

    rec = asyncio.run(go())
    assert rec["status"] == "ok", rec.get("error")
    assert not {s["id"]: s.get("error") for s in rec["steps"] if s.get("error")}
    assert sum(s.get("hits") or 0 for s in rec["steps"]) > 0


def test_cache_status_never_parses_a_trace(monkeypatch):
    """`/candidates` asks this on every poll, with 9,000 episodes behind it: it answers from the index and the
    presence of each edge file, so no trace may be opened."""
    df.episode_graphs(SIM_STATE.traces)                # warm the index and the edge cache
    warm = df.cache_status(SIM_STATE.traces)
    assert warm["episodes"] == 52 and warm["missing"] == 0

    from crystal.induce import mining as mining_mod
    from crystal.trace import store as trace_store

    def refuse(*a, **kw):
        raise AssertionError("cache_status parsed a trace")

    monkeypatch.setattr(trace_store, "load_session", refuse)
    monkeypatch.setattr(trace_store, "load_sessions", refuse)
    monkeypatch.setattr(mining_mod, "load_sessions", refuse)
    assert df.cache_status(SIM_STATE.traces) == warm


def test_a_stale_edge_file_still_counts_as_missing(tmp_path):
    """The cheap freshness check reads only the header of an edge file, so it must still notice a fingerprint or
    a trace that moved -- otherwise the page would claim the analysis is done and then block in the request."""
    traces, idx_path = _two_traces(tmp_path)
    cdir = tmp_path / "dataflow"
    idx = df.build_index(traces, path=idx_path)
    cache = EdgeCache(dir=cdir, fingerprint="fp")
    for e in idx.episodes():
        cache.put_entry(e, df.extract_call_edges(df.load_trace_episode(Path(e.path)), {}))
    assert all(cache.fresh_entry(e) for e in idx.episodes())
    assert not any(EdgeCache(dir=cdir, fingerprint="other").fresh_entry(e) for e in idx.episodes())

    _write_trace(traces / "one.jsonl", [_ticket_call(), _logs_call(), _read_call()], {"key": "ZZ-42"})
    idx = df.build_index(traces, path=idx_path)
    assert {e.stem: cache.fresh_entry(e) for e in idx.episodes()} == {"one": False, "two": True}
