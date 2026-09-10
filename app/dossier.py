"""Build the dossier view model from a run record (or a recorded agent trace): headline, coverage, evidence grouped by
information type, highlighted extracted values, diff-style excerpts, the research diagram and the "how" panel.

No LLM: the headline is derived from extracts and typed results. `crystal.flow.cards` (coverage, headline) is used
when present; the fallbacks here compute the same shapes from the run record alone, so the page works either way.
"""
from __future__ import annotations

import html
import json
import re
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from typing import Any

from markupsafe import Markup

from app import diagram
from app.render import cards as render_cards
from crystal.extract.ids import ID_PATTERNS, typed_mentions
from crystal.flow.runner import count_hits

try:  # a parallel branch adds crystal/flow/cards.py; code against its interface, fall back when it is missing
    from crystal.flow.cards import coverage as _coverage, headline as _headline
except ImportError:  # pragma: no cover - exercised when cards.py is absent
    _coverage = _headline = None

# ---------------------------------------------------------------- information types

KIND_TYPE = {"issue": "issue", "message": "message", "page": "page", "log": "log", "series": "metric", "code": "code",
             "source": "code", "commit": "commit", "diff": "commit", "incident": "incident", "owners": "owners"}
SERVER_TYPE = {"jira": "issue", "slack": "message", "confluence": "page", "logz": "log", "chronosphere": "metric",
               "code": "code", "git": "commit", "pagerduty": "incident", "flows": "flow"}
GROUP_ORDER = ["issue", "incident", "message", "metric", "log", "commit", "code", "owners", "page", "flow", "other"]
GROUP_LABELS = {"issue": "Ticket", "incident": "Incident", "message": "Discussion", "metric": "Error rate",
                "log": "Log lines", "commit": "Changes just before", "code": "Code that raises the error",
                "owners": "Owners", "page": "Runbook and wiki", "flow": "Flow runs", "other": "Other results"}
WIDE_TYPES = {"message", "log", "commit", "page", "code"}

# extracts whose values are not worth marking inside content (prose, timestamps, dictionaries, people)
SKIP_EXTRACTS = {"lines", "remediation", "summary", "window", "commit_window", "created", "time", "status",
                 "people", "sender", "assignee", "reporter"}
_TS = re.compile(ID_PATTERNS["iso_ts"][0])
_DATE = re.compile(ID_PATTERNS["date"][0])
_TRACE = re.compile(ID_PATTERNS["trace_id"][0])
_POD = re.compile(ID_PATTERNS["k8s_pod"][0])
_KEY = re.compile(ID_PATTERNS["jira_key"][0])
_REPEAT = re.compile(r"(?:looks like|same as|repeat of|recurrence of|again[,:]?\s+like)\s+([A-Z][A-Z0-9]{1,9}-\d{1,6})|([A-Z][A-Z0-9]{1,9}-\d{1,6})\s+again\b")


def step_type(step: dict) -> str:
    tool = step.get("tool") or ""
    if tool == "code.codeowners":
        return "owners"
    server = tool.split(".", 1)[0]
    return SERVER_TYPE.get(server, "other")


# ---------------------------------------------------------------- highlights

def collect_highlights(record: dict) -> "OrderedDict[str, list[str]]":
    """value -> ['step.extract', ...]: every extracted (or carried) value worth marking inside the content."""
    out: "OrderedDict[str, list[str]]" = OrderedDict()

    def add(value: Any, label: str) -> None:
        if isinstance(value, list):
            for v in value:
                add(v, label)
            return
        if not isinstance(value, str):
            return
        v = value.strip()
        if not (3 <= len(v) <= 80) or _TS.fullmatch(v) or _DATE.fullmatch(v) or v.lower() in ("true", "false", "none"):
            return
        out.setdefault(v, [])
        if label not in out[v]:
            out[v].append(label)

    for k, v in (record.get("inputs") or {}).items():
        add(v, f"input {k}")
    for s in record.get("steps", []):
        for k, v in (s.get("extracts") or {}).items():
            if k not in SKIP_EXTRACTS:
                add(v, f"extracted by {s['id']} as {k}")
        for it in s.get("items") or []:
            for k, v in (it.get("extracts") or {}).items():
                if k not in SKIP_EXTRACTS:
                    add(v, f"extracted by {s['id']} as {k}")
        for v, src in (s.get("carried") or {}).items():
            add(v, f"carried {src} → {s['id']}")
    return out


