"""Common behaviours as DATAFLOW: what information passes between the calls of an episode (no LLM).

`crystal.induce.mining` mines frequent contiguous chains of tool TYPES. That is a proxy for a flow, and it is wrong
in three ways: a chain can recur without a single argument being derivable (the agent chose the values itself), one
unrelated call in the middle breaks it, and the same task done in a different order is reported as several
candidates. What a flow actually IS, is information passing: a Jira key goes in, the ticket yields an error string,
the error string searches the logs, the logs yield file paths, the paths are read.

So mine the dataflow. For every argument of every call, the inducer's `Binder` already answers "where could this
value have come from" -- an input, an earlier result (an exact copy, a typed id inside it, a catalog entity, a
"Label: value" line), the entity catalog, or timestamp arithmetic on an anchor. Each answer is an EDGE:

    (source, value_type, target)      source: "<server>.<tool>" | prompt | workspace | catalog | constant
                                      target: "<server>.<tool>"
                                      value_type: the binding kind refined with the id/catalog type where there is
                                      one -- ids:jira_key, catalog:service, catalog-attr:service.pagerduty_id,
                                      copy:<field>, regex:<label>, window, input:<name>

plus, per edge, the argument it fed on the target and the calls behind it. An argument the binder cannot explain
produces NO edge: that is the point. An episode is the SET of its edges, so:

  * a pattern is bindable BY CONSTRUCTION -- every edge is a binding that really happened, so the 8%-bound junk
    candidates of the sequence miner cannot form at all;
  * an unrelated call in the middle does not break anything, it is simply not on the graph;
  * the same task with its steps in a different order is ONE candidate, because a set has no order.

Mining: frequent single edges (support >= min_support episodes), then frequent CONNECTED sets of them, grown by
closure (an edge joins a set when it shares a tool node with it and occurs in every episode the set does), keeping
only closed sets. Ranked by support x edges, preferring graphs rooted at a `prompt` edge -- a rooted graph names
the flow's real input. `constant` edges (an argument the agent typed that is neither derived nor authored content)
never seed or extend a candidate; they are recorded, not mined, so a pile of `limit: 20` arguments cannot invent a
flow.

Each episode's edges are cached under the state dir (`dataflow/<trace-stem>.json`), keyed by the trace file's size
and mtime plus a fingerprint of the catalog and workspace metadata, because the binder has to run over EVERY
episode -- a real machine has ~9,000 -- not just the ones that get induced. Measured on this laptop: the sim's 52
episodes take 3.4s cold and 0.04s warm; 5,000 sim-sized episodes (380 MB of traces) take 423s cold and 4.5s warm,
for a 16 MB cache. The mining itself is 0.5s at that size; it is the binder that costs.

A `DataflowCandidate` is induced like any other: each supporting episode is sliced to the calls that participate,
in their recorded order (with the calls they bind to pulled in, so the slice is self-contained), and the existing
`crystal.induce.inducer.induce()` compiles those partial sessions.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from crystal.extract import ids
from crystal.induce.mining import (Episode, PROMPTS_PER_CANDIDATE, _is_authored, load_episodes, slice_calls)
from crystal.trace.store import Session

PROMPT, WORKSPACE, CATALOG, CONSTANT = "prompt", "workspace", "catalog", "constant"
PSEUDO_SOURCES = (PROMPT, WORKSPACE, CATALOG, CONSTANT)

MIN_SUPPORT = 2
CACHE_VERSION = 1
MAX_FREQUENT_EDGES = 400        # the mining context: the most frequent edges, so a huge machine stays responsive
MAX_CONCEPTS = 20000            # closed edge sets enumerated before the search stops (deterministically)
MAX_CANDIDATE_EDGES = 60

_TPL_RX = re.compile(r"\{\{(.*?)\}\}", re.S)
_CATALOG_ATTR_RX = re.compile(r"^catalog\.(\w+)\[(\w+)\.(\w+)\]\.(\w+)$")
_REF_RX = re.compile(r"^(\w+)\.(\w+)")
_SUFFIX_RX = re.compile(r"_\d+$")


# ---------------------------------------------------------------- edges
@dataclass(frozen=True, order=True)
class Edge:
    """One typed information hop. Identity is the triple; the arguments it feeds are recorded per episode."""
    source: str
    value_type: str
    target: str

    def __str__(self) -> str:
        return f"{self.source} --{self.value_type}--> {self.target}"

    @property
    def rooted(self) -> bool:
        return self.source == PROMPT


@dataclass(frozen=True)
class CallEdge:
    """An edge as it actually occurred: which call fed which call, through which argument."""
    source: str
    value_type: str
    target: str
    arg: str
    src_call: int | None        # index into the episode's calls, None for prompt/workspace/catalog/constant
    tgt_call: int

    @property
    def edge(self) -> Edge:
        return Edge(self.source, self.value_type, self.target)

    def as_row(self) -> list:
        return [self.source, self.value_type, self.target, self.arg, self.src_call, self.tgt_call]

    @classmethod
    def from_row(cls, row: list) -> "CallEdge":
        return cls(row[0], row[1], row[2], row[3], row[4], row[5])


@dataclass(eq=False)        # identity, so a graph can key a dict of its own occurrences
class EpisodeGraph:
    """An episode as a set of edges plus the calls behind them."""
    episode: Episode
    call_edges: list[CallEdge]

    def __post_init__(self) -> None:
        self.uses: dict[Edge, list[CallEdge]] = defaultdict(list)
        for ce in self.call_edges:
            self.uses[ce.edge].append(ce)
        self.deps: dict[int, set[int]] = defaultdict(set)      # call -> the calls whose results it binds to
        for ce in self.call_edges:
            if ce.src_call is not None:
                self.deps[ce.tgt_call].add(ce.src_call)

    @property
    def edges(self) -> set[Edge]:
        return set(self.uses)

    @property
    def minable(self) -> set[Edge]:
        """Edges that may seed or extend a candidate: everything but the constants."""
        return {e for e in self.uses if e.source != CONSTANT}

    @property
    def session_id(self) -> str:
        return self.episode.session_id

    @property
    def prompt(self) -> str:
        return self.episode.prompt

    def node_of(self, i: int) -> str:
        c = self.episode.calls[i]
        return f"{c['server']}.{c['tool']}"

    def calls_for(self, edges: Iterable[Edge]) -> tuple[int, ...]:
        """The calls that participate in these edges, in recorded order, closed over what they bind to: an
        intervening call is included exactly when a participating call binds to its result."""
        seed: set[int] = set()
        for e in edges:
            for ce in self.uses.get(e, ()):
                seed.add(ce.tgt_call)
                if ce.src_call is not None:
                    seed.add(ce.src_call)
        frontier, out = list(seed), set(seed)
        while frontier:
            i = frontier.pop()
            for j in self.deps.get(i, ()):  # noqa: PLC0206 - defaultdict: .get avoids inserting keys
                if j not in out:
                    out.add(j)
                    frontier.append(j)
        return tuple(sorted(out))


# ---------------------------------------------------------------- extraction
def extract_call_edges(ep: Episode, catalog: dict, workspace_meta: dict | None = None) -> list[CallEdge]:
    """Run the inducer's Binder over one episode and report where every argument's value came from.

    This is the binder's own taxonomy, read off the template it produced rather than re-derived: whatever the
    binder can explain here, `induce()` will bind the same way when the candidate is compiled. That is why a
    dataflow candidate's bindability is ~100% by construction."""
    from crystal.induce.inducer import Binder
    sess = _binder_session(ep, workspace_meta)
    b = Binder(sess, catalog or {})
    b.name_steps()
    b.choose_anchor()
    for i, c in enumerate(sess.calls):
        b.discover_windows(i, c)
    out: list[CallEdge] = []
    for i, call in enumerate(sess.calls):
        target = f"{call['server']}.{call['tool']}"
        for arg, value in (call.get("input") or {}).items():
            try:
                res = b.bind(value, i)
            except Exception:  # noqa: BLE001 - a value the binder chokes on simply has no edge
                continue
            for source, vt, src in _sources_of(b, res, value):
                out.append(CallEdge(source=source, value_type=vt, target=target, arg=arg, src_call=src, tgt_call=i))
    # dedupe, keeping the recorded order
    seen, uniq = set(), []
    for ce in out:
        if ce not in seen:
            seen.add(ce)
            uniq.append(ce)
    return uniq


