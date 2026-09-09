"""Extractor specs used by flows:  {from: <jsonpath into step result>, using: regex|jsonpath|catalog:<kind>|window|ids:<type>|position|literal, ...}

  window:   {from: <timestamp>, before: 1h, after: 4h, round: 1h}   round (optional) floors the anchor to the hour/day
  position: {from: <text>, program: {start: {...}, end: {...}}}      learned position program (crystal.extract.positions)
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from jsonpath_ng.ext import parse as jp_parse

from crystal.extract import ids, positions
from crystal.extract.catalog import gazetteer_for

_DUR = re.compile(r"^(\d+)([smhd])$")


def parse_duration(s: str) -> timedelta:
    m = _DUR.match(str(s).strip())
    if not m:
        raise ValueError(f"bad duration {s!r}")
    n, u = int(m.group(1)), m.group(2)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[u]: n})


def select(obj: Any, path: str | None) -> list[Any]:
    """jsonpath select; '' or '$' returns [obj]. Bare dotted paths are accepted."""
    if not path or path == "$":
        return [obj]
    expr = path if path.startswith("$") else "$." + path
    return [m.value for m in jp_parse(expr).find(obj)]


def _as_texts(values: list[Any]) -> list[str]:
    out = []
    for v in values:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, (dict, list)):
            import json
            out.append(json.dumps(v))
        elif v is not None:
            out.append(str(v))
    return out


def run_extractor(spec: dict, result: Any, catalog: dict | None = None) -> Any:
    """Apply one extractor spec to a step result. Returns a scalar (or dict for window) or a list when `all: true`."""
    using = spec.get("using", "jsonpath")
    values = select(result, spec.get("from"))
    want_all = bool(spec.get("all"))

    if using == "jsonpath":
        return values if want_all else (values[0] if values else spec.get("default"))

    if using == "literal":
        return spec.get("value")

    if using == "regex":
        rx = re.compile(spec["pattern"], re.M | (re.I if spec.get("ignore_case") else 0))
        group = spec.get("group", 1 if rx.groups else 0)
        found, seen = [], set()
        for t in _as_texts(values):
            for m in rx.finditer(t):
                v = m.group(group) if not isinstance(group, str) else m.group(group)
                if v not in seen:
                    seen.add(v)
                    found.append(v)
                if not want_all and found:
                    return found[0]
        return found if want_all else spec.get("default")

    if using.startswith("ids:"):
        kind = using.split(":", 1)[1]
        found, seen = [], set()
        for t in _as_texts(values):
            for v in ids.find_all(t, kind):
                if v not in seen:
                    seen.add(v)
                    found.append(v)
        return found if want_all else (found[0] if found else spec.get("default"))

    if using.startswith("catalog:"):
        kind = using.split(":", 1)[1]
        gz = gazetteer_for(kind, catalog)
        found, seen = [], set()
        for t in _as_texts(values):
            for hit in gz.find(t):
                if hit["name"] not in seen:
                    seen.add(hit["name"])
                    found.append(hit["name"])
        return found if want_all else (found[0] if found else spec.get("default"))

    if using == "window":
        ts = values[0] if values else None
        if not ts:
            return spec.get("default")
        t = _parse_ts(ts)
        if spec.get("round"):
            t = round_down(t, spec["round"])
        start = t - parse_duration(spec.get("before", "1h"))
        end = t + parse_duration(spec.get("after", "4h"))
        return {"start": _iso(start), "end": _iso(end), "anchor": _iso(t),
                "start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d")}

    if using == "position":
        prog = spec["program"]
        found, seen = [], set()
        for t in _as_texts(values):
            for v in (positions.apply_all(prog, t) if want_all else [positions.apply(prog, t)]):
                if v is not None and v not in seen:
                    seen.add(v)
                    found.append(v)
            if not want_all and found:
                return found[0]
        return found if want_all else spec.get("default")

    raise ValueError(f"unknown extractor {using!r}")


def round_down(t: datetime, unit: str) -> datetime:
    """Floor a timestamp to a whole number of `unit` (e.g. 1h, 1d, 15m) since midnight UTC."""
    step = parse_duration(unit).total_seconds()
    day = t.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    secs = (t - day).total_seconds()
    return day + timedelta(seconds=secs - secs % step)


def _parse_ts(s: Any) -> datetime:
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    s = str(s)
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
