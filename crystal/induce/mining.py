"""Common behaviours: recurring tool-call sequences across the traces of a workspace (no LLM).

Every trace, whichever way it got here (the hook, `record`, the scripted agent, an imported transcript), is an
episode: one prompt and the calls made under it. An imported session with several prompts is represented by its
`<sessionId>-e<N>` episodes, never by the whole session as well. Each episode is normalised to its sequence of
`server.tool` names over MCP calls (Claude Code's own Read/Grep/Glob/Bash are context, not steps), with consecutive
repeats of the same tool collapsed into one fan-out marker (`logz.search_logs*`). Frequent contiguous subsequences
of length >= 2 supported by >= 2 episodes are the candidates; only closed ones are reported (a subsequence whose
every extension loses support), ranked by support x length, each with the prompts that produced it and the calls a
flow would save (the calls the pattern covers, summed over the episodes it appears in).

A candidate can be induced directly: `induce_candidate()` slices every supporting episode to the span matching
the pattern and hands those partial sessions to `crystal.induce.inducer.induce()`, so the flow contains exactly
the shared behaviour and binds its arguments the usual way (inputs, extracts, ladders, fan-outs).
"""
from __future__ import annotations

import json
from typing import Any

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from crystal.trace.store import Session, load_sessions

MAX_LEN = 40
MIN_SUPPORT = 2
MIN_LEN = 2
PROMPTS_PER_CANDIDATE = 6


@dataclass
class Episode:
    session: Session
    tokens: list[str]                      # server.tool per collapsed run
    spans: list[tuple[int, int]]           # [start, end) into `calls` for each token
    fanout: list[bool]                     # the run had more than one call
    calls: list[dict]                      # the MCP calls of the session, in order

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def prompt(self) -> str:
        return self.session.prompt or ""

    @property
    def inputs(self) -> dict:
        return self.session.meta.get("inputs") or {}


@dataclass
class Candidate:
    seq: tuple[str, ...]
    support: int
    occurrences: list[tuple[Episode, int, int]]        # (episode, token start, token end) — first occurrence per episode
    fanout: tuple[bool, ...]                           # a position fanned out in at least one supporting episode
    saving: int                                        # calls covered, summed over the supporting episodes
    prompts: list[str] = field(default_factory=list)
    rank: int = 0
    bound: float | None = None        # share of step arguments a program can actually derive (see bindability())

    @property
    def length(self) -> int:
        return len(self.seq)

    @property
    def score(self) -> int:
        return self.support * self.length

    @property
    def display(self) -> list[str]:
        return [t + ("*" if f else "") for t, f in zip(self.seq, self.fanout)]

    @property
    def servers(self) -> list[str]:
        return list(dict.fromkeys(t.split(".", 1)[0] for t in self.seq))

    @property
    def episode_ids(self) -> list[str]:
        return [e.session_id for e, _, _ in self.occurrences]

    def view(self) -> dict:
        return {"rank": self.rank, "support": self.support, "length": self.length, "score": self.score, "saving": self.saving,
                "bound": self.bound,
                "sequence": self.display, "servers": self.servers, "episodes": self.episode_ids, "prompts": self.prompts}


# ---------------------------------------------------------------- episodes
def normalise(session: Session) -> tuple[list[str], list[tuple[int, int]], list[bool], list[dict]]:
    calls = [c for c in session.calls if c.get("server") != "claude-code"]
    tokens, spans, fan = [], [], []
    i = 0
    while i < len(calls):
        name = f"{calls[i]['server']}.{calls[i]['tool']}"
        j = i + 1
        while j < len(calls) and f"{calls[j]['server']}.{calls[j]['tool']}" == name:
            j += 1
        tokens.append(name)
        spans.append((i, j))
        fan.append(j - i > 1)
        i = j
    return tokens, spans, fan, calls


def episodes(sessions: list[Session]) -> list[Episode]:
    """One episode per trace, except a session that was split: its `-e<N>` episodes stand for it."""
    parents = {s.meta.get("parent_session") for s in sessions if s.meta.get("parent_session")}
    out = []
    for s in sessions:
        if s.session_id in parents:
            continue
        tokens, spans, fan, calls = normalise(s)
        if tokens:
            out.append(Episode(session=s, tokens=tokens, spans=spans, fanout=fan, calls=calls))
    return out