class Marker:
    """Wrap occurrences of highlighted values in <mark title="..."> inside escaped text."""

    def __init__(self, highlights: dict[str, list[str]]):
        self.highlights = highlights
        vals = sorted(highlights, key=len, reverse=True)
        self.rx = re.compile("|".join(re.escape(v) for v in vals)) if vals else None

    def has_match(self, text: str) -> bool:
        return bool(self.rx and text and self.rx.search(text))

    def mark(self, text: Any) -> Markup:
        text = "" if text is None else str(text)
        if not self.rx:
            return Markup(html.escape(text))
        parts, pos = [], 0
        for m in self.rx.finditer(text):
            parts.append(html.escape(text[pos:m.start()]))
            who = "; ".join(self.highlights.get(m.group(0), []))
            parts.append(f'<mark title="{html.escape(who)}">{html.escape(m.group(0))}</mark>')
            pos = m.end()
        parts.append(html.escape(text[pos:]))
        return Markup("".join(parts))

    def excerpt(self, text: Any, context: int = 2, min_lines: int = 10, head: int = 4) -> Markup:
        """Diff-style excerpt: only the lines around a highlighted match, the rest folded into
        'show N more lines' expanders (details/summary, so it works with no JS)."""
        text = "" if text is None else str(text)
        lines = text.split("\n")
        if len(lines) <= min_lines:
            return Markup("".join(f'<div class="ln">{self.mark(l) or "&nbsp;"}</div>' for l in lines))
        keep = [False] * len(lines)
        hit_any = False
        for i, l in enumerate(lines):
            if self.has_match(l):
                hit_any = True
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    keep[j] = True
        if not hit_any:
            for j in range(min(head, len(lines))):
                keep[j] = True
        out, i = [], 0
        while i < len(lines):
            if keep[i]:
                out.append(f'<div class="ln">{self.mark(lines[i]) or "&nbsp;"}</div>')
                i += 1
                continue
            j = i
            while j < len(lines) and not keep[j]:
                j += 1
            hidden = "".join(f'<div class="ln">{self.mark(l) or "&nbsp;"}</div>' for l in lines[i:j])
            out.append(f'<details class="more"><summary>show {j - i} more lines</summary>{hidden}</details>')
            i = j
        return Markup("".join(out))


# ---------------------------------------------------------------- evidence groups

def _card_key(c: dict) -> tuple | None:
    k = c.get("kind")
    if k == "message":
        return (k, c.get("channel"), c.get("ts"))
    if k == "log":
        return (k, c.get("ts"), c.get("pod"), c.get("message"))
    if k == "commit":
        return (k, c.get("sha"))
    if k == "page":
        return (k, c.get("id"))
    if k == "issue":
        return (k, c.get("key"))
    if k == "incident":
        return (k, c.get("id"))
    if k == "owners":
        return (k, c.get("path"), tuple(c.get("owners") or []))
    if k == "code":
        return (k, c.get("file"), c.get("line"))
    return None


def _sources(record: dict) -> list[dict]:
    """One source per (step, item): the cards a call produced, tagged with the step and the fan-out item."""
    out = []
    for s in record.get("steps", []):
        if s.get("skipped"):
            continue
        typ = step_type(s)
        if "items" in s:
            for n, it in enumerate(s["items"]):
                cs = render_cards(s["tool"], it.get("result")) if it.get("result") is not None else []
                if it.get("error") and not cs:
                    cs = [{"kind": "error", "text": str(it["error"])[:400]}]
                out.append({"step": s["id"], "title": s.get("title") or s["id"], "item": it.get("item"), "n": n,
                            "hits": it.get("hits") or 0, "cards": cs, "error": it.get("error"), "type": typ})
        else:
            cs = render_cards(s["tool"], s.get("result")) if s.get("result") is not None else []
            if isinstance(s.get("result"), str) and s["result"].lstrip().startswith("error"):
                cs = [{"kind": "error", "text": s["result"][:400]}]
            if s.get("error") and not cs:
                cs = [{"kind": "error", "text": str(s["error"])[:400]}]
            out.append({"step": s["id"], "title": s.get("title") or s["id"], "item": None, "n": 0,
                        "hits": s.get("hits") or 0, "cards": cs, "error": s.get("error"), "type": typ})
    return out


