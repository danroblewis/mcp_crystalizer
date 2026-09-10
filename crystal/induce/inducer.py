"""Trace -> flow inducer (no LLM).

For every argument of every recorded call, search the session's earlier results for the value and label the
binding (TraceCompiler's taxonomy): input | copy (exact leaf) | ids:<type> | catalog:<kind> | catalog-attr |
regex (line-anchored "Label: value") | window (timestamp arithmetic, optionally on an anchor rounded to the
hour/day) | composite (several of those inside a search string) | position (a learned position program, see
crystal.extract.positions) | literal | unresolved. Binding runs in two passes so windows discovered from later
calls (logz from/to) can explain dates inside earlier search strings (slack after:/before:); the timestamp field
that explains the most windows in a session is the session's anchor. Consecutive calls to the same tool collapse
into forEach (one arg varies over a list) or a ladder (earlier rungs had no hits).

Steps are aligned across sessions by signature, not by occurrence: same tool, and for every argument both
sessions bind, the classes of what the templates reference (text | id | window) overlap; literals and ladder
rungs do not matter. A step that fits a group its session already occupies joins it as an extra fan-out member
when it has the same {{ item }} shape (logs for trace ids found later in the session). Per group, literals merge
by majority, timestamp arguments by majority (never a ladder; the alternatives go to the report), other
differing templates become a ladder ordered by success rate. Extract names are canonicalised across sessions,
steps unique to some sessions stay optional with seen_in counts, and steps are reordered so every reference
points backwards. Values that differ across sessions with no binding are handed to the position-program
learner; what it cannot explain is reported as unresolved.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

import yaml

from crystal.extract import ids, positions
from crystal.extract.catalog import Gazetteer, load_catalog
from crystal.extract.extractors import parse_duration, round_down
from crystal.flow.cards import skeleton_card
from crystal.flow.runner import count_hits
from crystal.trace.store import Session

STEP_NAMES = {"jira_get_issue": "issue", "jira_search": "issues", "conversations_search_messages": "slack",
              "conversations_replies": "thread", "conversations_history": "history", "grep": "code", "codeowners": "owners",
              "read_file": "source", "confluence_search": "confluence", "confluence_get_page": "runbook",
              "query_prometheus_range": "metrics", "search_logs": "logs", "git_log": "commits", "git_show": "commit",
              "list_incidents": "pagerduty", "get_incident": "pagerduty_incident"}
TS_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}")
LABEL_LINE_RX = re.compile(r"^\s*([A-Za-z][A-Za-z _]{0,30}?)\s*([:=])\s*(\S.*?)\s*$")
REF_RX = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\.([A-Za-z_]\w*)")
TPL_RX = re.compile(r"\{\{.*?\}\}", re.S)
# id types that behave like search text rather than keys
TEXTY_TYPES = {"error_class", "slack_channel", "email", "url"}
# rounding options for window anchors: exact first, then the coarsest rounding that fits
ROUNDINGS: tuple[tuple[str | None, int], ...] = ((None, 900), ("1d", 86400), ("1h", 1800))


def leaves(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                sub = f"{path}.{k}" if path else k
            else:
                sub = f"{path}['{k}']" if path else f"['{k}']"
            out += leaves(v, sub)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += leaves(v, f"{path}[{i}]")
    else:
        out.append((path, obj))
    return out


def generalize(path: str) -> tuple[str, bool]:
    g = re.sub(r"\[\d+\]", "[*]", path)
    return g, g != path


def _ts(s: Any) -> datetime | None:
    if isinstance(s, str) and TS_RX.match(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    return None


def _dur(td: timedelta) -> str:
    secs = int(abs(td.total_seconds()))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs and secs % n == 0:
            return f"{secs // n}{unit}"
    return f"{secs}s"


def _plural(name: str) -> str:
    return name + ("_list" if name.endswith("s") else "s")


def _bounded(lit: str, text: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(lit) + r"(?![\w-])", text) is not None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _idlike_num(v: Any) -> bool:
    """Numbers worth copying between calls are id-sized (page ids, incident numbers), not counts or limits."""
    return _is_num(v) and float(v).is_integer() and abs(int(v)) >= 1000


class Binder:
    def __init__(self, session: Session, catalog: dict):
        self.s = session
        self.inputs = session.meta.get("inputs") or {}
        self.catalog = catalog
        self.gazetteers = {k: Gazetteer(v) for k, v in catalog.items() if isinstance(v, dict)}
        self.step_ids: list[str] = []
        self.extracts: dict[str, dict] = defaultdict(dict)
        self.windows: list[dict] = []   # {step, g, round, name, before, after}
        self.anchor: tuple[int, str] | None = None   # (step index, generalized path) explaining most timestamps
        self._leaf_cache: dict[int, list] = {}
        self._cands_cache: dict[int, list] = {}

    # ---------------------------------------------------------------- helpers
    def name_steps(self) -> None:
        seen = Counter()
        for c in self.s.calls:
            base = STEP_NAMES.get(c["tool"], c["tool"])
            seen[base] += 1
            self.step_ids.append(base if seen[base] == 1 else f"{base}_{seen[base]}")

    def leaves_of(self, j: int) -> list[tuple[str, Any]]:
        if j not in self._leaf_cache:
            self._leaf_cache[j] = leaves(self.s.calls[j].get("output"))
        return self._leaf_cache[j]

    def add_extract(self, step: str, name: str, spec: dict) -> str:
        for n, sp in self.extracts[step].items():
            if sp == spec:
                return n
        n = name
        if n in self.extracts[step] and spec.get("using") != "window":
            kind = str(spec.get("using", "field")).split(":")[0]
            n = f"{name}_{kind}"
        i = 2
        while n in self.extracts[step]:
            n, i = f"{name}_{i}", i + 1
        self.extracts[step][n] = spec
        return n

    def ref(self, step: str, name: str, in_list: bool) -> dict:
        tpl = "{{ %s.%s | first }}" % (step, name) if in_list else "{{ %s.%s }}" % (step, name)
        return {"template": tpl, "list_ref": f"{step}.{name}" if in_list else None}

    # ---------------------------------------------------------------- candidates
    def candidates(self, idx: int) -> list[dict]:
        """Every value extractable from outputs before call idx: {value, template, kind, list_ref}. Cached per idx."""
        if idx in self._cands_cache:
            return self._cands_cache[idx]
        out: list[dict] = []
        for name, iv in self.inputs.items():
            out.append({"value": str(iv), "template": "{{ inputs.%s }}" % name, "kind": "input", "list_ref": None})
        seen_entities: dict[str, tuple[str, str]] = {}
        for j in range(idx):
            step = self.step_ids[j]
            seen_g: Counter = Counter()   # position of each leaf within its generalized (list) path
            for path, leaf in self.leaves_of(j):
                g, in_list = generalize(path)
                first = seen_g[g] == 0   # `| first` is only right for the first element of a list path
                seen_g[g] += 1
                if _is_num(leaf):
                    if not _idlike_num(leaf):
                        continue
                    kind = path.split(".")[-1].split("[")[0].strip("'")
                    spec = {"from": g, **({"all": True} if in_list else {})}
                    out.append({"value": str(leaf), "kind": "copy", "spec": (step, _plural(kind) if in_list else kind, spec), "list_ref_flag": in_list, "step": step, "first": first})
                    continue
                if not isinstance(leaf, str) or not leaf:
                    continue
                # exact leaf copy
                if len(leaf) <= 200 and "\n" not in leaf:
                    kind = ids.type_of(leaf) or path.split(".")[-1].split("[")[0].strip("'")
                    spec = {"from": g, **({"all": True} if in_list else {})}
                    out.append({"value": leaf, "kind": "copy", "spec": (step, _plural(kind) if in_list else kind, spec), "list_ref_flag": in_list, "step": step, "first": first})
                # typed ids inside the leaf
                for m in ids.typed_mentions(leaf):
                    if m["value"] == leaf:
                        continue
                    spec = {"from": g, "using": f"ids:{m['type']}", **({"all": True} if in_list else {})}
                    out.append({"value": m["value"], "kind": f"ids:{m['type']}", "spec": (step, _plural(m["type"]) if in_list else m["type"], spec), "list_ref_flag": in_list, "step": step, "first": first})
                # catalog entity mentions
                for kind, gz in self.gazetteers.items():
                    for h in gz.find(leaf):
                        spec = {"from": g, "using": f"catalog:{kind}"}
                        out.append({"value": h["name"], "kind": f"catalog:{kind}", "spec": (step, kind, spec), "list_ref_flag": False, "step": step})
                        seen_entities.setdefault(h["name"], (kind, step))
                        # aliases as they appear in text
                        out.append({"value": leaf[h["start"]:h["end"]], "kind": f"catalog:{kind}", "spec": (step, kind, spec), "list_ref_flag": False, "step": step})
                # "Label: value" lines
                for line in leaf.splitlines():
                    m = LABEL_LINE_RX.match(line)
                    if m and len(m.group(3)) >= 3:
                        label, sep, val = m.groups()
                        pat = r"^%s\s*%s\s*(.+)$" % (re.escape(label), re.escape(sep))
                        spec = {"from": g, "using": "regex", "pattern": pat, **({"all": True} if in_list else {})}
                        nm = re.sub(r"\W+", "_", label.lower())
                        out.append({"value": val, "kind": "regex", "spec": (step, _plural(nm) if in_list else nm, spec), "list_ref_flag": in_list, "step": step})
        # catalog attributes of mentioned entities: e.g. the service's pagerduty id / repo path / channels
        for ent, (kind, step) in seen_entities.items():
            for ak, av in self.catalog.get(kind, {}).get(ent, {}).items():
                if isinstance(av, str) and len(av) >= 3:
                    out.append({"value": av, "kind": "catalog-attr", "attr": (kind, ent, ak), "step": step})
        # windows (dates) are added by bind_composite from self.windows
        self._cands_cache[idx] = out
        return out

    def commit(self, cand: dict) -> dict:
        """Materialise a candidate: create its extract and return {template, kind, list_ref}."""
        if cand["kind"] == "input":
            return {"template": cand["template"], "kind": "input", "list_ref": None}
        if cand["kind"] == "catalog-attr":
            kind, ent, ak = cand["attr"]
            # need the entity extract on that step; find or create from any leaf mentioning the entity
            step = cand["step"]
            ex_name = None
            for n, sp in self.extracts[step].items():
                if sp.get("using") == f"catalog:{kind}":
                    ex_name = n
                    break
            if not ex_name:
                j = self.step_ids.index(step)
                gz = self.gazetteers[kind]
                for path, leaf in self.leaves_of(j):
                    if isinstance(leaf, str) and any(h["name"] == ent for h in gz.find(leaf)):
                        g, _ = generalize(path)
                        ex_name = self.add_extract(step, kind, {"from": g, "using": f"catalog:{kind}"})
                        break
            if not ex_name:
                return {"template": cand["value"], "kind": "literal", "list_ref": None}
            return {"template": "{{ catalog.%s[%s.%s].%s }}" % (kind, step, ex_name, ak), "kind": "catalog-attr", "list_ref": None,
                    "step_index": self.step_ids.index(step)}
        step, name, spec = cand["spec"]
        n = self.add_extract(step, name, spec)
        r = self.ref(step, n, cand["list_ref_flag"])
        return {"template": r["template"], "kind": cand["kind"], "list_ref": r["list_ref"], "ref": f"{step}.{n}",
                "step_index": self.step_ids.index(step)}

    # ---------------------------------------------------------------- binding
    def bind(self, value: Any, idx: int) -> dict:
        if _is_num(value):
            b = self.bind(str(value), idx) if _idlike_num(value) else {"kind": "literal"}
            if b["kind"] not in ("literal", "unresolved"):
                return b
            return {"template": value, "kind": "literal", "list_ref": None}
        if not isinstance(value, str) or not value.strip():
            return {"template": value, "kind": "literal", "list_ref": None}
        t = _ts(value)
        if t:
            w = self.bind_window(t, idx)
            if w:
                return w
        cands = [c for c in self.candidates(idx) if c["value"] == value]
        if cands:
            # prefer: input > copy > ids > catalog > regex > catalog-attr; then earliest step
            cands.sort(key=self._rank)
            best = cands[0]
            exact_idx = self.step_ids.index(best["step"]) if best.get("step") in self.step_ids else -1
            if best["kind"] == "regex" and exact_idx > 0:
                comp = self.bind_composite(value, idx, max_step=exact_idx - 1)
                if comp and comp.get("max_step", 0) < exact_idx:
                    return comp
            return self.commit(best)
        comp = self.bind_composite(value, idx)
        if comp:
            return comp
        return {"template": value, "kind": "unresolved", "list_ref": None, "pos_cands": self.position_candidates(value, idx)}

    _ORDER = {"input": 0, "copy": 1, "catalog": 1, "ids": 2, "regex": 2, "catalog-attr": 3}

    def _rank(self, c: dict) -> tuple:
        """Preference among candidates for the same value: kind, scalar before list, earlier step first. A value that is
        not the first element of its list path ranks last: `| first` would pick a different element at runtime."""
        order = self._ORDER.get(c["kind"].split(":")[0], 2)
        if c.get("list_ref_flag") and not c.get("first", True):
            order = 4
        return (order, bool(c.get("list_ref_flag")), self.step_ids.index(c["step"]) if c.get("step") in self.step_ids else -1)

    def position_candidates(self, value: str, idx: int, limit: int = 5) -> list[dict]:
        """Earlier text leaves containing the value as a whole token: (text, span) examples for the position learner."""
        out = []
        for j in range(idx):
            for path, leaf in self.leaves_of(j):
                if not isinstance(leaf, str) or len(leaf) <= len(value) or value not in leaf:
                    continue
                span = positions.example_from_value(leaf, value)
                if span:
                    g, in_list = generalize(path)
                    out.append({"step": self.step_ids[j], "from": g, "in_list": in_list, "text": leaf, "span": span})
        out.sort(key=lambda c: (c["in_list"], self.step_ids.index(c["step"])))
        return out[:limit]

    # ---------------------------------------------------------------- windows
    def window_options(self, t: datetime, idx: int) -> list[dict]:
        """Every (anchor timestamp leaf before idx, rounding) that reaches t in whole steps: {j, g, round, delta, rank}."""
        out = []
        for j in range(idx):
            for path, leaf in self.leaves_of(j):
                lt = _ts(leaf)
                if not lt:
                    continue
                for r, (rnd, step) in enumerate(ROUNDINGS):
                    a = round_down(lt, rnd) if rnd else lt
                    secs = (t - a).total_seconds()
                    if abs(secs) > 30 * 86400 or secs % step != 0:
                        continue
                    if rnd and secs == (t - lt).total_seconds():
                        continue   # rounding changed nothing: the exact option already covers it
                    out.append({"j": j, "g": generalize(path)[0], "round": rnd, "delta": t - a, "rank": r, "anchor": lt})
                    break
        return out

    def choose_anchor(self) -> None:
        """Pass 1: the anchor is a timestamp field of the earliest step whose timestamps explain any ISO-timestamp
        arg (the trigger object): scalar fields before list fields, then the earliest timestamp value (created before
        updated, created_at before resolved_at: investigations are anchored on when things started)."""
        found: dict[tuple[int, str], datetime] = {}
        for i, c in enumerate(self.s.calls):
            for v in (c.get("input") or {}).values():
                t = _ts(v)
                if not t:
                    continue
                for o in self.window_options(t, i):
                    found.setdefault((o["j"], o["g"]), o["anchor"])
        if found:
            self.anchor = min(found, key=lambda k: (k[0], "[*]" in k[1], found[k]))

    def best_option(self, t: datetime, idx: int) -> dict | None:
        opts = self.window_options(t, idx)
        if self.anchor:
            opts = [o for o in opts if (o["j"], o["g"]) == self.anchor]   # arithmetic only on the anchor field
        if not opts:
            return None
        return min(opts, key=lambda o: (o["j"], o["rank"], abs(o["delta"].total_seconds())))

    def place_window(self, step: str, g: str, rnd: str | None, sides: dict[str, str]) -> str:
        """Find or create the window extract on (step, g, round) carrying these before/after durations."""
        for want_exact in (True, False):
            for w in self.windows:
                if w["step"] != step or w["g"] != g or w.get("round") != rnd:
                    continue
                if all(w.get(k) == d for k, d in sides.items()) if want_exact else all(w.get(k) in (None, d) for k, d in sides.items()):
                    for k, d in sides.items():
                        w[k] = d
                        self.extracts[step][w["name"]][k] = d
                    return w["name"]
        spec = {"from": g, "using": "window", **sides, **({"round": rnd} if rnd else {})}
        name = self.add_extract(step, "window", spec)
        self.windows.append({"step": step, "g": g, "round": rnd, "name": name, **sides})
        return name

    def discover_windows(self, idx: int, call: dict) -> None:
        """Pass 1: the timestamps of one call that share an anchor and rounding form one window (start+end)."""
        by_key: dict[tuple, dict[str, str]] = defaultdict(dict)
        for v in (call.get("input") or {}).values():
            t = _ts(v)
            o = self.best_option(t, idx) if t else None
            if not o:
                continue
            side = "before" if o["delta"].total_seconds() <= 0 else "after"
            key = (self.step_ids[o["j"]], o["g"], o["round"])
            if by_key[key].get(side, _dur(o["delta"])) != _dur(o["delta"]):
                self.place_window(*key, {side: _dur(o["delta"])})   # two starts in one call: separate windows
            else:
                by_key[key][side] = _dur(o["delta"])
        for key, sides in by_key.items():
            self.place_window(*key, sides)

    def bind_window(self, t: datetime, idx: int) -> dict | None:
        o = self.best_option(t, idx)
        if not o:
            return None
        step, g, rnd, d = self.step_ids[o["j"]], o["g"], o["round"], o["delta"]
        side, key = ("start", "before") if d.total_seconds() <= 0 else ("end", "after")
        name = self.place_window(step, g, rnd, {key: _dur(d)})
        return {"template": "{{ %s.%s.%s }}" % (step, name, side), "kind": "window", "list_ref": None}

    def bind_composite(self, value: str, idx: int, max_step: int | None = None) -> dict | None:
        out = value
        parts, part_refs, max_ref = [], [], -1
        # dates -> window start/end dates; the word before the date decides the side when both match
        for dm in DATE_RX.finditer(value):
            d = dm.group(0)
            if d not in out:
                continue
            before_txt = value[max(0, dm.start() - 12):dm.start()].lower()
            prefer_end = any(w in before_txt for w in ("before", "until", "to:", "end"))
            for w in self.windows:
                j = self.step_ids.index(w["step"])
                if j >= idx or (max_step is not None and j > max_step):
                    continue
                anchors = [_ts(l) for p, l in self.leaves_of(j) if generalize(p)[0] == w["g"]]
                anchor = next((a for a in anchors if a), None)
                if not anchor:
                    continue
                if w.get("round"):
                    anchor = round_down(anchor, w["round"])
                sides = [("start", "before", -1), ("end", "after", 1)]
                if prefer_end:
                    sides.reverse()
                hit = None
                for side, key, sign in sides:
                    if w.get(key) and (anchor + sign * parse_duration(w[key])).strftime("%Y-%m-%d") == d:
                        hit = "{{ %s.%s.%s_date }}" % (w["step"], w["name"], side)
                        break
                if hit:
                    out = out.replace(d, hit, 1)
                    parts.append(("window", d))
                    max_ref = max(max_ref, j)
                    break
        cands = [c for c in self.candidates(idx) if len(c["value"]) >= 3 and c["value"] in out
                 and (max_step is None or c["kind"] == "input" or self.step_ids.index(c["step"]) <= max_step)]
        cands.sort(key=lambda c: (-len(c["value"]), self._rank(c)))
        for c in cands:
            lit = c["value"]
            if lit in out and _bounded(lit, out) and lit not in ("{{", "}}"):
                b = self.commit(c)
                if b["kind"] == "literal":
                    continue
                tpl = b["template"]
                out = out.replace(lit, tpl)
                parts.append((b["kind"], lit))
                part_refs.append(b.get("list_ref") or ("[" + b["ref"] + "]" if b.get("ref") else None))
                max_ref = max(max_ref, b.get("step_index", -1))
        if "{{" not in out:
            return None
        out = out.replace("{{{", "{ {{").replace("}}}", "}} }")   # keep Jinja delimiters unambiguous (PromQL braces)
        return {"template": out, "kind": "composite", "list_ref": None, "parts": parts, "max_step": max_ref,
                "part_refs": part_refs}

    # ---------------------------------------------------------------- session
    def bind_session(self) -> list[dict]:
        self.name_steps()
        # pass 1: pick the anchor, then discover windows from ISO args anywhere in the session
        self.choose_anchor()
        for i, c in enumerate(self.s.calls):
            self.discover_windows(i, c)
        steps = []
        for i, c in enumerate(self.s.calls):
            args_t, kinds, list_refs, refs, classes, pos_cands = {}, {}, {}, {}, {}, {}
            for k, v in (c.get("input") or {}).items():
                b = self.bind(v, i)
                args_t[k], kinds[k] = b["template"], b["kind"]
                classes[k] = arg_classes(b, v)
                if b.get("pos_cands"):
                    pos_cands[k] = b["pos_cands"]
                if b.get("list_ref"):
                    list_refs[k] = b["list_ref"]
                    refs[k] = b["list_ref"]
                elif b.get("ref"):
                    refs[k] = "[" + b["ref"] + "]"   # scalar ref, wrapped so it can be unioned with lists
                elif b["kind"] == "composite" and len(b.get("part_refs", [])) == 1 and b["part_refs"][0] \
                        and b["parts"][0][0].split(":")[0] in ("ids", "copy"):
                    refs[k] = b["part_refs"][0]
                    kinds[k] = "composite-elem"
            hits = 0 if c.get("is_error") else count_hits(c.get("output"), None)
            steps.append({"idx": i, "id": self.step_ids[i], "tool": f"{c['server']}.{c['tool']}", "args": args_t,
                          "kinds": kinds, "list_refs": list_refs, "refs": refs, "hits": hits, "raw_args": c.get("input") or {},
                          "classes": classes, "pos_cands": pos_cands})
        return self.collapse(steps)

    def collapse(self, steps: list[dict]) -> list[dict]:
        out: list[dict] = []
        for st in steps:
            prev = out[-1] if out else None
            if prev and prev["tool"] == st["tool"]:
                diff = [k for k in set(prev["args"]) | set(st["args"]) if prev["args"].get(k) != st["args"].get(k)]
                raw_diff = [k for k in set(prev["raw_args"]) | set(st["raw_args"]) if prev["raw_args"].get(k) != st["raw_args"].get(k)]
                if len(raw_diff) == 1 and len(diff) <= 1:
                    k = raw_diff[0]
                    if _elem(st, k) and (_elem(prev, k) or prev.get("forEach") == k) and _shape(st, k) == (prev.get("shape") or _shape(prev, k)):
                        refs = set(prev.get("forEach_refs", [])) | {r for r in (prev["refs"].get(k), st["refs"].get(k)) if r}
                        prev["forEach"], prev["forEach_refs"], prev["shape"] = k, sorted(refs), _shape(st, k)
                        prev["args"][k] = _shape(st, k)
                        prev["count"] = prev.get("count", 1) + 1
                        prev["hits"] += st["hits"]
                        continue
                if len(diff) == 1:
                    k = diff[0]
                    if prev["hits"] == 0 and not prev.get("forEach"):
                        rungs = prev.get("ladder", [(prev["args"][k], prev["hits"])])
                        rungs.append((st["args"][k], st["hits"]))
                        prev.update(ladder=rungs, ladder_key=k, hits=st["hits"])
                        prev["args"][k] = st["args"][k]
                        prev["classes"][k] = prev["classes"].get(k, frozenset()) | st["classes"].get(k, frozenset())
                        continue
            out.append(dict(st))
        seen = Counter()
        for st in out:
            base = re.sub(r"_\d+$", "", st["id"])
            seen[base] += 1
            st["id"] = base if seen[base] == 1 else f"{base}_{seen[base]}"
        return out


def _elem(st: dict, k: str) -> bool:
    return st["kinds"].get(k, "").startswith("ids:") or st["kinds"].get(k) in ("copy", "composite-elem")


def _shape(st: dict, k: str) -> str:
    return re.sub(r"\{\{[^}]*\}\}", "{{ item }}", str(st["args"].get(k, "")))


# -------------------------------------------------------------------- signatures
def _class_of(kind: str, value: Any) -> str:
    if kind == "window" or _ts(value):
        return "window"
    if kind == "input":
        return "text"
    base = kind.split(":")[0]
    if base == "ids":
        t = kind.split(":", 1)[1]
        return "window" if t in ("iso_ts", "date") else ("text" if t in TEXTY_TYPES else "id")
    if base in ("catalog", "regex", "catalog-attr", "position"):
        return "text"
    if base == "copy":
        if isinstance(value, str) and (re.fullmatch(r"-?\d+", value) or (ids.type_of(value) or "") not in TEXTY_TYPES | {""}):
            return "id"
        return "text"
    return "text"


def arg_classes(b: dict, raw: Any) -> frozenset[str]:
    """What an argument's binding refers to: {text, id, window} (empty = literal/unresolved, a wildcard)."""
    if _ts(raw):
        return frozenset({"window"})
    kind = b["kind"]
    if kind in ("literal", "unresolved"):
        return frozenset()
    if kind == "composite":
        return frozenset(_class_of(pk, pv) for pk, pv in b.get("parts", [])) or frozenset({"text"})
    return frozenset({_class_of(kind, raw)})