def load_episodes(trace_dir: Path | None = None) -> list[Episode]:
    return episodes(load_sessions(trace_dir))


# ---------------------------------------------------------------- mining
def _contains(small: tuple, big: tuple) -> bool:
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def mine(eps: list[Episode], min_support: int = MIN_SUPPORT, min_len: int = MIN_LEN, max_len: int = MAX_LEN,
         limit: int | None = None) -> list[Candidate]:
    """Frequent closed contiguous subsequences of the episodes' token sequences, ranked by support x length."""
    first: dict[tuple, dict[int, tuple[int, int]]] = defaultdict(dict)      # seq -> episode index -> (start, end)
    fan: dict[tuple, list[bool]] = {}
    for ei, ep in enumerate(eps):
        toks = tuple(ep.tokens)
        for i in range(len(toks)):
            for n in range(min_len, min(max_len, len(toks) - i) + 1):
                seq = toks[i:i + n]
                occ = first[seq]
                if ei not in occ:
                    occ[ei] = (i, i + n)
                f = fan.setdefault(seq, [False] * n)
                for k in range(n):
                    if ep.fanout[i + k]:
                        f[k] = True
    frequent = {seq: occ for seq, occ in first.items() if len(occ) >= min_support}
    # closed: drop a pattern when a longer pattern with the same support contains it
    by_support: dict[int, list[tuple]] = defaultdict(list)
    for seq, occ in frequent.items():
        by_support[len(occ)].append(seq)
    closed = []
    for sup, seqs in by_support.items():
        seqs.sort(key=len, reverse=True)
        kept: list[tuple] = []
        for seq in seqs:
            if any(len(k) > len(seq) and _contains(seq, k) for k in kept):
                continue
            kept.append(seq)
        closed.extend(kept)
    out = []
    for seq in closed:
        occ = frequent[seq]
        occurrences = [(eps[ei], s, e) for ei, (s, e) in sorted(occ.items())]
        saving = sum(ep.spans[e - 1][1] - ep.spans[s][0] for ep, s, e in occurrences)
        prompts = []
        for ep, _, _ in occurrences:
            p = ep.prompt.strip()
            if p and p not in prompts:
                prompts.append(p)
        out.append(Candidate(seq=seq, support=len(occ), occurrences=occurrences, fanout=tuple(fan[seq]), saving=saving,
                             prompts=prompts[:PROMPTS_PER_CANDIDATE]))
    out.sort(key=lambda c: (-c.score, -c.support, -c.saving, c.seq))
    for i, c in enumerate(out, 1):
        c.rank = i
    return out[:limit] if limit else out


def candidates(trace_dir: Path | None = None, **kw) -> list[Candidate]:
    return mine(load_episodes(trace_dir), **kw)


# ---------------------------------------------------------------- inducing a candidate
def slice_episode(ep: Episode, start: int, end: int) -> Session:
    """The episode restricted to the calls under tokens [start, end): a partial session the inducer can take."""
    lo, hi = ep.spans[start][0], ep.spans[end - 1][1]
    calls = [dict(c) for c in ep.calls[lo:hi]]
    for i, c in enumerate(calls, 1):
        c["seq"] = i
    meta = dict(ep.session.meta)
    meta.setdefault("prompt", ep.prompt or None)
    return Session(session_id=ep.session_id, source=ep.session.source, meta=meta, calls=calls, prompts=list(ep.session.prompts),
                   result=ep.session.result, path=ep.session.path)


def induce_candidate(cand: Candidate, name: str, catalog: dict | None = None, workspace_meta: dict | None = None) -> tuple[dict, dict]:
    from crystal.induce.inducer import induce
    sessions = [slice_episode(ep, s, e) for ep, s, e in cand.occurrences]
    if workspace_meta:
        for s in sessions:
            s.meta.setdefault("workspace_meta", workspace_meta)
    flow, report = induce(sessions, name, catalog)
    flow["title"] = name.replace("-", " ")
    flow["description"] = ("Common behaviour mined from %d episodes: %s." % (cand.support, " -> ".join(cand.display)))
    flow["mined"] = {"sequence": cand.display, "support": cand.support, "saving": cand.saving,
                     "episodes": cand.episode_ids, "prompts": cand.prompts}
    report["candidate"] = cand.view()
    return flow, report