def _binder_session(ep: Episode, workspace_meta: dict | None) -> Session:
    """The episode as a session the Binder can take: MCP calls only, so call indices are the episode's."""
    meta = dict(ep.session.meta)
    if workspace_meta:
        meta.setdefault("workspace_meta", workspace_meta)
    return Session(session_id=ep.session_id, source=ep.session.source, meta=meta, calls=ep.calls,
                   prompts=list(ep.session.prompts), result=ep.session.result, path=ep.session.path)


def _sources_of(b, res: dict, value: Any) -> list[tuple[str, str, int | None]]:
    """(source node, value type, source call) for one bound argument -- several for a composite."""
    kind = res.get("kind")
    if kind == "unresolved":
        return []
    if kind == "literal":
        # A constant the agent typed. Recorded (it is part of the picture) but never mined: see the module
        # docstring. Content the agent AUTHORED -- a script, a shell command, prose -- is not even that.
        return [] if _is_authored(value) else [(CONSTANT, "constant", None)]
    tpl = res.get("template")
    if not isinstance(tpl, str):
        return []
    out: list[tuple[str, str, int | None]] = []
    for expr in _TPL_RX.findall(tpl):
        got = _decode(b, expr.strip())
        if got and got not in out:
            out.append(got)
    return out


def _decode(b, expr: str) -> tuple[str, str, int | None] | None:
    """Read one `{{ ... }}` the binder wrote back into (source node, value type, source call index)."""
    head = expr.split("|")[0].strip()
    m = _CATALOG_ATTR_RX.match(head)
    if m:   # catalog.<kind>[<step>.<extract>].<attr>: the value came from the catalog, keyed by an entity a call yielded
        kind, step, _, attr = m.groups()
        j = _step_index(b, step)
        return (_node_of(b, j) if j is not None else CATALOG, f"catalog-attr:{kind}.{attr}", j)
    m = _REF_RX.match(head)
    if not m:
        return None
    root, name = m.group(1), m.group(2)
    if root == "inputs":
        iv = str((b.inputs or {}).get(name, ""))
        t = ids.type_of(iv)
        return (PROMPT, f"ids:{t}" if t else f"input:{name}", None)
    if root == "workspace":
        return (WORKSPACE, f"workspace:{name}", None)
    if root == "catalog":
        return (CATALOG, f"catalog:{name}", None)
    j = _step_index(b, root)
    if j is None:
        return None
    spec = (b.extracts.get(root) or {}).get(name) or {}
    return (_node_of(b, j), _value_type(spec, name), j)


