"""Shared helpers for the simulated MCP servers: world loading and small query-language subsets."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORLD_PATH = Path(__file__).resolve().parent.parent / "data" / "world.json"
_world: dict | None = None


def world() -> dict:
    global _world
    if _world is None:
        _world = json.loads(WORLD_PATH.read_text())
    return _world


def text(obj) -> str:
    """Real vendor servers return JSON inside a text block; we do the same."""
    return json.dumps(obj, indent=2, default=str)


def parse_time(s: str | None, default: datetime | None = None) -> datetime | None:
    if not s:
        return default
    s = s.strip()
    m = re.fullmatch(r"now(?:-(\d+)([smhd]))?", s)
    if m:
        base = latest_time()
        if m.group(1):
            n, u = int(m.group(1)), m.group(2)
            base -= timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[u]: n})
        return base
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.fromisoformat(s + "T00:00:00+00:00")
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def latest_time() -> datetime:
    """'now' for the simulation is a fixed point after the last incident so results are stable."""
    last = max(datetime.fromisoformat(i["started_at"].replace("Z", "+00:00")) for i in world()["incidents"])
    return last + timedelta(days=2)


# ---------------------------------------------------------------- query languages (subsets)
_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def tokenize(q: str) -> list[tuple[str, bool]]:
    """Return (token, is_phrase) pairs; quoted strings stay whole."""
    return [(a, True) if a else (b, False) for a, b in _TOKEN.findall(q) if a or b]


def match_terms(hay: str, terms: list[tuple[str, bool]], mode: str = "and") -> bool:
    """Case-insensitive term matching. A phrase must appear verbatim; a bare token is a word
    (trailing '*' = prefix). mode 'and' requires all, 'or' requires any."""
    h = hay.lower()
    hits = []
    for t, phrase in terms:
        t = t.lower()
        if phrase:
            hits.append(t in h)
        elif t.endswith("*"):
            hits.append(re.search(r"(?<![\w-])" + re.escape(t[:-1]), h) is not None)
        else:
            hits.append(re.search(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", h) is not None or t in h.split())
    return all(hits) if mode == "and" else any(hits)


def lucene_match(doc: dict, query: str) -> bool:
    """Subset of Elasticsearch query_string: `field:value`, `field:"phrase"`, quoted phrases,
    bare terms (AND by default), explicit AND/OR/NOT, parentheses are stripped, trailing '*'."""
    q = query.replace("(", " ").replace(")", " ")
    parts = re.findall(r'(?:NOT\s+)?(?:[\w.@-]+:)?(?:"[^"]*"|\S+)', q)
    ops = []
    clauses = []
    for p in parts:
        if p in ("AND", "OR"):
            ops.append(p)
            continue
        neg = p.startswith("NOT ")
        p = p[4:] if neg else p
        field, _, val = p.partition(":") if re.match(r"[\w.@-]+:", p) else ("", "", p)
        val = val.strip('"') if val.startswith('"') else val
        phrase = p.endswith('"') or (":" in p and val.startswith('"'))
        if field:
            fv = str(_get(doc, field, ""))
            ok = match_terms(fv, [(val, True)]) if not val.endswith("*") else fv.lower().startswith(val[:-1].lower()) or match_terms(fv, [(val, False)])
        else:
            ok = match_terms(flatten(doc), [(val, phrase or '"' in p)])
        clauses.append(not ok if neg else ok)
    if not clauses:
        return True
    if "OR" in ops and "AND" not in ops:
        return any(clauses)
    return all(clauses)


def _get(doc: dict, path: str, default=None):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, dict) and path in cur:
            return cur[path]
        else:
            return default
    return cur


def flatten(doc) -> str:
    if isinstance(doc, dict):
        return " ".join(flatten(v) for v in doc.values())
    if isinstance(doc, list):
        return " ".join(flatten(v) for v in doc)
    return str(doc)


def jql_match(issue: dict, jql: str) -> bool:
    """Subset of JQL: `key = X`, `project = X`, `component = X`, `status = X`, `text ~ "..."`,
    `summary ~ "..."`, `labels = X`, `updated >= -14d` (honoured), AND only, ORDER BY ignored."""
    jql = re.split(r"\border\s+by\b", jql, flags=re.I)[0]
    for clause in re.split(r"\bAND\b", jql, flags=re.I):
        clause = clause.strip()
        if not clause:
            continue
        m = re.match(r'(\w+)\s*(=|!=|~|>=|<=|in)\s*(.+)', clause, re.I)
        if not m:
            return False
        field, op, val = m.group(1).lower(), m.group(2), m.group(3).strip()
        val = val.strip('"\'')
        if op == "in":
            vals = [v.strip().strip('"\'') for v in val.strip("()").split(",")]
        if field == "text":
            terms = tokenize(val)
            hay = issue["summary"] + " " + issue["description"] + " " + " ".join(issue["components"])
            # JQL text ~ ORs bare terms; phrases must match verbatim; AND inside string honoured
            mode = "and" if re.search(r"\bAND\b", val) else "or"
            terms = [(t, p) for t, p in terms if t not in ("AND", "OR")]
            if not match_terms(hay, terms, mode):
                return False
        elif field == "summary":
            if not match_terms(issue["summary"], tokenize(val), "or"):
                return False
        elif field in ("updated", "created"):
            m2 = re.fullmatch(r"-(\d+)([dhw])", val)
            if m2:
                n = int(m2.group(1)) * {"d": 1, "h": 1 / 24, "w": 7}[m2.group(2)]
                cutoff = latest_time() - timedelta(days=n)
                when = datetime.fromisoformat(issue[field].replace("Z", "+00:00"))
                if op == ">=" and when < cutoff:
                    return False
                if op == "<=" and when > cutoff:
                    return False
        else:
            actual = issue.get(field) if field != "component" else issue["components"]
            actual = actual if isinstance(actual, list) else [actual]
            actual = [str(a).lower() for a in actual]
            if op == "in":
                if not any(v.lower() in actual for v in vals):
                    return False
            elif op == "=":
                if val.lower() not in actual:
                    return False
            elif op == "!=":
                if val.lower() in actual:
                    return False
    return True


def cql_match(page: dict, cql: str) -> bool:
    """Subset of CQL: `text ~ "..."`, `title ~ "..."`, `siteSearch ~ "..."`, `space = X`,
    `space in (...)`, `label = X`, `type = page`, `lastModified >= now("-30d")`, AND/OR at top level."""
    cql = re.split(r"\border\s+by\b", cql, flags=re.I)[0]
    top_or = re.search(r"\bOR\b", cql) and not re.search(r"\bAND\b", cql)
    results = []
    for clause in re.split(r"\b(?:AND|OR)\b", cql):
        clause = clause.strip().strip("()").strip()
        if not clause:
            continue
        m = re.match(r'([\w.]+)\s*(=|!=|~|>=|<=|in)\s*(.+)', clause, re.I)
        if not m:
            results.append(False)
            continue
        field, op, val = m.group(1).lower(), m.group(2), m.group(3).strip().strip('"\'')
        if field in ("text", "sitesearch"):
            hay = page["title"] + " " + page["body"]
            terms = [(t.rstrip("~"), p) for t, p in tokenize(val)]
            results.append(match_terms(hay, terms, "or"))
        elif field == "title":
            results.append(match_terms(page["title"], tokenize(val), "or"))
        elif field == "space":
            vals = [v.strip().strip('"\'') for v in val.strip("()").split(",")] if op == "in" else [val]
            results.append(page["space"].lower() in [v.lower() for v in vals])
        elif field == "label":
            results.append(val.lower() in [l.lower() for l in page["labels"]])
        elif field == "type":
            results.append(val.lower() in ("page", "blogpost"))
        elif field == "lastmodified":
            results.append(True)
        else:
            results.append(False)
    if not results:
        return True
    return any(results) if top_or else all(results)


def slack_query(q: str) -> tuple[list[tuple[str, bool]], dict]:
    """Split Slack modifiers (in:, from:, after:, before:, on:, has:) from search terms."""
    mods, terms = {}, []
    for tok, phrase in tokenize(q):
        m = re.fullmatch(r"(in|from|after|before|on|during|has|is):(.+)", tok) if not phrase else None
        if m:
            mods[m.group(1)] = m.group(2)
        else:
            terms.append((tok, phrase))
    return terms, mods