def _compatible(st: dict, group: dict) -> tuple[bool, int]:
    """Same tool and overlapping classes on every argument both bind. Returns (ok, score)."""
    if st["tool"] != group["tool"]:
        return False, 0
    score = 0
    for k, cs in st["classes"].items():
        cg = group["classes"].get(k)
        if cg is None:
            continue
        if cs and cg and not (cs & cg):
            return False, 0
        if cs & cg:
            score += 1
        if _hashable(st["args"].get(k)) in group["templates"].get(k, ()):
            score += 2
    return True, score


def _fanout_mate(st: dict, mate: dict) -> str | None:
    """The key on which st is a further fan-out of mate (same {{ item }} shape, id class), else None."""
    k = mate.get("forEach") or st.get("forEach")
    if not k:
        keys = [k for k, cs in st["classes"].items() if cs == {"id"} and st["refs"].get(k)]
        k = keys[0] if len(keys) == 1 else None
    if not k or not (st["refs"].get(k) or st.get("forEach_refs")) or not (mate["refs"].get(k) or mate.get("forEach_refs")):
        return None
    if _shape(st, k) != (mate.get("shape") or _shape(mate, k)):
        return None
    for other in set(st["args"]) | set(mate["args"]):
        if other != k and st["args"].get(other) != mate["args"].get(other):
            return None
    return k