def evidence_groups(record: dict) -> list[dict]:
    """Group cards by information TYPE across steps (all log lines from a fan-out land in one group, with a per-item
    summary), deduplicated: a message found by search and again as a thread reply appears once."""
    groups: dict[str, dict] = {}
    seen: set[tuple] = set()
    anchored: set[str] = set()   # one #s-<step> anchor per step across all groups (the diagram links to it)
    for src in _sources(record):
        for c in src["cards"]:
            typ = KIND_TYPE.get(c.get("kind"), src["type"] if c.get("kind") in ("json", "error", "empty") else "other")
            if c.get("kind") == "empty":
                continue
            key = _card_key(c)
            if key is not None:
                if key in seen:
                    if c.get("kind") == "page" and c.get("body"):
                        g = groups[typ]
                        for blk in g["sources"]:
                            blk["cards"] = [x for x in blk["cards"] if _card_key(x) != key]
                    else:
                        continue
                seen.add(key)
            g = groups.setdefault(typ, {"type": typ, "label": GROUP_LABELS.get(typ, typ), "sources": [], "steps": [],
                                        "wide": typ in WIDE_TYPES, "hits": 0})
            blk = next((b for b in g["sources"] if b["step"] == src["step"] and b["item"] == src["item"]), None)
            if blk is None:
                blk = {"step": src["step"], "title": src["title"], "item": src["item"], "hits": src["hits"], "cards": [],
                       "error": src["error"], "anchor": src["step"] not in anchored}
                anchored.add(src["step"])
                g["sources"].append(blk)
                if src["step"] not in g["steps"]:
                    g["steps"].append(src["step"])
            blk["cards"].append(c)
    out = []
    for typ in GROUP_ORDER:
        g = groups.get(typ)
        if not g:
            continue
        g["sources"] = [b for b in g["sources"] if b["cards"]]
        if not g["sources"]:
            continue
        g["hits"] = sum(len(b["cards"]) for b in g["sources"])
        if typ == "log":
            g["summary"] = _log_summary(g["sources"])
        if typ == "message":
            for b in g["sources"]:
                b["cards"].sort(key=lambda c: str(c.get("ts") or ""))
        out.append(g)
    return out


def _log_summary(sources: list[dict]) -> dict:
    rows, pods, levels = [], Counter(), Counter()
    for b in sources:
        logs = [c for c in b["cards"] if c.get("kind") == "log"]
        if not logs:
            continue
        p = Counter(c.get("pod") for c in logs if c.get("pod"))
        lv = Counter(c.get("level") for c in logs if c.get("level"))
        pods.update(p)
        levels.update(lv)
        times = sorted(c.get("ts") for c in logs if c.get("ts"))
        rows.append({"step": b["step"], "item": b["item"], "query": b["title"] if b["item"] is None else None,
                     "n": len(logs), "pods": [k for k, _ in p.most_common()], "levels": dict(lv.most_common()),
                     "first": times[0] if times else None, "last": times[-1] if times else None})
    return {"rows": rows, "pods": [k for k, _ in pods.most_common()], "levels": dict(levels.most_common()),
            "n": sum(r["n"] for r in rows)}


# ---------------------------------------------------------------- metric sparkline