def _value_type(spec: dict, name: str) -> str:
    using = spec.get("using")
    if not using:
        return "copy:" + _SUFFIX_RX.sub("", name)
    if using == "regex":
        return "regex:" + _SUFFIX_RX.sub("", name)
    if using == "window":
        return "window"
    return str(using)          # ids:<type> | catalog:<kind>


def _step_index(b, step_id: str) -> int | None:
    try:
        return b.step_ids.index(step_id)
    except ValueError:
        return None


def _node_of(b, j: int) -> str:
    c = b.s.calls[j]
    return f"{c['server']}.{c['tool']}"


# ---------------------------------------------------------------- cache
@dataclass
class EdgeCache:
    """Per-episode edge sets on disk, keyed by the trace file's size and mtime (plus a fingerprint of the catalog
    and workspace metadata, which also decide what the binder can explain)."""
    dir: Path | None
    fingerprint: str = ""
    hits: int = 0
    misses: int = 0
    writes: int = 0

    def path_for(self, ep: Episode) -> Path | None:
        p = ep.session.path
        if self.dir is None or p is None:
            return None
        return self.dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", p.stem) + ".json")

    @staticmethod
    def key_of(ep: Episode) -> tuple[int, float] | None:
        p = ep.session.path
        if p is None:
            return None
        try:
            st = p.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime)

    def get(self, ep: Episode) -> list[CallEdge] | None:
        path, key = self.path_for(ep), self.key_of(ep)
        if path is None or key is None or not path.exists():
            return None
        try:
            blob = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if blob.get("v") != CACHE_VERSION or blob.get("fp") != self.fingerprint:
            return None
        if blob.get("size") != key[0] or blob.get("mtime") != key[1]:
            return None
        self.hits += 1
        return [CallEdge.from_row(r) for r in blob.get("edges", [])]

    def put(self, ep: Episode, call_edges: list[CallEdge]) -> None:
        path, key = self.path_for(ep), self.key_of(ep)
        self.misses += 1
        if path is None or key is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"v": CACHE_VERSION, "fp": self.fingerprint, "size": key[0], "mtime": key[1],
                                        "edges": [ce.as_row() for ce in call_edges]}))
            self.writes += 1
        except OSError:
            pass