def align(bound: list[dict]) -> list[dict]:
    """Group steps across sessions by signature. Each group: {tool, members: [(si, pos, st)], classes, templates}."""
    groups: list[dict] = []
    for si, b in enumerate(bound):
        used: dict[int, dict] = {}   # group index -> the member from this session
        for pos, st in enumerate(b["steps"]):
            ranked = []
            for gi, g in enumerate(groups):
                ok, score = _compatible(st, g)
                if ok:
                    ranked.append((-score, abs(pos - g["mean_pos"]), gi))
            ranked.sort()
            target = None
            for _, _, gi in ranked:
                if gi not in used:
                    target = gi
                    break
                k = _fanout_mate(st, used[gi])
                if k:
                    st["fanout_key"] = k
                    target = gi
                    break
            if target is None:
                groups.append({"tool": st["tool"], "members": [], "classes": {}, "templates": defaultdict(set), "mean_pos": pos})
                target = len(groups) - 1
            g = groups[target]
            g["members"].append((si, pos, st))
            used.setdefault(target, st)
            for k, cs in st["classes"].items():
                g["classes"][k] = g["classes"].get(k, frozenset()) | cs
            for k, v in st["args"].items():
                g["templates"][k].add(_hashable(v))
            g["mean_pos"] = sum(p for _, p, _ in g["members"]) / len(g["members"])
    return groups


