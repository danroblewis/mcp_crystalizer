"""Position-program extractor synthesis (FlashExtract-lite), no LLM.

A span in a text is described by a start position and an end position. Each position is either absolute
({"abs": i}, negative counts from the end) or "the k-th position where a left-context regex ends and a
right-context regex starts" ({"left": [tok..], "right": [tok..], "k": k}). Context regexes are sequences of
at most MAX_TOKENS tokens from a small alphabet: token classes (WORD, NUM, HEX, IDENT, PUNCT, WS, START,
END, BOL, EOL) and literal tokens mined from the text next to the span ("lit:trace_id=", "lit:Error:").
The end position is counted among positions after the start, so a program is a pair of independent
boundary programs that still describe one contiguous span.

Learning (`learn`) enumerates every boundary program consistent with each (text, span) example,
intersects the sets across examples, and ranks survivors: literal anchors before classes, fewer tokens,
smaller |k|, fewer negative spans (other value-shaped substrings the agent did not choose) whose
boundaries the context alone would also accept. `apply` runs a program on a new text.

Program JSON (usable from a flow as {using: position, program: {...}}):
  {"start": {"left": ["lit:on "], "right": ["IDENT"], "k": 1},
   "end":   {"left": ["IDENT"], "right": ["WS", "lit:at"], "k": 1}}
"""
from __future__ import annotations

import re
from itertools import product
from typing import Any

MAX_TOKENS = 3
MAX_K = 3

# class name -> (forward regex, regex for the reversed text). Classes match maximal runs.
CLASSES: dict[str, tuple[str, str]] = {
    "WORD":  (r"(?<![A-Za-z])[A-Za-z]+(?![A-Za-z])",) * 2,
    "NUM":   (r"(?<!\d)\d+(?!\d)",) * 2,
    "HEX":   (r"(?<![0-9a-f])[0-9a-f]+(?![0-9a-f])",) * 2,
    "IDENT": (r"(?<![\w.-])[\w][\w.-]*(?![\w.-])", r"(?<![\w.-])[\w.-]*[\w](?![\w.-])"),
    "PUNCT": (r"[^\w\s]",) * 2,
    "WS":    (r"\s+",) * 2,
    "START": (r"\A", r"\Z"),
    "END":   (r"\Z", r"\A"),
    "BOL":   (r"(?:(?<=\n)|\A)", r"(?=\n|\Z)"),
    "EOL":   (r"(?=\n|\Z)", r"(?:(?<=\n)|\A)"),
}
_CLASS_RX = {k: re.compile(v[0]) for k, v in CLASSES.items()}
_FINE = re.compile(r"\s+|[A-Za-z]+|\d+|[^\w\s]")
_COARSE = re.compile(r"\s+|[\w][\w.-]*|[^\w\s]")
_VALUE_RX = re.compile(r"[\w][\w.-]{2,}")
_rx_cache: dict[tuple, re.Pattern] = {}


# ---------------------------------------------------------------- tokens and regexes
def _tok_rx(tok: str, reverse: bool) -> str:
    if tok.startswith("lit:"):
        lit = tok[4:]
        return re.escape(lit[::-1] if reverse else lit)
    return CLASSES[tok][1 if reverse else 0]


def _seq_rx(seq: tuple[str, ...], reverse: bool) -> re.Pattern:
    key = (seq, reverse)
    if key not in _rx_cache:
        toks = list(reversed(seq)) if reverse else list(seq)
        _rx_cache[key] = re.compile("(?=" + "".join(_tok_rx(t, reverse) for t in toks) + ")")
    return _rx_cache[key]


def _right_starts(text: str, seq: tuple[str, ...]) -> set[int]:
    if not seq:
        return set(range(len(text) + 1))
    return {m.start() for m in _seq_rx(seq, False).finditer(text)}


def _left_ends(text: str, seq: tuple[str, ...]) -> set[int]:
    if not seq:
        return set(range(len(text) + 1))
    n = len(text)
    return {n - m.start() for m in _seq_rx(seq, True).finditer(text[::-1])}


def boundary_positions(text: str, left: tuple[str, ...], right: tuple[str, ...]) -> list[int]:
    """Every position p in text where `left` ends at p and `right` starts at p."""
    return sorted(_left_ends(text, left) & _right_starts(text, right))


def _describe(tok: str, allow_literal: bool = True) -> list[str]:
    """Ways to name one concrete token: its literal and every class that matches it whole."""
    out = []
    if allow_literal and tok.strip():
        out.append("lit:" + tok)
    for cls in ("WORD", "NUM", "HEX", "IDENT", "PUNCT", "WS"):
        if _CLASS_RX[cls].fullmatch(tok):
            out.append(cls)
    if not out:
        out.append("lit:" + tok)
    return out


def _tokens_before(text: str, p: int) -> list[list[tuple[str, int]]]:
    """Token lists (fine and coarse tokenizations) of the text just before p, as (token, offset), last token last."""
    out = []
    lo = max(0, p - 80)
    for rx in (_FINE, _COARSE):
        toks = [(m.group(0), lo + m.start()) for m in rx.finditer(text[lo:p])]
        out.append(toks[-MAX_TOKENS:])
    return out


