"""Provenance: for every value the extractors found, where it CAME FROM and where it WENT.

The dossier used to mark an extracted value with the step that extracted it. That answers half of the question --
the other half, "did anything ever use this value?", is what turns a highlight into a fact about the research. A
value that was carried into a later call is the flow actually working; a value that was only ever detected is
noise the extractors happened to see.

Consumption is decided the way `crystal.induce.dataflow` decides an edge: an argument's value is explained by an
earlier result when it *is* that result's value (or contains it). The run record already holds every call's
RENDERED args, so no binder and no templating is needed here -- a value is carried when it occurs inside a later
step's rendered arguments, and the argument it landed in names the edge. The result is reported as
`crystal.induce.dataflow.CallEdge`s (that module's own taxonomy: `ids:<type>`, `catalog:<kind>`, `regex:<name>`,
`copy:<name>`, `window`, `input:<name>`), so the page and the miner say the same thing about the same run.

No LLM, no I/O, one pass over the record.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any

from crystal.extract import ids as _ids

try:  # dataflow is the authority on the edge taxonomy; the page must still render if it is mid-edit
    from crystal.induce.dataflow import PROMPT, CallEdge
    from crystal.induce.dataflow import _value_type as _df_value_type
except Exception:  # pragma: no cover - defensive
    PROMPT = "prompt"

    @dataclass(frozen=True)
    class CallEdge:  # type: ignore[no-redef]
        source: str
        value_type: str
        target: str
        arg: str
        src_call: int | None
        tgt_call: int

    _df_value_type = None

INPUTS = "inputs"
_SUFFIX_RX = re.compile(r"_\d+$")
_PLURAL_KEEP = ("ts", "ss", "us")


def leaf_strings(v: Any) -> list[str]:
    """Every string (and stringified number) at the leaves of a JSON-ish value."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, bool) or v is None:
        return []
    if isinstance(v, (int, float)):
        return [str(v)]
    if isinstance(v, list):
        return [s for x in v for s in leaf_strings(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in leaf_strings(x)]
    return []


def singular(name: str) -> str:
    """`trace_ids` -> `trace_id`, `shas` -> `sha`, `dates` -> `date`; `thread_ts` and `status` left alone."""
    n = _SUFFIX_RX.sub("", str(name or ""))
    if len(n) > 4 and n.endswith("ies"):
        return n[:-3] + "y"
    if len(n) > 4 and n.endswith("es") and n[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return n[:-2]
    if len(n) > 3 and n.endswith("s") and not n.endswith(_PLURAL_KEEP):
        return n[:-1]
    return n


def display_type(value_type: str, name: str = "") -> str:
    """The short word for an edge label: the dataflow value type read back as the thing that moved."""
    vt = str(value_type or "")
    if vt == "window":
        return "window"
    head, _, rest = vt.partition(":")
    if head == "catalog-attr":
        return rest.split(".")[-1] or singular(name)
    if head in ("ids", "catalog") and rest:
        return rest
    if head in ("regex", "copy", "input") and rest:
        return singular(rest)
    return singular(name) or vt or "value"


# ---------------------------------------------------------------- the model

@dataclass(frozen=True)
class Origin:
    """Where a value first appeared: a run input, or an extractor on a step's result."""
    step: str                      # step id, or "inputs"
    title: str
    name: str                      # extractor name, or input name
    value_type: str
    index: int                     # position in the record's steps, -1 for inputs

    @property
    def is_input(self) -> bool:
        return self.step == INPUTS


@dataclass(frozen=True)
class Use:
    """Where the value went: a later call, and the argument it landed in."""
    step: str
    title: str
    arg: str
    index: int
    item: str | None = None


@dataclass
class ValueFlow:
    """One distinct value, with everything the page needs to say about it."""
    value: str
    origins: list[Origin] = field(default_factory=list)
    uses: list[Use] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return bool(self.uses)

    @property
    def kind(self) -> str:
        """The word for what this value IS: `trace_id`, `error_class`, `service`, `window`."""
        for o in self.origins:
            d = display_type(o.value_type, o.name)
            if d:
                return d
        return _ids.type_of(self.value) or "value"

    def title(self) -> str:
        """The hover text: what it is, who found it, who used it."""
        bits = [self.kind]
        ins = [o for o in self.origins if o.is_input]
        ex = [o for o in self.origins if not o.is_input]
        if ins:
            bits.append("run input " + ", ".join(dict.fromkeys(o.name for o in ins)))
        if ex:
            who = ", ".join(dict.fromkeys(f"“{o.title}”" for o in ex[:3]))
            names = ", ".join(dict.fromkeys(o.name for o in ex[:3]))
            bits.append(f"extracted by {who} as {names}" + (" and more" if len(ex) > 3 else ""))
        if self.uses:
            used = ", ".join(f"“{u.title}” as {u.arg}" for u in self.uses[:3])
            bits.append("used by " + used + (f" and {len(self.uses) - 3} more" if len(self.uses) > 3 else ""))
        else:
            bits.append("detected, never used in a later call")
        return ", ".join(bits)


@dataclass
class Provenance:
    values: "OrderedDict[str, ValueFlow]"
    call_edges: list[CallEdge]
    steps: list[dict]

    def get(self, value: str) -> ValueFlow | None:
        return self.values.get(value)

    @property
    def carried(self) -> list[ValueFlow]:
        return [v for v in self.values.values() if v.used]

    def edges(self) -> "OrderedDict[tuple[str, str], list[dict]]":
        """(source step, target step) -> the values that moved along it, richest first.

        One entry per (name, kind): the label the diagram draws, the concrete values behind it and the arguments
        they fed, so the edge can say `trace_id` and still name `1589cbb8…` on hover."""
        out: "OrderedDict[tuple[str, str], list[dict]]" = OrderedDict()
        for vf in self.values.values():
            for u in vf.uses:
                for o in vf.origins:
                    if o.index >= u.index:
                        continue
                    key = (o.step, u.step)
                    rows = out.setdefault(key, [])
                    label = display_type(o.value_type, o.name)
                    row = next((r for r in rows if r["label"] == label and r["name"] == o.name), None)
                    if row is None:
                        row = {"label": label, "name": o.name, "value_type": o.value_type, "values": [], "args": []}
                        rows.append(row)
                    if vf.value not in row["values"]:
                        row["values"].append(vf.value)
                    if u.arg not in row["args"]:
                        row["args"].append(u.arg)
                    break       # the nearest earlier origin owns the edge
        return out


# ---------------------------------------------------------------- building

def _spec_of(flow: dict | None, step_id: str, name: str) -> dict:
    for s in (flow or {}).get("steps") or []:
        if s.get("id") == step_id:
            return ((s.get("extract") or {}).get(name)) or {}
    return {}


def value_type_of(flow: dict | None, step_id: str, name: str, value: str) -> str:
    """The dataflow value type for an extract, from the flow YAML when there is one, from the value when there
    is not (a recorded agent trace has no extractors, only values)."""
    if step_id == INPUTS:
        t = (((flow or {}).get("inputs") or {}).get(name) or {}).get("type") or (_ids.type_of(value) if value else None)
        return f"ids:{t}" if t else f"input:{name}"
    spec = _spec_of(flow, step_id, name)
    if spec:
        if _df_value_type is not None:
            try:
                return _df_value_type(spec, name)
            except Exception:  # noqa: BLE001 - fall through to the local reading
                pass
        using = spec.get("using")
        if using == "regex":
            return "regex:" + singular(name)
        if using == "window":
            return "window"
        if using:
            return str(using)
        return "copy:" + singular(name)
    t = _ids.type_of(value) if isinstance(value, str) else None
    return f"ids:{t}" if t else "copy:" + singular(name)


def _markable(v: Any, skip: set[str], name: str) -> str | None:
    """The same filter the highlights have always used: no timestamps, no prose, no booleans."""
    if name in skip or not isinstance(v, str):
        return None
    s = v.strip()
    if not (3 <= len(s) <= 80) or s.lower() in ("true", "false", "none"):
        return None
    if _ids.type_of(s) in ("iso_ts", "date"):
        return None
    return s


def analyse(record: dict, flow: dict | None = None, skip: set[str] | None = None) -> Provenance:
    """Walk the run record once: collect what every step produced, then find those values in later calls' args."""
    skip = set(skip or ())
    steps = list(record.get("steps") or [])
    index = {s["id"]: i for i, s in enumerate(steps) if s.get("id")}
    title = {s["id"]: (s.get("title") or s["id"]) for s in steps if s.get("id")}
    values: "OrderedDict[str, ValueFlow]" = OrderedDict()

    def add_origin(value: str, o: Origin) -> None:
        vf = values.setdefault(value, ValueFlow(value=value))
        if o not in vf.origins:
            vf.origins.append(o)

    for k, v in (record.get("inputs") or {}).items():
        for s in leaf_strings(v):
            got = _markable(s, set(), k)
            if got:
                t = _ids.type_of(got)
                add_origin(got, Origin(INPUTS, "the run's inputs", k, f"ids:{t}" if t else f"input:{k}", -1))

    for i, s in enumerate(steps):
        sid = s.get("id") or f"s{i}"
        blocks = [s.get("extracts") or {}] + [it.get("extracts") or {} for it in s.get("items") or []]
        for blk in blocks:
            for name, raw in blk.items():
                for one in (raw if isinstance(raw, list) else [raw]):
                    got = _markable(one, skip, name)
                    if got:
                        add_origin(got, Origin(sid, title.get(sid, sid), name,
                                               value_type_of(flow, sid, name, got), i))
        # a recorded trace has no extractors: `carried` already says which earlier call the value came out of
        for v, src in (s.get("carried") or {}).items():
            got = _markable(v, skip, "")
            if not got:
                continue
            j = index.get(src, -1) if src != INPUTS else -1
            if j >= i:
                continue
            if src == INPUTS and any(o.is_input for o in values.get(got, ValueFlow(got)).origins):
                continue        # the run's own inputs already named it
            t = _ids.type_of(got)
            name = t or "value"
            add_origin(got, Origin(src if src in index or src == INPUTS else INPUTS,
                                   title.get(src, "the run's inputs" if src == INPUTS else src),
                                   name, f"ids:{t}" if t else "copy:value", j))

    _find_uses(values, steps, title)
    _name_by_use(values)
    return Provenance(values=values, call_edges=_call_edges(values, steps), steps=steps)


def _name_by_use(values: "OrderedDict[str, ValueFlow]") -> None:
    """A recorded trace has no extractor names: a value that is not a typed id arrives as an anonymous `value`.
    Name it after the argument it was carried into -- a path that ends up in `path` IS a path."""
    for vf in values.values():
        if not vf.uses:
            continue
        arg = vf.uses[0].arg
        vf.origins = [replace(o, name=arg, value_type="copy:" + singular(arg))
                      if o.name in ("value", "") else o for o in vf.origins]


def _find_uses(values: "OrderedDict[str, ValueFlow]", steps: list[dict], title: dict[str, str]) -> None:
    """A value is CARRIED when it turns up in the rendered arguments of a later call."""
    if not values:
        return
    ordered = sorted(values.values(), key=lambda vf: (-len(vf.value), vf.value))
    for i, s in enumerate(steps):
        sid = s.get("id") or f"s{i}"
        calls = [(None, s.get("args") or {})] + [(it.get("item"), it.get("args") or {}) for it in s.get("items") or []]
        leaves: list[tuple[str, str | None, str]] = []
        for item, args in calls:
            if not isinstance(args, dict):
                continue
            for arg, raw in args.items():
                for text in leaf_strings(raw):
                    leaves.append((arg, item if isinstance(item, str) else None, text))
        if not leaves:
            continue
        for vf in ordered:
            first = min((o.index for o in vf.origins), default=None)
            if first is None or first >= i:
                continue
            for arg, item, text in leaves:
                if _occurs(vf.value, text):
                    u = Use(sid, title.get(sid, sid), arg, i, item)
                    if not any(x.step == u.step and x.arg == u.arg for x in vf.uses):
                        vf.uses.append(u)
                    break


def _occurs(value: str, text: str) -> bool:
    """Short and numeric values must match the whole argument; anything longer may sit inside one
    (`trace_id:1589cbb8…`, a JQL clause, a Slack query)."""
    if value == text:
        return True
    if len(value) < 5 or value.isdigit():
        return False
    return value in text


def _call_edges(values: "OrderedDict[str, ValueFlow]", steps: list[dict]) -> list[CallEdge]:
    """The run as `dataflow.CallEdge`s: source/target are the tools, the call indices are the steps."""
    tool = {s.get("id"): (s.get("tool") or s.get("id") or "") for s in steps}
    out: list[CallEdge] = []
    seen = set()
    for vf in values.values():
        for u in vf.uses:
            for o in vf.origins:
                if o.index >= u.index:
                    continue
                ce = CallEdge(source=PROMPT if o.is_input else tool.get(o.step, o.step),
                              value_type=o.value_type, target=tool.get(u.step, u.step), arg=u.arg,
                              src_call=None if o.is_input else o.index, tgt_call=u.index)
                if ce not in seen:
                    seen.add(ce)
                    out.append(ce)
                break
    return out