def _rewrite(value: Any, ref_map: dict[str, str], step_map: dict[str, str]) -> Any:
    """Rename `step.extract` references inside templates (and bare refs) per the canonical maps."""
    if isinstance(value, list):
        return [_rewrite(v, ref_map, step_map) for v in value]
    if isinstance(value, dict):
        return {k: _rewrite(v, ref_map, step_map) for k, v in value.items()}
    if not isinstance(value, str):
        return value

    def sub(m):
        step, name = m.group(1), m.group(2)
        if f"{step}.{name}" in ref_map:
            return ref_map[f"{step}.{name}"]
        if step in step_map:
            return f"{step_map[step]}.{name}"
        return m.group(0)
    if "{{" in value:
        return TPL_RX.sub(lambda m: REF_RX.sub(sub, m.group(0)), value)
    return REF_RX.sub(sub, value)


def _window_name(spec: dict) -> str:
    return "window_" + "_".join(x for x in (spec.get("before"), spec.get("after"), ("r" + spec["round"]) if spec.get("round") else None) if x)


def canonicalize(bound: list[dict], groups: list[dict], gids: list[str]) -> dict[str, dict]:
    """Assign global step ids and extract names, rewrite every template in every session. Returns global extracts."""
    step_maps: list[dict[str, str]] = [{} for _ in bound]
    for g, gid in zip(groups, gids):
        for si, _, st in g["members"]:
            step_maps[si][st["id"]] = gid
    # per global step: spec -> sessions and the local names they used
    by_step: dict[str, dict[str, dict]] = defaultdict(dict)
    for si, b in enumerate(bound):
        for local_sid, exs in b["extracts"].items():
            gid = step_maps[si].get(local_sid)
            if not gid:
                continue
            for nm, spec in exs.items():
                key = json.dumps(spec, sort_keys=True)
                e = by_step[gid].setdefault(key, {"spec": spec, "sessions": set(), "names": Counter()})
                e["sessions"].add(si)
                e["names"][nm] += 1
    extracts: dict[str, dict] = defaultdict(dict)
    global_name: dict[tuple[str, str], str] = {}   # (gid, spec key) -> name
    for gid, specs in by_step.items():
        taken: set[str] = set()
        ordered = sorted(specs.items(), key=lambda kv: (-len(kv[1]["sessions"]), kv[1]["names"].most_common(1)[0][0]))
        for key, e in ordered:
            spec = e["spec"]
            if spec.get("using") == "window":
                name = "window" if "window" not in taken else _window_name(spec)
            else:
                name = e["names"].most_common(1)[0][0]
            base, i = name, 2
            while name in taken:
                name, i = f"{base}_{i}", i + 1
            taken.add(name)
            global_name[(gid, key)] = name
            extracts[gid][name] = spec
    # rewrite templates in every session
    for si, b in enumerate(bound):
        ref_map = {}
        for local_sid, exs in b["extracts"].items():
            gid = step_maps[si].get(local_sid)
            if not gid:
                continue
            for nm, spec in exs.items():
                ref_map[f"{local_sid}.{nm}"] = f"{gid}.{global_name[(gid, json.dumps(spec, sort_keys=True))]}"
        for st in b["steps"]:
            st["args"] = _rewrite(st["args"], ref_map, step_maps[si])
            for key in ("refs", "list_refs"):
                st[key] = _rewrite(st[key], ref_map, step_maps[si])
            for key in ("forEach_refs", "shape"):
                if key in st:
                    st[key] = _rewrite(st[key], ref_map, step_maps[si])
            if st.get("ladder"):
                st["ladder"] = [(_rewrite(t, ref_map, step_maps[si]), h) for t, h in st["ladder"]]
            for k, cands in st.get("pos_cands", {}).items():
                for c in cands:
                    c["gstep"] = step_maps[si].get(c["step"], c["step"])
            st["gid"] = step_maps[si].get(st["id"], st["id"])
    return extracts