def _tokens_after(text: str, p: int) -> list[list[tuple[str, int]]]:
    out = []
    for rx in (_FINE, _COARSE):
        toks = [(m.group(0), p + m.start()) for m in rx.finditer(text[p:p + 80])]
        out.append(toks[:MAX_TOKENS])
    return out


def _context_seqs(toks: list[tuple[str, int]], suffix: bool, span: tuple[int, int]) -> set[tuple[str, ...]]:
    """All token-description sequences over the last (suffix) or first (prefix) 0..n tokens. Tokens lying inside
    the span are described by classes only: a literal there would just memorise the example's value."""
    s, e = span
    seqs: set[tuple[str, ...]] = {()}
    for n in range(1, len(toks) + 1):
        window = toks[-n:] if suffix else toks[:n]
        descs = [_describe(t, allow_literal=not (s <= off and off + len(t) <= e)) for t, off in window]
        for combo in product(*descs):
            seqs.add(tuple(combo))
    return seqs


def _anchors_for(text: str, p: int, span: tuple[int, int]) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    lefts: set[tuple[str, ...]] = set()
    rights: set[tuple[str, ...]] = set()
    for toks in _tokens_before(text, p):
        lefts |= _context_seqs(toks, True, span)
    for toks in _tokens_after(text, p):
        rights |= _context_seqs(toks, False, span)
    if p == 0:
        lefts.add(("START",))
    if p == 0 or text[p - 1] == "\n":
        lefts.add(("BOL",))
        lefts |= {("BOL",) + s for s in list(lefts) if s and s[0] not in ("BOL", "START") and len(s) < MAX_TOKENS}
    if p == len(text):
        rights.add(("END",))
    if p == len(text) or text[p] == "\n":
        rights.add(("EOL",))
        rights |= {s + ("EOL",) for s in list(rights) if s and s[-1] not in ("EOL", "END") and len(s) < MAX_TOKENS}
    return lefts, rights


# ---------------------------------------------------------------- learning
def _boundary_programs(text: str, p: int, after: int | None, span: tuple[int, int]) -> dict[tuple, int]:
    """Every (left, right, k) whose k-th boundary position is p; k counts positions > after when given.
    Returns {(left, right): k}, keeping the smaller |k| when both signs fit."""
    lefts, rights = _anchors_for(text, p, span)
    left_sets = {l: _left_ends(text, l) for l in lefts}
    right_sets = {r: _right_starts(text, r) for r in rights}
    out: dict[tuple, int] = {}
    for l, ls in left_sets.items():
        if p not in ls:
            continue
        for r, rs in right_sets.items():
            if not l and not r:
                continue
            if p not in rs:
                continue
            pos = sorted(x for x in (ls & rs) if after is None or x > after)
            i = pos.index(p)
            k_fwd, k_back = i + 1, i - len(pos)
            k = k_fwd if k_fwd <= -k_back else k_back
            if abs(k) <= MAX_K:
                out[(l, r)] = k
    return out


def _negatives(text: str, span: tuple[int, int]) -> list[tuple[int, int]]:
    """Value-shaped substrings the agent did not choose: maximal [\\w.-] runs of length >= 3, other than the span."""
    s, e = span
    return [(m.start(), m.end()) for m in _VALUE_RX.finditer(text) if (m.start(), m.end()) != (s, e)]


# final tie-breaker between otherwise equal programs: the more general class first (IDENT covers WORD/HEX/NUM), so
# the ranking is a total order and does not depend on set iteration order (hash seed)
_GENERALITY = {c: i for i, c in enumerate(["IDENT", "WORD", "HEX", "NUM", "PUNCT", "WS", "BOL", "EOL", "START", "END"])}


def _rank_key(prog: dict, neg_hits: int, side: str = "start") -> tuple:
    """Total order over boundary programs: literal anchor > fewer negatives > fewer token runs > smaller |k| >
    the program that names the span's own token class next to the boundary (its shape rather than the text on the
    far side, which is a different field) > fewer tokens > more general classes > token text."""
    left, right = list(prog.get("left", ())), list(prog.get("right", ()))
    toks = left + right
    has_lit = any(t.startswith("lit:") for t in toks)
    runs, prev = 0, None
    for t in toks:
        kind = "lit" if t.startswith("lit:") else t
        if not (kind == "lit" and prev == "lit"):
            runs += 1
        prev = kind
    span_tok = (left[-1] if left else None) if side == "end" else (right[0] if right else None)
    shape = 0 if span_tok is not None and not span_tok.startswith("lit:") else 1
    generality = tuple(_GENERALITY.get(t, len(_GENERALITY)) for t in toks)
    return (0 if has_lit else 1, neg_hits, runs, abs(prog.get("k", 1)), shape, len(toks), generality, tuple(toks))