def bindability(cand: Candidate, catalog: dict | None = None, workspace_meta: dict | None = None) -> float | None:
    """The share of this candidate's step arguments the inducer can derive -- from an input, an earlier result, the
    entity catalog or a time window. A low score means the agent supplied those values from its own knowledge (which
    URL to read next, which file to open), and no program can reproduce that choice: the sequence recurs, but it is
    not a flow. None when the candidate cannot be induced at all."""
    try:
        flow, report = induce_candidate(cand, "probe", catalog=catalog, workspace_meta=workspace_meta)
    except Exception:  # noqa: BLE001 - a candidate that will not induce is simply unscored
        return None
    # Only arguments that VARY between the supporting episodes say anything: a constant (max_length: 8000) is
    # bound trivially and would flatter the score. A varying argument is either derived (it renders a template)
    # or unresolved (the flow has to hardcode one episode's value).
    derived = authored = 0
    for st in flow.get("steps", []):
        for v in (st.get("args") or {}).values():
            blob = json.dumps(v, default=str)
            if "{{" in blob:
                derived += 1
            elif _is_authored(v):
                authored += 1
    unresolved = sum(len(args) for args in (report.get("unresolved") or {}).values())
    varying = derived + unresolved + authored
    if not varying:
        return None if not flow.get("steps") else 1.0      # every argument is the same in every episode
    return derived / varying


AUTHORED_CHARS = 200


def _is_authored(value: Any) -> bool:
    """An argument that is content the agent wrote -- a script for run_python, a shell command, a prose instruction --
    rather than a parameter. Replaying it verbatim reproduces one past session, not a reusable query, so it counts
    against a candidate even when every episode passed the same text."""
    if not isinstance(value, str):
        return False
    return "\n" in value.strip() or len(value) > AUTHORED_CHARS


def score_bindability(cands: list[Candidate], catalog: dict | None = None, workspace_meta: dict | None = None) -> list[Candidate]:
    """Fill in `bound` for each candidate (an induce per candidate; no LLM, no network)."""
    for c in cands:
        c.bound = bindability(c, catalog, workspace_meta)
    return cands


def write_candidate_flow(cand: Candidate, name: str, flow_dir: Path, catalog: dict | None = None,
                         workspace_meta: dict | None = None) -> tuple[Path, dict]:
    from crystal.induce.inducer import dump_flow
    flow, report = induce_candidate(cand, name, catalog, workspace_meta)
    flow_dir.mkdir(parents=True, exist_ok=True)
    out = flow_dir / f"{name}.yaml"
    out.write_text(dump_flow(flow))
    return out, report


def format_candidates(cands: list[Candidate], prompts: bool = True) -> str:
    if not cands:
        return "no recurring tool sequences yet (need >= 2 episodes sharing >= 2 consecutive calls)"
    scored = any(c.bound is not None for c in cands)
    head = f"{'#':>3} {'support':>7} {'len':>4} {'score':>6} {'saving':>7}"
    lines = [head + (f" {'bound':>6}" if scored else "") + "  sequence"]
    for c in cands:
        b = "" if not scored else (f" {'-':>6}" if c.bound is None else f" {round(c.bound * 100):>5}%")
        lines.append(f"{c.rank:>3} {c.support:>7} {c.length:>4} {c.score:>6} {c.saving:>7}{b}  {' -> '.join(c.display)}")
        if prompts:
            for p in c.prompts[:3]:
                lines.append(f"{'':32}  \"{p[:110].replace(chr(10), ' ')}{'…' if len(p) > 110 else ''}\"")
    if scored and cands and all((c.bound or 0) < 0.5 for c in cands[:3]):
        lines.append("")
        lines.append("Note: `bound` is the share of arguments a program could derive. These are low, so the agent supplied most")
        lines.append("of them itself -- which page to fetch, or a whole script to run -- rather than deriving them from the")
        lines.append("request or an earlier result. The sequence repeats, but a flow would only replay one past session.")
        lines.append("Sequences crystallize when the tools are QUERIES with typed parameters (an issue key, a service, a time")
        lines.append("window) rather than executors that take code, a command or prose.")
    return "\n".join(lines)