def solve_positions(groups: list[dict], gids: list[str], extracts: dict[str, dict], n_sessions: int) -> dict:
    """For each (step, arg) unresolved in several sessions, learn a position program over one source text
    field from the (text, span) pairs those sessions provide; on success bind the arg to a new extract."""
    solved = {}
    for g, gid in zip(groups, gids):
        keys = sorted(set().union(*(st["args"].keys() for _, _, st in g["members"])))
        for k in keys:
            todo = [st for _, _, st in g["members"] if st["kinds"].get(k) == "unresolved" and st.get("pos_cands", {}).get(k)]
            if len({json.dumps(st["raw_args"].get(k), default=str) for st in todo}) < 2:
                continue
            sources: dict[tuple[str, str], list] = defaultdict(list)
            for st in todo:
                seen = set()
                for c in st["pos_cands"][k]:
                    src = (c["gstep"], c["from"])
                    if src not in seen:
                        seen.add(src)
                        sources[src].append((st, c))
            for (src, path), items in sorted(sources.items(), key=lambda kv: -len(kv[1])):
                if len(items) < len(todo):
                    continue   # the source must explain every unresolved session
                pairs = [(c["text"], c["span"]) for _, c in items]
                progs = positions.learn(pairs)
                if not progs or not held_out_ok(pairs):
                    continue
                prog = positions.serializable(progs[0])
                spec = {"from": path, "using": "position", "program": prog}
                name = k if k not in extracts[src] else f"{k}_pos"
                i = 2
                while name in extracts[src] and extracts[src][name] != spec:
                    name, i = f"{k}_pos{i}", i + 1
                extracts[src][name] = spec
                for st, _ in items:
                    st["args"][k] = "{{ %s.%s }}" % (src, name)
                    st["kinds"][k] = "position"
                    st["classes"][k] = frozenset({"text"})
                    st["refs"][k] = f"[{src}.{name}]"
                solved[f"{gid}.{k}"] = {"from": f"{src}.{path}", "program": positions.describe(progs[0]), "sessions": len(items)}
                break
    return solved