def cache_dir(state_dir: Path | None = None) -> Path:
    from crystal import state as state_mod
    return (state_dir / "dataflow") if state_dir else state_mod.current().dataflow


def fingerprint(catalog: dict | None, workspace_meta: dict | None) -> str:
    blob = json.dumps({"catalog": catalog or {}, "workspace": workspace_meta or {}}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def build_graphs(eps: list[Episode], catalog: dict | None = None, workspace_meta: dict | None = None,
                 cache: EdgeCache | None = None) -> list[EpisodeGraph]:
    out = []
    for ep in eps:
        got = cache.get(ep) if cache else None
        if got is None:
            got = extract_call_edges(ep, catalog or {}, workspace_meta)
            if cache:
                cache.put(ep, got)
        out.append(EpisodeGraph(episode=ep, call_edges=got))
    return out


def episode_graphs(trace_dir: Path | None = None, catalog: dict | None = None, workspace_meta: dict | None = None,
                   use_cache: bool = True, cache: EdgeCache | None = None) -> list[EpisodeGraph]:
    if catalog is None:
        from crystal.extract.catalog import load_catalog
        catalog = load_catalog()
    if cache is None and use_cache:
        cache = EdgeCache(dir=cache_dir(), fingerprint=fingerprint(catalog, workspace_meta))
    return build_graphs(load_episodes(trace_dir), catalog, workspace_meta, cache)


# ---------------------------------------------------------------- candidate
@dataclass
class DataflowCandidate:
    edges: tuple[Edge, ...]
    support: int
    occurrences: list[tuple[EpisodeGraph, tuple[int, ...]]]     # (episode graph, participating call indices)
    saving: int
    prompts: list[str] = field(default_factory=list)
    rank: int = 0
    bound: float | None = None
    truncated: bool = False

    # -------------------------------------------------- shape
    @property
    def length(self) -> int:
        return len(self.edges)

    @property
    def score(self) -> int:
        return self.support * self.length

    @property
    def rooted(self) -> bool:
        """The graph is rooted at the request itself: some edge carries a value straight from the prompt."""
        return any(e.rooted for e in self.edges)

    @property
    def root_value_type(self) -> str | None:
        """The type of the value the flow takes in -- what a later step can name the flow from."""
        roots = sorted({e.value_type for e in self.edges if e.rooted})
        return roots[0] if roots else None

    @property
    def nodes(self) -> list[str]:
        seen = []
        for e in self.edges:
            for n in (e.source, e.target):
                if n not in seen:
                    seen.append(n)
        return seen

    @property
    def servers(self) -> list[str]:
        return list(dict.fromkeys(n.split(".", 1)[0] for n in self.nodes if n not in PSEUDO_SOURCES))

    @property
    def episode_ids(self) -> list[str]:
        return [g.session_id for g, _ in self.occurrences]

    @property
    def display(self) -> list[str]:
        return render_edges(self.edges)

    def summary(self) -> str:
        return " ; ".join(self.display)

    def view(self) -> dict:
        return {"rank": self.rank, "support": self.support, "edges": [str(e) for e in self.edges],
                "length": self.length, "score": self.score, "saving": self.saving, "bound": self.bound,
                "rooted": self.rooted, "root_value_type": self.root_value_type, "dataflow": self.display,
                "servers": self.servers, "episodes": self.episode_ids,
                "calls": {g.session_id: list(idx) for g, idx in self.occurrences}, "prompts": self.prompts}

    # -------------------------------------------------- inducing
    def sample(self, n: int) -> "DataflowCandidate":
        """A cheaper copy for scoring: a few episodes answer "is this derivable" as well as all of them."""
        if len(self.occurrences) <= n:
            return self
        occ = self.occurrences[:n]
        return DataflowCandidate(edges=self.edges, support=len(occ), occurrences=occ,
                                 saving=sum(len(i) for _, i in occ), prompts=self.prompts, rank=self.rank)

    def slice_sessions(self) -> list[Session]:
        """Each supporting episode restricted to its participating calls, in their recorded order."""
        return [slice_calls(g.episode, idx) for g, idx in self.occurrences]


def render_edges(edges: Iterable[Edge]) -> list[str]:
    """The graph as readable chains: `prompt --ids:jira_key--> jira.jira_get_issue --error_class--> logz.search_logs`.
    Parallel edges between the same two nodes share one arrow (`--window, ids:trace_id-->`); every edge appears
    exactly once, and a branch starts a new line."""
    edges = sorted(edges)
    if not edges:
        return []
    arrows: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in edges:
        arrows[(e.source, e.target)].append(e.value_type)
    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    incoming: set[str] = set()
    for src, tgt in sorted(arrows):
        outgoing[src].append((src, tgt))
        incoming.add(tgt)
    used: set[tuple[str, str]] = set()
    lines: list[str] = []
    todo = sorted(arrows)

    def label(pair: tuple[str, str]) -> str:
        return " --%s--> " % ", ".join(sorted(arrows[pair]))

    while len(used) < len(todo):
        free = [p for p in todo if p not in used]
        start = next((p for p in free if p[0] == PROMPT), None) or next((p for p in free if p[0] not in incoming), free[0])
        used.add(start)
        parts, node = [start[0], label(start), start[1]], start[1]
        while True:
            nxt = [p for p in outgoing.get(node, ()) if p not in used]
            if not nxt:
                break
            used.add(nxt[0])
            parts += [label(nxt[0]), nxt[0][1]]
            node = nxt[0][1]
        lines.append("".join(parts))
    return lines


# ---------------------------------------------------------------- mining
def mine(graphs: list[EpisodeGraph], min_support: int = MIN_SUPPORT, limit: int | None = None,
         max_concepts: int = MAX_CONCEPTS) -> list[DataflowCandidate]:
    """Frequent, connected, closed edge sets over the episodes' dataflow graphs.

    Frequent single edges first, then closure-driven growth (Close-by-One over the episodes x edges context, which
    enumerates every closed set exactly once): a set's closure is every frequent edge that occurs in all of its
    episodes, and only the connected parts of a closure are kept -- an edge joins a set when it shares a tool node
    with it. Closed means no strictly larger set has the same support."""
    per_edge: dict[Edge, set[int]] = defaultdict(set)
    for i, g in enumerate(graphs):
        for e in g.minable:
            per_edge[e].add(i)
    frequent = [e for e, eps in per_edge.items() if len(eps) >= min_support]
    truncated = len(frequent) > MAX_FREQUENT_EDGES
    frequent.sort(key=lambda e: (-len(per_edge[e]), e))
    frequent = frequent[:MAX_FREQUENT_EDGES]
    frequent.sort()
    if not frequent:
        return []
    index = {e: i for i, e in enumerate(frequent)}
    bits = [_bits(per_edge[e]) for e in frequent]
    all_eps = _bits(range(len(graphs)))

    def closure(eps: int) -> frozenset[Edge]:
        return frozenset(frequent[i] for i, b in enumerate(bits) if eps & ~b == 0)

    concepts: list[tuple[frozenset[Edge], int]] = []
    seen_eps: set[int] = set()

    def cbo(edges: frozenset[Edge], eps: int, start: int) -> None:
        if len(concepts) >= max_concepts:
            return
        concepts.append((edges, eps))
        for i in range(start, len(frequent)):
            if frequent[i] in edges:
                continue
            new_eps = eps & bits[i]
            if _popcount(new_eps) < min_support:
                continue
            new_edges = closure(new_eps)
            if any(index[x] < i for x in new_edges - edges):
                continue                      # canonicity: this concept is reached from a smaller edge
            if new_eps in seen_eps:
                continue
            seen_eps.add(new_eps)
            cbo(new_edges, new_eps, i + 1)
            if len(concepts) >= max_concepts:
                return

    seen_eps.add(all_eps)
    cbo(closure(all_eps), all_eps, 0)

    # a closure may be several disconnected graphs: keep each connected part, then close it again in its own right
    sets: dict[frozenset[Edge], int] = {}
    for edges, _eps in concepts:
        for comp in _components(edges):
            comp, eps = _close_component(comp, per_edge, frequent)
            if not comp or len(eps) < min_support:
                continue
            if len(comp) > MAX_CANDIDATE_EDGES:
                continue
            prev = sets.get(comp)
            if prev is None or len(eps) > prev:
                sets[comp] = len(eps)
    # closed: drop a set a strictly larger set with the same support contains
    ordered = sorted(sets, key=lambda s: (-len(s), sorted(s)))
    kept: list[frozenset[Edge]] = []
    for s in ordered:
        if any(s < k and sets[k] == sets[s] for k in kept):
            continue
        kept.append(s)

    out = [_candidate(s, per_edge, graphs) for s in kept]
    for c in out:
        c.truncated = truncated
    out.sort(key=_rank_key)
    for i, c in enumerate(out, 1):
        c.rank = i
    return out[:limit] if limit else out


def _rank_key(c: DataflowCandidate):
    """Support x edges, but a graph rooted at a `prompt` edge comes first: it names the flow's real input, which is
    what makes a candidate a flow rather than a fragment of one."""
    return (not c.rooted, -c.score, -c.support, -c.saving, sorted(str(e) for e in c.edges))


def _candidate(edges: frozenset[Edge], per_edge: dict[Edge, set[int]], graphs: list[EpisodeGraph]) -> DataflowCandidate:
    eps = sorted(set.intersection(*(per_edge[e] for e in edges)))
    occ = [(graphs[i], graphs[i].calls_for(edges)) for i in eps]
    prompts: list[str] = []
    for g, _ in occ:
        p = (g.prompt or "").strip()
        if p and p not in prompts:
            prompts.append(p)
    return DataflowCandidate(edges=tuple(sorted(edges)), support=len(occ), occurrences=occ,
                             saving=sum(len(idx) for _, idx in occ), prompts=prompts[:PROMPTS_PER_CANDIDATE])


def _components(edges: Iterable[Edge]) -> list[frozenset[Edge]]:
    """Split an edge set into connected parts. Only real tool nodes connect: `prompt`, `workspace`, `catalog` and
    `constant` are not places, they are origins, so two edges out of `prompt` into unrelated tools are not one
    graph."""
    edges = list(edges)
    parent = list(range(len(edges)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    by_node: dict[str, list[int]] = defaultdict(list)
    for i, e in enumerate(edges):
        for n in (e.source, e.target):
            if n not in PSEUDO_SOURCES:
                by_node[n].append(i)
    for members in by_node.values():
        for i in members[1:]:
            a, b = find(members[0]), find(i)
            parent[a] = b
    groups: dict[int, set[Edge]] = defaultdict(set)
    for i, e in enumerate(edges):
        groups[find(i)].add(e)
    return [frozenset(g) for g in groups.values()]


def _close_component(comp: frozenset[Edge], per_edge: dict[Edge, set[int]], frequent: list[Edge]) -> tuple[frozenset[Edge], set[int]]:
    """Grow one connected part back to its own closure: every frequent edge that occurs in all of its episodes and
    touches one of its tool nodes, until nothing more joins."""
    cur = set(comp)
    eps = set.intersection(*(per_edge[e] for e in cur))
    while True:
        nodes = {n for e in cur for n in (e.source, e.target) if n not in PSEUDO_SOURCES}
        extra = {e for e in frequent if e not in cur and eps <= per_edge[e]
                 and ((e.source in nodes) or (e.target in nodes))}
        if not extra:
            return frozenset(cur), eps
        cur |= extra


def _bits(indices: Iterable[int]) -> int:
    v = 0
    for i in indices:
        v |= 1 << i
    return v


def _popcount(v: int) -> int:
    return v.bit_count()


def candidates(trace_dir: Path | None = None, min_support: int = MIN_SUPPORT, limit: int | None = None,
               catalog: dict | None = None, workspace_meta: dict | None = None, use_cache: bool = True,
               cache: EdgeCache | None = None) -> list[DataflowCandidate]:
    graphs = episode_graphs(trace_dir, catalog=catalog, workspace_meta=workspace_meta, use_cache=use_cache, cache=cache)
    return mine(graphs, min_support=min_support, limit=limit)


# ---------------------------------------------------------------- reporting
def format_candidates(cands: list[DataflowCandidate], prompts: bool = True) -> str:
    if not cands:
        return ("no recurring dataflow yet (need >= 2 episodes that pass the same kind of value between the same "
                "tools; `mcp-explorer candidates --sequences` mines plain tool chains instead)")
    scored = any(c.bound is not None for c in cands)
    head = f"{'#':>3} {'support':>7} {'edges':>5} {'score':>6} {'saving':>7}"
    lines = [head + (f" {'bound':>6}" if scored else "") + "  dataflow"]
    pad = len(head) + (7 if scored else 0)
    for c in cands:
        b = "" if not scored else (f" {'-':>6}" if c.bound is None else f" {round(c.bound * 100):>5}%")
        chains = c.display or ["(no edges)"]
        lines.append(f"{c.rank:>3} {c.support:>7} {c.length:>5} {c.score:>6} {c.saving:>7}{b}  {chains[0]}")
        for extra in chains[1:]:
            lines.append(f"{'':{pad}}  {extra}")
        if prompts:
            for p in c.prompts[:3]:
                lines.append(f"{'':{pad}}  \"{p[:110].replace(chr(10), ' ')}{'…' if len(p) > 110 else ''}\"")
    if cands and cands[0].truncated:
        lines.append("")
        lines.append(f"Note: more than {MAX_FREQUENT_EDGES} distinct edges recur here; only the most frequent ones were mined.")
    if cands and not any(c.rooted for c in cands[:3]):
        lines.append("")
        lines.append("Note: no candidate is rooted at the request (`prompt --...-->`), so these graphs pass values")
        lines.append("between tools but nobody names what goes IN. Sessions recorded with their inputs (the scripted")
        lines.append("agent, `record`, a hook that captured the prompt) produce rooted graphs.")
    return "\n".join(lines)
