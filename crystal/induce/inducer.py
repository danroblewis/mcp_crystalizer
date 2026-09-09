"""Trace -> flow inducer (no LLM).

For every argument of every recorded call, search the session's earlier results for the value and label the
binding (TraceCompiler's taxonomy): input | copy (exact leaf) | ids:<type> | catalog:<kind> | catalog-attr |
regex (line-anchored "Label: value") | window (timestamp arithmetic) | composite (several of those inside a
search string) | literal | unresolved. Binding runs in two passes so windows discovered from later calls
(logz from/to) can explain dates inside earlier search strings (slack after:/before:). Consecutive calls to
the same tool collapse into forEach (one arg varies over a list) or a ladder (earlier rungs had no hits).
Steps are aligned across sessions by (tool, occurrence) and merged: literals by majority, rungs ordered by
success rate. Values that differ across sessions with no binding are reported as unresolved.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

import yaml

from crystal.extract import ids
from crystal.extract.catalog import Gazetteer, load_catalog
from crystal.extract.extractors import parse_duration
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


class Binder:
    def __init__(self, session: Session, catalog: dict):
        self.s = session
        self.inputs = session.meta.get("inputs") or {}
        self.catalog = catalog
        self.gazetteers = {k: Gazetteer(v) for k, v in catalog.items() if isinstance(v, dict)}
        self.step_ids: list[str] = []
        self.extracts: dict[str, dict] = defaultdict(dict)
        self.windows: list[dict] = []   # {step, g, name, before, after}
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
        if n in self.extracts[step]:
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
            for path, leaf in self.leaves_of(j):
                if not isinstance(leaf, str) or not leaf:
                    continue
                g, in_list = generalize(path)
                # exact leaf copy
                if len(leaf) <= 200 and "\n" not in leaf:
                    kind = ids.type_of(leaf) or path.split(".")[-1].split("[")[0].strip("'")
                    spec = {"from": g, **({"all": True} if in_list else {})}
                    out.append({"value": leaf, "kind": "copy", "spec": (step, _plural(kind) if in_list else kind, spec), "list_ref_flag": in_list, "step": step})
                # typed ids inside the leaf
                for m in ids.typed_mentions(leaf):
                    if m["value"] == leaf:
                        continue
                    spec = {"from": g, "using": f"ids:{m['type']}", **({"all": True} if in_list else {})}
                    out.append({"value": m["value"], "kind": f"ids:{m['type']}", "spec": (step, _plural(m["type"]) if in_list else m["type"], spec), "list_ref_flag": in_list, "step": step})
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
            spec = {"from": None, "using": f"catalog:{kind}"}
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
            return {"template": "{{ catalog.%s[%s.%s].%s }}" % (kind, step, ex_name, ak), "kind": "catalog-attr", "list_ref": None}
        step, name, spec = cand["spec"]
        n = self.add_extract(step, name, spec)
        r = self.ref(step, n, cand["list_ref_flag"])
        return {"template": r["template"], "kind": cand["kind"], "list_ref": r["list_ref"], "ref": f"{step}.{n}",
                "step_index": self.step_ids.index(step)}

    # ---------------------------------------------------------------- binding
    def bind(self, value: Any, idx: int) -> dict:
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
        return {"template": value, "kind": "unresolved", "list_ref": None}

    _ORDER = {"input": 0, "copy": 1, "catalog": 1, "ids": 2, "regex": 2, "catalog-attr": 3}

    def _rank(self, c: dict) -> tuple:
        """Preference among candidates for the same value: kind, scalar before list, earlier step first."""
        return (self._ORDER.get(c["kind"].split(":")[0], 2), bool(c.get("list_ref_flag")),
                self.step_ids.index(c["step"]) if c.get("step") in self.step_ids else -1)

    def bind_window(self, t: datetime, idx: int) -> dict | None:
        best = None
        for j in range(idx):
            for path, leaf in self.leaves_of(j):
                lt = _ts(leaf)
                if not lt:
                    continue
                secs = (t - lt).total_seconds()
                if abs(secs) > 30 * 86400 or secs % 900 != 0:
                    continue
                if best is None or abs(secs) < best[0]:
                    best = (abs(secs), j, path, t - lt)
            if best:
                break  # the earliest step holding a plausible anchor wins (the trigger object)
        if not best:
            return None
        _, j, path, d = best
        step, (g, _) = self.step_ids[j], generalize(path)
        side, key = ("start", "before") if d.total_seconds() <= 0 else ("end", "after")
        for w in self.windows:
            if w["step"] == step and w["g"] == g and w.get(key) in (None, _dur(d)):
                w[key] = _dur(d)
                self.extracts[step][w["name"]][key] = _dur(d)
                return {"template": "{{ %s.%s.%s }}" % (step, w["name"], side), "kind": "window", "list_ref": None}
        spec = {"from": g, "using": "window", key: _dur(d)}
        name = self.add_extract(step, "window", spec)
        self.windows.append({"step": step, "g": g, "name": name, key: _dur(d)})
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
                    parts.append("window")
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
                parts.append(b["kind"])
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
        # pass 1: discover windows from ISO args anywhere in the session
        for i, c in enumerate(self.s.calls):
            for v in (c.get("input") or {}).values():
                if isinstance(v, str) and _ts(v):
                    self.bind_window(_ts(v), i)
        steps = []
        for i, c in enumerate(self.s.calls):
            args_t, kinds, list_refs, refs = {}, {}, {}, {}
            for k, v in (c.get("input") or {}).items():
                b = self.bind(v, i)
                args_t[k], kinds[k] = b["template"], b["kind"]
                if b.get("list_ref"):
                    list_refs[k] = b["list_ref"]
                    refs[k] = b["list_ref"]
                elif b.get("ref"):
                    refs[k] = "[" + b["ref"] + "]"   # scalar ref, wrapped so it can be unioned with lists
                elif b["kind"] == "composite" and len(b.get("part_refs", [])) == 1 and b["part_refs"][0] \
                        and b["parts"][0].split(":")[0] in ("ids", "copy"):
                    refs[k] = b["part_refs"][0]
                    kinds[k] = "composite-elem"
            hits = 0 if c.get("is_error") else count_hits(c.get("output"), None)
            steps.append({"idx": i, "id": self.step_ids[i], "tool": f"{c['server']}.{c['tool']}", "args": args_t,
                          "kinds": kinds, "list_refs": list_refs, "refs": refs, "hits": hits, "raw_args": c.get("input") or {}})
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
                    elem = lambda x: x["kinds"].get(k, "").startswith("ids:") or x["kinds"].get(k) in ("copy", "composite-elem")  # noqa: E731
                    shape = lambda x: re.sub(r"\{\{[^}]*\}\}", "{{ item }}", str(x["args"].get(k, "")))  # noqa: E731
                    if elem(st) and (elem(prev) or prev.get("forEach") == k) and shape(st) == (prev.get("shape") or shape(prev)):
                        refs = set(prev.get("forEach_refs", [])) | {r for r in (prev["refs"].get(k), st["refs"].get(k)) if r}
                        prev["forEach"], prev["forEach_refs"], prev["shape"] = k, sorted(refs), shape(st)
                        prev["args"][k] = shape(st)
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
                        continue
            out.append(dict(st))
        seen = Counter()
        for st in out:
            base = re.sub(r"_\d+$", "", st["id"])
            seen[base] += 1
            st["id"] = base if seen[base] == 1 else f"{base}_{seen[base]}"
        return out


def induce(sessions: list[Session], name: str, catalog: dict | None = None) -> tuple[dict, dict]:
    catalog = catalog if catalog is not None else load_catalog()
    bound = []
    for s in sessions:
        b = Binder(s, catalog)
        steps = b.bind_session()
        bound.append({"session": s.session_id, "steps": steps, "extracts": {k: dict(v) for k, v in b.extracts.items()}})
    n = len(bound)
    groups: dict[tuple[str, int], list[tuple[int, dict]]] = defaultdict(list)
    for b in bound:
        occ = Counter()
        for pos, st in enumerate(b["steps"]):
            occ[st["tool"]] += 1
            groups[(st["tool"], occ[st["tool"]])].append((pos, st))
    merged = []
    for (tool, occ), items in groups.items():
        mean_pos = sum(p for p, _ in items) / len(items)
        sid = Counter(st["id"] for _, st in items).most_common(1)[0][0]
        keys = sorted(set().union(*(st["args"].keys() for _, st in items)))
        args: dict[str, Any] = {}
        unresolved = {}
        for k in keys:
            variants: dict[Any, list[int]] = defaultdict(list)
            raw_vals = set()
            for _, st in items:
                if st.get("ladder") and st.get("ladder_key") == k:
                    for tpl, hits in st["ladder"]:
                        variants[_hashable(tpl)].append(hits)
                elif k in st["args"]:
                    variants[_hashable(st["args"][k])].append(st["hits"])
                if st["kinds"].get(k) == "unresolved":
                    raw_vals.add(json.dumps(st["raw_args"].get(k), default=str))
            if len(variants) == 1:
                args[k] = _coerce(next(iter(variants)))
            elif all(not (isinstance(t, str) and "{{" in t) for t in variants):
                args[k] = _coerce(Counter({t: len(h) for t, h in variants.items()}).most_common(1)[0][0])
            else:
                ranked = sorted(variants.items(), key=lambda kv: (-(sum(1 for h in kv[1] if h > 0) / len(kv[1])), -len(kv[1]), -str(kv[0]).count("{{")))
                args[k] = {"ladder": [_coerce(t) for t, _ in ranked]}
            if len(raw_vals) > 1:
                unresolved[k] = sorted(raw_vals)
        step: dict[str, Any] = {"id": sid, "title": sid.replace("_", " "), "tool": tool, "args": args}
        fe = [st for _, st in items if st.get("forEach")]
        if fe:
            refs = sorted(set(r for st in fe for r in st.get("forEach_refs", [])))
            expr = refs[0] if len(refs) == 1 else "(" + " + ".join(refs) + ")"
            step["forEach"] = f"{expr} | unique | list" if refs else "[]"
            step["max_items"] = max(st.get("count", 1) for st in fe) + 2
            k = fe[0]["forEach"]
            step["args"][k] = fe[0].get("shape") or "{{ item }}"
        if len(items) < n:
            step["optional"] = True
            step["seen_in"] = f"{len(items)}/{n} sessions"
        if unresolved:
            step["unresolved"] = unresolved
        merged.append((mean_pos, step))
    merged.sort(key=lambda x: x[0])
    steps = [s for _, s in merged]
    extracts: dict[str, dict] = defaultdict(dict)
    for b in bound:
        for sid, exs in b["extracts"].items():
            for nm, spec in exs.items():
                extracts[sid].setdefault(nm, spec)
    text_all = json.dumps(steps)
    for st in steps:
        exs = {nm: sp for nm, sp in extracts.get(st["id"], {}).items() if re.search(r"\b%s\.%s\b" % (re.escape(st["id"]), re.escape(nm)), text_all)}
        if exs:
            st["extract"] = exs
    inputs = {}
    for s in sessions:
        for k, v in (s.meta.get("inputs") or {}).items():
            inputs.setdefault(k, {"type": ids.type_of(str(v)) or "string", "required": True, "example": v})
    flow = {"name": name, "title": name.replace("-", " "), "status": "draft", "version": 1,
            "induced_from": [s.session_id for s in sessions],
            "trigger": {"type": sessions[0].meta.get("trigger", "unknown")}, "inputs": inputs, "steps": steps}
    report = {"sessions": n, "steps": len(steps),
              "unresolved": {st["id"]: st["unresolved"] for st in steps if st.get("unresolved")},
              "optional_steps": [f"{st['id']} ({st['seen_in']})" for st in steps if st.get("optional")],
              "ladders": {st["id"]: {k: len(v["ladder"]) for k, v in st["args"].items() if isinstance(v, dict) and "ladder" in v} for st in steps if any(isinstance(v, dict) and "ladder" in v for v in st["args"].values())},
              "forEach": {st["id"]: st["forEach"] for st in steps if st.get("forEach")}}
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