def held_out_ok(pairs: list[tuple[str, tuple[int, int]]]) -> bool:
    """Held-out gate for a position program: for every example, a program learned from the others alone must
    reproduce it. A program that only memorises its training spans (an ordinal that happens to fit) fails here
    instead of at runtime on the next ticket."""
    if len(pairs) < 2:
        return False
    for i, (text, (s, e)) in enumerate(pairs):
        progs = positions.learn(pairs[:i] + pairs[i + 1:])
        if not progs or positions.apply(progs[0], text) != text[s:e]:
            return False
    return True


def _window_width(tpl: Any, extracts: dict[str, dict]) -> float:
    m = re.fullmatch(r"\{\{\s*(\w+)\.(\w+)\.(start|end)\s*\}\}", str(tpl))
    if not m:
        return 0.0
    spec = extracts.get(m.group(1), {}).get(m.group(2), {})
    key = "before" if m.group(3) == "start" else "after"
    try:
        return parse_duration(spec.get(key, "0s")).total_seconds()
    except ValueError:
        return 0.0


def _deps(st: dict, ids_: set[str]) -> set[str]:
    text = json.dumps({k: v for k, v in st.items() if k in ("args", "forEach", "when")})
    return ({m.group(1) for m in REF_RX.finditer(text)} & ids_) - {st["id"]}


def _refs_in(v: Any) -> set[str]:
    return {m.group(1) for m in REF_RX.finditer(str(v))}