def learn_boundary(examples: list[tuple[str, int, int | None, tuple[int, int]]], negatives: list[list[int]] | None = None,
                   side: str = "start") -> list[dict]:
    """examples: (text, position, after, span) per example; side is "start" or "end" (which side of the boundary
    the span lies on). Returns consistent boundary programs, best first."""
    common: dict[tuple, int] | None = None
    for text, p, after, span in examples:
        progs = _boundary_programs(text, p, after, span)
        if common is None:
            common = progs
        else:
            common = {key: k for key, k in common.items() if progs.get(key) == k}
        if not common:
            break
    if not common:
        return []
    ranked = []
    for (l, r), k in common.items():
        neg = 0
        if negatives:
            for (text, _, _, _), negs in zip(examples, negatives):
                pos = set(boundary_positions(text, l, r))
                neg += sum(1 for x in negs if x in pos)
        prog = {"left": list(l), "right": list(r), "k": k}
        ranked.append((_rank_key(prog, neg, side), prog))
    ranked.sort(key=lambda x: x[0])
    return [p for _, p in ranked]


def learn(examples: list[tuple[str, tuple[int, int]]], max_programs: int = 1) -> list[dict]:
    """Learn span programs from (text, (start, end)) pairs. Returns up to max_programs programs, best first,
    each verified to reproduce every example. Absolute positions are the fallback when no context program fits."""
    examples = [(t, (s, e)) for t, (s, e) in examples if 0 <= s < e <= len(t)]
    if not examples:
        return []
    negs = [_negatives(t, sp) for t, sp in examples]
    starts = learn_boundary([(t, s, None, (s, e)) for t, (s, e) in examples], [[a for a, _ in n] for n in negs])
    ends = learn_boundary([(t, e, s, (s, e)) for t, (s, e) in examples], [[b for _, b in n] for n in negs], side="end")
    if not starts and all(s == examples[0][1][0] for _, (s, _) in examples):
        starts = [{"abs": examples[0][1][0]}]
    if not ends and all(e == examples[0][1][1] for _, (_, e) in examples):
        ends = [{"abs": examples[0][1][1]}]
    out = []
    for sp in starts[:4]:
        for ep in ends[:4]:
            prog = {"start": sp, "end": ep}
            if all(apply(prog, t) == t[s:e] for t, (s, e) in examples):
                out.append(prog)
                if len(out) >= max_programs:
                    return out
    return out


# ---------------------------------------------------------------- applying
def _resolve(bp: dict, text: str, after: int | None) -> int | None:
    if "abs" in bp:
        i = bp["abs"]
        i = i if i >= 0 else len(text) + i
        return i if 0 <= i <= len(text) and (after is None or i > after) else None
    pos = [x for x in boundary_positions(text, tuple(bp.get("left", ())), tuple(bp.get("right", ())))
           if after is None or x > after]
    k = bp.get("k", 1)
    if not pos or k == 0 or abs(k) > len(pos):
        return None
    return pos[k - 1] if k > 0 else pos[k]


def apply(program: dict, text: str) -> str | None:
    s = _resolve(program["start"], text, None)
    if s is None:
        return None
    e = _resolve(program["end"], text, s)
    return text[s:e] if e is not None else None


def apply_all(program: dict, text: str) -> list[str]:
    """Every span the program's start context accepts (k ignored), each closed by the end program."""
    sp = program["start"]
    if "abs" in sp:
        v = apply(program, text)
        return [v] if v is not None else []
    out, seen = [], set()
    for s in boundary_positions(text, tuple(sp.get("left", ())), tuple(sp.get("right", ()))):
        e = _resolve(program["end"], text, s)
        if e is not None:
            v = text[s:e]
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


def describe(program: dict) -> str:
    def one(bp: dict) -> str:
        if "abs" in bp:
            return f"abs {bp['abs']}"
        return f"{''.join(bp.get('left', ())) or '·'} | {''.join(bp.get('right', ())) or '·'} #{bp.get('k', 1)}"
    return f"start[{one(program['start'])}] end[{one(program['end'])}]"


def example_from_value(text: str, value: str) -> tuple[int, int] | None:
    """The span of `value` in text as a whole token (not inside a longer [\\w.-] run), first occurrence."""
    if not value:
        return None
    for m in re.finditer(re.escape(value), text):
        s, e = m.start(), m.end()
        if (s == 0 or not re.match(r"[\w.-]", text[s - 1]) or not re.match(r"[\w.-]", value[0])) and \
                (e == len(text) or not re.match(r"[\w.-]", text[e]) or not re.match(r"[\w.-]", value[-1])):
            return s, e
    return None


def serializable(program: dict) -> dict:
    """Plain lists/ints only (YAML-friendly)."""
    def one(bp: dict) -> Any:
        if "abs" in bp:
            return {"abs": int(bp["abs"])}
        return {"left": list(bp.get("left", ())), "right": list(bp.get("right", ())), "k": int(bp.get("k", 1))}
    return {"start": one(program["start"]), "end": one(program["end"])}