def sparkline(card: dict, anchor_ts: str | None = None, w: int = 480, h: int = 84) -> Markup:
    """A small line chart of one series: 2px line over a light area, a baseline, the peak labelled, and a vertical
    rule at the incident's start when it falls inside the range. Stretches to the card width (non-scaling stroke)."""
    vals = card.get("values") or []
    if not vals:
        return Markup("")
    ts = [t for t, _ in vals]
    ys = [v for _, v in vals]
    pad_l, pad_r, pad_t, pad_b = 6, 6, 14, 16
    t0, t1 = ts[0], ts[-1]
    mx = max(ys) or 1.0
    span = max(1, t1 - t0)

    def X(t):
        return pad_l + (t - t0) / span * (w - pad_l - pad_r)

    def Y(v):
        return pad_t + (1 - v / mx) * (h - pad_t - pad_b)

    pts = [(X(t), Y(v)) for t, v in vals]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    base = h - pad_b
    area = f"M{pts[0][0]:.1f},{base} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" L{pts[-1][0]:.1f},{base} Z"
    peak_i = max(range(len(ys)), key=lambda i: ys[i])
    px, py = pts[peak_i]
    color = diagram.TYPE_COLORS["metric"]
    out = [f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="{len(ys)} points, peak {mx:.2f}">',
           f'<line x1="{pad_l}" y1="{base}" x2="{w - pad_r}" y2="{base}" stroke="#dde1de" stroke-width="1" vector-effect="non-scaling-stroke"/>',
           f'<path d="{area}" fill="{color}" opacity="0.12"/>',
           f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>']
    if anchor_ts:
        try:
            at = datetime.fromisoformat(str(anchor_ts).replace("Z", "+00:00")).timestamp()
            if t0 <= at <= t1:
                ax = X(at)
                out.append(f'<line x1="{ax:.1f}" y1="{pad_t - 4}" x2="{ax:.1f}" y2="{base}" stroke="#1b211e" stroke-width="1" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"><title>incident start</title></line>')
        except ValueError:
            pass
    out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{color}" stroke="#fff" stroke-width="1.5" vector-effect="non-scaling-stroke"><title>peak {mx:.2f}</title></circle>')
    lbl_t0, lbl_t1 = (datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M") for t in (t0, t1))
    out.append(f'<text x="{pad_l}" y="{h - 3}" font-size="10" fill="#5c6662" textLength="30" lengthAdjust="spacingAndGlyphs">{lbl_t0}</text>')
    out.append(f'<text x="{w - pad_r - 30}" y="{h - 3}" font-size="10" fill="#5c6662" textLength="30" lengthAdjust="spacingAndGlyphs">{lbl_t1}</text>')
    out.append("</svg>")
    return Markup("".join(out))


# ---------------------------------------------------------------- headline

def _merged_extracts(record: dict) -> dict:
    """First non-empty value per extract name across steps (lists are unioned, order kept)."""
    ex: dict[str, Any] = {}
    for s in record.get("steps", []):
        blocks = [s.get("extracts") or {}] + [it.get("extracts") or {} for it in s.get("items") or []]
        for blk in blocks:
            for k, v in blk.items():
                if v in (None, "", [], {}):
                    continue
                if isinstance(v, list):
                    cur = ex.setdefault(k, [])
                    if isinstance(cur, list):
                        cur.extend(x for x in v if x not in cur)
                elif k not in ex:
                    ex[k] = v
    return ex


def _all_cards(record: dict, kind: str) -> list[dict]:
    return [c for src in _sources(record) for c in src["cards"] if c.get("kind") == kind]


def _fmt_ts(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(ts)


def _duration(a: str | None, b: str | None) -> str | None:
    try:
        da, db = (datetime.fromisoformat(str(x).replace("Z", "+00:00")) for x in (a, b))
    except (ValueError, TypeError):
        return None
    m = int((db - da).total_seconds() // 60)
    if m < 0:
        return None
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def fallback_headline(flow: dict | None, record: dict) -> dict:
    ex = _merged_extracts(record)
    issues = _all_cards(record, "issue")
    incidents = _all_cards(record, "incident")
    commits = _all_cards(record, "commit")
    pages = _all_cards(record, "page")
    messages = _all_cards(record, "message")
    issue = issues[0] if issues else {}
    inc = incidents[0] if incidents else {}
    own_key = issue.get("key") or (record.get("inputs") or {}).get("key")
    texts = [issue.get("description") or ""] + [m.get("text") or "" for m in messages] + [i.get("details") or "" for i in incidents]

    what = ex.get("error_sig") or issue.get("summary") or ex.get("summary") or inc.get("title") or ex.get("error_class")
    service = ex.get("service") or (issue.get("components") or [None])[0] or ex.get("service_in_text")
    if not service and inc.get("title", "").startswith("["):
        service = inc["title"][1:].split("]", 1)[0]
    window = ex.get("window") if isinstance(ex.get("window"), dict) else {}
    started = inc.get("created") or ex.get("created") or ex.get("time") or window.get("anchor")
    resolved = inc.get("resolved")
    when = {"started": started, "started_fmt": _fmt_ts(started), "resolved": resolved, "resolved_fmt": _fmt_ts(resolved),
            "duration": _duration(started, resolved)} if started else None

    teams = list(dict.fromkeys(ex.get("teams") or []))
    people = list(dict.fromkeys([p for p in (ex.get("people") or []) if p] + [x for x in (issue.get("assignee"), inc.get("assignee"), ex.get("sender"), ex.get("assignee")) if x]))

    status = {}
    if issue.get("status"):
        status["ticket"] = issue["status"]
    if inc.get("status"):
        status["incident"] = inc["status"]

    changed = []
    for c in commits[:5]:
        changed.append({"sha": (c.get("sha") or "")[:10], "subject": c.get("subject"), "author": c.get("author"), "date": _fmt_ts(c.get("date"))})

    repeat = None
    for t in texts:
        for m in _REPEAT.finditer(t):
            k = m.group(1) or m.group(2)
            if k and k != own_key:
                repeat = k
                break
        if repeat:
            break
    related, seen_pages = [], set()
    for p in pages:
        if "postmortem" in (p.get("title") or "").lower() and p.get("id") not in seen_pages:
            seen_pages.add(p.get("id"))
            related.append({"kind": "postmortem", "title": p.get("title"), "id": p.get("id")})
    caused_by = None
    for s in record.get("steps", []):
        r = s.get("result")
        if isinstance(r, dict) and r.get("key") == own_key:
            for l in (r.get("fields") or {}).get("issuelinks") or []:
                if "caused by" in str(l.get("type", "")) and l.get("key"):
                    caused_by = l["key"]

    trace_ids = list(dict.fromkeys([v for v in (ex.get("trace_ids") or []) if isinstance(v, str)]))
    pods = list(dict.fromkeys([v for v in (ex.get("pods") or []) if isinstance(v, str)]))
    if not trace_ids or not pods:  # traces carry no extracts: mine the highlighted values by type
        for v in collect_highlights(record):
            if _TRACE.fullmatch(v) and v not in trace_ids:
                trace_ids.append(v)
            elif _POD.fullmatch(v) and v not in pods:
                pods.append(v)

    return {"what": what, "key": own_key, "when": when, "service": service, "team_owners": teams, "status": status,
            "changed_before": changed, "repeat_of": repeat, "caused_by": caused_by, "related": related, "people": people,
            "trace_ids": trace_ids, "pods": pods, "error_class": ex.get("error_class"),
            "remediation": (ex.get("remediation") or [None])[0] if isinstance(ex.get("remediation"), list) else ex.get("remediation")}


def _as_list(v: Any) -> list:
    if v in (None, "", [], {}):
        return []
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def headline_for(flow: dict | None, record: dict) -> dict:
    """crystal.flow.cards.headline when available (its keys merged over the fallback so nothing goes blank)."""
    base = fallback_headline(flow, record)
    if _headline and flow:
        try:
            h = _headline(flow, record) or {}
        except Exception:  # noqa: BLE001
            h = {}
        own_created = ((base.get("when") or {}).get("started")) or ""
        for k, v in h.items():
            if v in (None, "", [], {}):
                continue
            if k in ("team_owners", "people", "trace_ids", "pods"):
                base[k] = list(dict.fromkeys(_as_list(v) + _as_list(base.get(k))))
            elif k == "when":
                # cards.headline gives {anchor, start, end}; the fallback's {started, resolved, duration} is richer, so only fill a gap
                anchor = v.get("anchor") if isinstance(v, dict) else str(v)
                if not base.get("when") and anchor:
                    base["when"] = {"started": anchor, "started_fmt": _fmt_ts(anchor) or anchor, "resolved": None, "resolved_fmt": None, "duration": None}
            elif k == "status":
                st = dict(base.get("status") or {})
                if isinstance(v, dict):
                    if v.get("jira"):
                        st.setdefault("ticket", v["jira"])
                    if v.get("pagerduty"):
                        st.setdefault("incident", v["pagerduty"])
                else:
                    st.setdefault("incident", str(v))
                base["status"] = st
            elif k == "changed_before":
                # cards.headline gives {commits: [...], suspect_sha}; the lede wants a list of commit dicts
                commits = v.get("commits") if isinstance(v, dict) else v
                if isinstance(commits, list) and commits and isinstance(commits[0], dict) and not base.get("changed_before"):
                    base["changed_before"] = commits
                elif isinstance(commits, list) and commits and not isinstance(commits[0], dict) and not base.get("changed_before"):
                    base["changed_before"] = [{"sha": "", "subject": str(x), "author": None, "date": None} for x in commits]
                if isinstance(v, dict) and v.get("suspect_sha"):
                    base.setdefault("suspect_sha", v["suspect_sha"])
            elif k == "repeat_of":
                # cards.headline lists every same-error ticket (not date-restricted); a repeat is an EARLIER one
                if not base.get("repeat_of") and isinstance(v, list):
                    earlier = [r for r in v if isinstance(r, dict) and r.get("key") and (not own_created or str(r.get("created") or "") < own_created)]
                    if earlier:
                        base["repeat_of"] = sorted(earlier, key=lambda r: str(r.get("created") or ""))[0]["key"]
                elif not base.get("repeat_of") and isinstance(v, str):
                    base["repeat_of"] = v
            else:
                base[k] = v
    return base


def lede(h: dict) -> list[str]:
    """The opening sentences of the dossier, composed from the headline facts. Missing facts produce no sentence."""
    out = []
    if h.get("what"):
        out.append(f"{h['what']} on {h['service']}." if h.get("service") else f"{h['what']}.")
    elif h.get("service"):
        out.append(f"An incident on {h['service']}.")
    w = h.get("when") or {}
    if w.get("started_fmt"):
        s = f"Fired {w['started_fmt']}"
        if w.get("resolved_fmt"):
            s += f", resolved after {w['duration']}" if w.get("duration") else f", resolved {w['resolved_fmt']}"
        out.append(s + ".")
    st = h.get("status") or {}
    if st.get("incident") and not w.get("resolved_fmt"):
        out.append(f"The incident is {st['incident']}.")
    if h.get("team_owners"):
        s = f"Owned by {', '.join(h['team_owners'])}"
        if h.get("people"):
            s += f" ({h['people'][0]} on it)"
        out.append(s + ".")
    ch = h.get("changed_before") or []
    if ch:
        c = ch[0]
        n = f"{len(ch)} commits" if len(ch) > 1 else "One commit"
        out.append(f"{n} touched the service just before, latest {c['sha']} “{c['subject']}”." if c.get("sha") else f"{n} just before: {c['subject']}.")
    if h.get("repeat_of"):
        out.append(f"This is a repeat of {h['repeat_of']}.")
    if h.get("caused_by"):
        out.append(f"Caused by {h['caused_by']}.")
    return out


# ---------------------------------------------------------------- coverage

def fallback_coverage(flow: dict | None, record: dict) -> dict:
    steps = {s["id"]: s for s in record.get("steps", [])}
    spec = ((flow or {}).get("card") or {}).get("expected_outputs")
    expected = []
    if spec:
        for e in spec:
            ids = _as_list(e.get("steps"))
            hits = sum((steps.get(i) or {}).get("hits") or 0 for i in ids)
            expected.append({"name": e.get("name"), "steps": ids, "description": e.get("description", ""),
                             "required": bool(e.get("required")), "found": hits > 0, "hits": hits})
    else:
        flow_steps = {s["id"]: s for s in (flow or {}).get("steps", [])}
        for s in record.get("steps", []):
            if s.get("skipped"):
                continue
            hits = s.get("hits") or 0
            expected.append({"name": s.get("title") or s["id"], "steps": [s["id"]], "description": "",
                             "required": bool(flow_steps.get(s["id"], {}).get("required")), "found": hits > 0, "hits": hits})
    missing = [e["name"] for e in expected if not e["found"]]
    return {"expected": expected, "found": sum(1 for e in expected if e["found"]), "total": len(expected),
            "missing": missing, "required_missing": [e["name"] for e in expected if not e["found"] and e["required"]]}


def coverage_for(flow: dict | None, record: dict) -> dict:
    if _coverage and flow:
        try:
            c = _coverage(flow, record)
            if c and "expected" in c:
                c.setdefault("found", sum(1 for e in c["expected"] if e.get("found")))
                c.setdefault("total", len(c["expected"]))
                c.setdefault("missing", [e.get("name") for e in c["expected"] if not e.get("found")])
                c.setdefault("required_missing", [e.get("name") for e in c["expected"] if not e.get("found") and e.get("required")])
                return c
        except Exception:  # noqa: BLE001
            pass
    return fallback_coverage(flow, record)


# ---------------------------------------------------------------- diagram

def diagram_nodes(record: dict) -> list[dict]:
    nodes = [{"id": "inputs", "title": ", ".join(f"{k}={str(v)[:18]}" for k, v in (record.get("inputs") or {}).items()) or "inputs",
              "type": "input", "hits": None, "sub": "inputs", "href": "#facts"}]
    for s in record.get("steps", []):
        n = {"id": s["id"], "title": s.get("title") or s["id"], "type": step_type(s), "hits": s.get("hits"),
             "skipped": s.get("skipped"), "error": s.get("error")}
        if "items" in s:
            n["items"] = len(s["items"])
            n["item_hits"] = sum(1 for it in s["items"] if it.get("hits"))
        nodes.append(n)
    return nodes


def flow_diagram(flow: dict | None, record: dict) -> str:
    nodes = diagram_nodes(record)
    if flow and flow.get("steps"):
        edges = diagram.flow_edges(flow)
    else:  # no YAML (trace): edges from carried values
        edges = []
        for s in record.get("steps", []):
            for src in dict.fromkeys((s.get("carried") or {}).values()):
                edges.append((src, s["id"]))
    return diagram.svg(nodes, edges)


# ---------------------------------------------------------------- traces

def _leaf_strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [s for x in v for s in _leaf_strings(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _leaf_strings(x)]
    return []


def _humanize(tool: str) -> str:
    server, _, name = tool.partition(".")
    return f"{server}: {name.replace('_', ' ')}"


def session_record(session) -> dict:
    """A recorded agent session in the run-record shape: steps from calls, extracts empty, `carried` = the values in
    a call's input that appeared in an earlier call's output (or in the session's inputs). Those drive the
    highlights and the diagram edges: the 'what the agent did' picture next to the crystallized one."""
    meta = session.meta or {}
    inputs = meta.get("inputs") or {}
    input_vals = [str(v) for v in _leaf_strings(inputs)]
    steps, outputs = [], []
    for i, c in enumerate(session.calls):
        sid = f"c{c.get('seq') or i + 1}"
        tool = f"{c.get('server')}.{c.get('tool')}"
        args = c.get("input") or {}
        result = c.get("output")
        cands: list[str] = []
        for s in _leaf_strings(args):
            cands.append(s)
            cands.extend(m["value"] for m in typed_mentions(s))
        carried: dict[str, str] = {}
        for v in dict.fromkeys(cands):
            v = v.strip()
            if len(v) < 5 or v.isdigit() or _DATE.fullmatch(v) or _TS.fullmatch(v):
                continue
            if any(v in iv for iv in input_vals):
                carried[v] = "inputs"
                continue
            for j in range(len(outputs) - 1, -1, -1):
                if v in outputs[j]:
                    carried[v] = steps[j]["id"]
                    break
        err = c.get("is_error")
        entry = {"id": sid, "title": _humanize(tool), "tool": tool, "args": args, "result": result,
                 "error": (str(result)[:300] if err else None), "hits": 0 if err else count_hits(result, None),
                 "duration_ms": c.get("duration_ms"), "attempts": [], "extracts": {}, "carried": carried, "ts": c.get("ts")}
        steps.append(entry)
        outputs.append(json.dumps(result, default=str) if not isinstance(result, str) else result)
    return {"run_id": session.session_id, "flow": f"trace:{meta.get('trigger', session.source)}", "trigger": meta.get("trigger"),
            "source": session.source, "inputs": inputs, "started": (session.calls[0].get("ts") if session.calls else meta.get("ts")) or "",
            "steps": steps, "status": "ok", "notes": [n.get("text") for n in session.notes], "prompt": meta.get("prompt"),
            "summary": {s["id"]: {"hits": s["hits"], "error": s["error"], "skipped": None} for s in steps}}


# ---------------------------------------------------------------- the view model

def build(record: dict, flow: dict | None) -> dict:
    highlights = collect_highlights(record)
    marker = Marker(highlights)
    groups = evidence_groups(record)
    h = headline_for(flow, record)
    calls = sum(1 + len(s.get("attempts") or []) if "items" not in s else len(s["items"]) for s in record.get("steps", []) if not s.get("skipped"))
    return {"headline": h, "lede": lede(h), "coverage": coverage_for(flow, record), "groups": groups, "marker": marker,
            "highlights": highlights, "svg": Markup(flow_diagram(flow, record)),
            "types": [(t, diagram.TYPE_COLORS[t], diagram.TYPE_LABELS[t]) for t in GROUP_ORDER if any(g["type"] == t for g in groups)],
            "calls": calls, "card": (flow or {}).get("card") or {}}