def _cut(st: dict, unmet: set[str], dry: bool = False) -> tuple[bool, list[str]]:
    """Remove the references to `unmet` steps: ladder rungs and forEach terms go; a scalar template stays (it
    renders blank at runtime). Returns (clean, what was cut); clean = nothing had to stay. dry: only decide."""
    cut, clean = [], True
    for k, v in list(st["args"].items()):
        if isinstance(v, dict) and "ladder" in v:
            keep = [r for r in v["ladder"] if not (_refs_in(r) & unmet)]
            if keep and len(keep) < len(v["ladder"]):
                cut += [f"{k}: {r}" for r in v["ladder"] if r not in keep]
                if not dry:
                    st["args"][k] = {"ladder": keep} if len(keep) > 1 else keep[0]
            elif not keep:
                clean = False
                cut.append(f"{k}: every rung references {sorted(unmet)} (kept; renders blank)")
        elif _refs_in(v) & unmet:
            clean = False
            cut.append(f"{k}: {v} (kept; renders blank)")
    fe = st.get("forEach")
    if fe and (_refs_in(fe) & unmet):
        m = re.fullmatch(r"\(?(.*?)\)?\s*\|\s*unique\s*\|\s*list", fe)
        terms = [t.strip() for t in (m.group(1) if m else fe).split(" + ")]
        keep = [t for t in terms if not (_refs_in(t) & unmet)]
        cut += [f"forEach: {t}" for t in terms if t not in keep]
        if not dry:
            st["forEach"] = (f"{keep[0]} | unique | list" if len(keep) == 1 else "(" + " + ".join(keep) + ") | unique | list") if keep else "[]"
    return clean, cut


def _sort(steps: list[dict]) -> bool:
    """Move each step after the last step it references, in place. False when that never settles (a cycle)."""
    ids_ = [s["id"] for s in steps]
    for _ in range(len(steps) ** 2):
        moved = False
        for i, st in enumerate(steps):
            last = max((ids_.index(d) for d in _deps(st, set(ids_))), default=-1)
            if last > i:
                steps.insert(last + 1, steps.pop(i))
                ids_ = [s["id"] for s in steps]
                moved = True
                break
        if not moved:
            return True
    return False


def _topo(steps: list[dict], cuts: dict | None = None) -> list[dict]:
    """Reorder so every step comes after the steps its templates reference. Sessions that did things in opposite
    orders can leave a cycle (A's ladder falls back to B, B's to A; the fan-out of A includes what B found): one
    forward reference is cut per round, preferring a step that loses only a fallback rung or a fan-out term over
    one whose scalar template would render blank; every cut goes to the report."""
    for _ in range(len(steps) + 1):
        order = list(steps)
        if _sort(steps):
            return steps
        steps[:] = order                       # decide the cut on the majority order, not a half-sorted one
        ids_ = [s["id"] for s in steps]
        fwd = [(st, {d for d in _deps(st, set(ids_)) if ids_.index(d) > i}) for i, st in enumerate(steps)]
        fwd = [(st, u) for st, u in fwd if u]
        st, unmet = next(((st, u) for st, u in fwd if _cut(dict(st, args=dict(st["args"])), u, dry=True)[0]), fwd[0])
        if cuts is not None:
            cuts[st["id"]] = _cut(st, unmet)[1]
    return steps


def induce(sessions: list[Session], name: str, catalog: dict | None = None) -> tuple[dict, dict]:
    catalog = catalog if catalog is not None else load_catalog()
    bound = []
    for s in sessions:
        b = Binder(s, catalog)
        steps = b.bind_session()
        bound.append({"session": s.session_id, "steps": steps, "extracts": {k: dict(v) for k, v in b.extracts.items()}})
    n = len(bound)
    groups = align(bound)
    # global step ids: the members' majority local id; larger groups claim names first
    gids: list[str | None] = [None] * len(groups)
    taken: set[str] = set()
    for gi in sorted(range(len(groups)), key=lambda i: (-len({si for si, _, _ in groups[i]["members"]}), groups[i]["mean_pos"])):
        base = Counter(st["id"] for _, _, st in groups[gi]["members"]).most_common(1)[0][0]
        gid, i = base, 2
        while gid in taken:
            gid, i = f"{base}_{i}", i + 1
        taken.add(gid)
        gids[gi] = gid
    extracts = canonicalize(bound, groups, gids)
    position_solved = solve_positions(groups, gids, extracts, n)
    merged, window_alts, kinds_report, dropped_rungs = [], {}, {}, {}
    for g, sid in zip(groups, gids):
        items = [st for _, _, st in g["members"]]
        n_sess = len({si for si, _, _ in g["members"]})
        keys = sorted(set().union(*(st["args"].keys() for st in items)))
        args: dict[str, Any] = {}
        unresolved = {}
        kinds_report[sid] = {}
        for k in keys:
            variants: dict[Any, list[int]] = defaultdict(list)
            sessions_of: dict[Any, set] = defaultdict(set)
            raw_vals = set()
            unbound: set = set()          # literal values no binding explained (their sessions' raw values)
            is_ts = False
            for si, _, st in g["members"]:
                if k not in st["args"]:
                    continue
                if st.get("ladder") and st.get("ladder_key") == k:
                    for tpl, hits in st["ladder"]:
                        variants[_hashable(tpl)].append(hits)
                        sessions_of[_hashable(tpl)].add(si)
                else:
                    variants[_hashable(st["args"][k])].append(st["hits"])
                    sessions_of[_hashable(st["args"][k])].add(si)
                if st["kinds"].get(k) == "unresolved":
                    raw_vals.add(json.dumps(st["raw_args"].get(k), default=str))
                    unbound.add(_hashable(st["raw_args"].get(k)))
                if _ts(st["raw_args"].get(k)) or st["classes"].get(k) == {"window"}:
                    is_ts = True
                kinds_report[sid].setdefault(k, Counter())[st["kinds"].get(k, "literal")] += 1
            if len(variants) == 1:
                args[k] = _coerce(next(iter(variants)))
            elif all(not (isinstance(t, str) and "{{" in t) for t in variants):
                args[k] = _coerce(Counter({t: len(h) for t, h in variants.items()}).most_common(1)[0][0])
            elif is_ts:
                # timestamp/window args never ladder: majority of sessions, then the widest window
                ranked = sorted(variants, key=lambda t: (-len(sessions_of[t]), -_window_width(t, extracts), str(t)))
                args[k] = _coerce(ranked[0])
                window_alts.setdefault(sid, {})[k] = {str(t): len(sessions_of[t]) for t in ranked}
            else:
                # a literal rung that one session used and nothing explains is a hardcoded value in disguise (one ticket's
                # sha, one DM's words): it can never hit for another input, so it is dropped and reported instead
                dropped = [t for t in variants if not (isinstance(t, str) and "{{" in t) and t in unbound and len(sessions_of[t]) == 1]
                if dropped:
                    dropped_rungs.setdefault(sid, {})[k] = sorted(str(t) for t in dropped)
                ranked = sorted(((t, h) for t, h in variants.items() if t not in dropped),
                                key=lambda kv: (-(sum(1 for h in kv[1] if h > 0) / len(kv[1])), -len(kv[1]), -str(kv[0]).count("{{")))
                args[k] = {"ladder": [_coerce(t) for t, _ in ranked]} if len(ranked) > 1 else _coerce(ranked[0][0])
            if len(raw_vals) > 1:
                unresolved[k] = sorted(raw_vals)
        step: dict[str, Any] = {"id": sid, "title": sid.replace("_", " "), "tool": g["tool"], "args": args}
        fe = [st for st in items if st.get("forEach")]
        fk = fe[0]["forEach"] if fe else next((st["fanout_key"] for st in items if st.get("fanout_key")), None)
        if fk:
            shape = next((st.get("shape") for st in fe), None) or next((_shape(st, fk) for st in items if st.get("fanout_key")), "{{ item }}")
            refs = set()
            count = 1
            for st in items:
                if st.get("forEach") == fk:
                    refs |= set(st.get("forEach_refs", []))
                    count = max(count, st.get("count", 1))
                elif st["refs"].get(fk) and _shape(st, fk) == shape:
                    refs.add(st["refs"][fk])
            refs = sorted(refs)
            expr = refs[0] if len(refs) == 1 else "(" + " + ".join(refs) + ")"
            step["forEach"] = f"{expr} | unique | list" if refs else "[]"
            step["max_items"] = count + 2
            step["args"][fk] = shape
        if n_sess < n:
            step["optional"] = True
            step["seen_in"] = f"{n_sess}/{n} sessions"
        if unresolved:
            step["unresolved"] = unresolved
        merged.append((g["mean_pos"], step))
    merged.sort(key=lambda x: x[0])
    cut_refs: dict[str, list[str]] = {}
    steps = _topo([s for _, s in merged], cut_refs)
    text_all = json.dumps(steps)
    for st in steps:
        exs = {nm: sp for nm, sp in extracts.get(st["id"], {}).items() if re.search(r"\b%s\.%s\b" % (re.escape(st["id"]), re.escape(nm)), text_all)}
        if exs:
            st["extract"] = exs
    inputs = {}
    for s in sessions:
        for k, v in (s.meta.get("inputs") or {}).items():
            inputs.setdefault(k, {"type": ids.type_of(str(v)) or "string", "required": True, "example": v})
    # regression cases (`crystal test`): one per distinct traced input; every step that had hits in every session
    # must find something again, so a run where the steps quietly return nothing is a failure, not a pass
    always_hit = {gid for g, gid in zip(groups, gids)
                  if len({si for si, _, _ in g["members"]}) == n and all(st["hits"] > 0 for _, _, st in g["members"])}
    expect = {st["id"]: {"min_hits": 1} for st in steps if st["id"] in always_hit}
    tests, seen_inputs = [], set()
    for s in sessions:
        inp = s.meta.get("inputs") or {}
        key = json.dumps(inp, sort_keys=True, default=str)
        if inp and key not in seen_inputs:
            seen_inputs.add(key)
            tests.append({"inputs": dict(inp), "expect": {k: dict(v) for k, v in expect.items()}})   # fresh dicts: no YAML anchors
    flow = {"name": name, "title": name.replace("-", " "), "status": "draft", "version": 1,
            "induced_from": [s.session_id for s in sessions],
            "trigger": {"type": sessions[0].meta.get("trigger", "unknown")}, "inputs": inputs, "steps": steps, "tests": tests}
    alignment = {gid: {"sessions": len({si for si, _, _ in g["members"]}),
                       "local_ids": dict(Counter(st["id"] for _, _, st in g["members"]))}
                 for g, gid in zip(groups, gids)}
    report = {"sessions": n, "steps": len(steps),
              "unresolved": {st["id"]: st["unresolved"] for st in steps if st.get("unresolved")},
              "optional_steps": [f"{st['id']} ({st['seen_in']})" for st in steps if st.get("optional")],
              "ladders": {st["id"]: {k: len(v["ladder"]) for k, v in st["args"].items() if isinstance(v, dict) and "ladder" in v} for st in steps if any(isinstance(v, dict) and "ladder" in v for v in st["args"].values())},
              "forEach": {st["id"]: st["forEach"] for st in steps if st.get("forEach")},
              "window_alternatives": window_alts,
              "dropped_rungs": dropped_rungs,
              "cut_refs": cut_refs,
              "position_programs": position_solved,
              "tests": {"cases": len(tests), "min_hits": sorted(expect)},
              "alignment": alignment,
              "kinds": {sid: {k: dict(c) for k, c in ks.items()} for sid, ks in kinds_report.items()}}
    # every draft carries a deterministic flow card (crystal/flow/cards.py); `crystal author` overlays the agent's prose
    flow["card"] = skeleton_card(flow, report)
    return flow, report


def _hashable(v: Any) -> Any:
    return v if isinstance(v, (str, int, float, bool)) or v is None else json.dumps(v, sort_keys=True)


def _coerce(v: Any) -> Any:
    """Reverse _hashable: lists/dicts come back from their JSON form. Strings stay strings even when numeric
    (a "20014" page id must not become the int 20014; the tool schema says string)."""
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def dump_flow(flow: dict) -> str:
    class D(yaml.SafeDumper):
        pass

    def str_presenter(dumper, data):
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    D.add_representer(str, str_presenter)
    return yaml.dump(flow, Dumper=D, sort_keys=False, width=140, allow_unicode=True)
