"""Turn raw tool results into evidence cards for the run view. One renderer per tool family; JSON fallback."""
from __future__ import annotations

import json
import re
from typing import Any


def _short(s: Any, n: int = 300) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def cards(tool: str, result: Any) -> list[dict]:
    if result is None:
        return []
    if isinstance(result, dict) and set(result) == {"error"}:
        return [{"kind": "error", "text": result["error"]}]
    name = tool.split(".", 1)[1] if "." in tool else tool
    fn = RENDERERS.get(name)
    try:
        if fn:
            out = fn(result)
            if out:
                return out
    except Exception:  # noqa: BLE001
        pass
    return [{"kind": "json", "text": json.dumps(result, indent=1, default=str)[:6000]}]


def jira_issue(r):
    f = r.get("fields", {})
    return [{"kind": "issue", "key": r.get("key"), "summary": f.get("summary"), "status": (f.get("status") or {}).get("name"),
             "priority": (f.get("priority") or {}).get("name"), "assignee": (f.get("assignee") or {}).get("displayName"),
             "created": f.get("created"), "components": [c.get("name") for c in f.get("components", [])],
             "description": f.get("description", "")}]


def jira_search(r):
    return [c for i in r.get("issues", []) for c in jira_issue(i)]


def slack_messages(r):
    msgs = r.get("messages")
    if isinstance(msgs, dict):
        msgs = msgs.get("matches", [])
    return [{"kind": "message", "user": m.get("username") or m.get("user"), "channel": (m.get("channel") or {}).get("name"),
             "ts": m.get("ts"), "time": m.get("time"), "text": m.get("text"), "permalink": m.get("permalink"), "replies": m.get("reply_count")}
            for m in msgs or []]


def confluence_search(r):
    return [{"kind": "page", "id": p.get("id"), "title": p.get("title"), "space": (p.get("space") or {}).get("key"),
             "excerpt": p.get("excerpt"), "url": p.get("url"), "modified": p.get("last_modified")} for p in r.get("results", [])]


def confluence_page(r):
    return [{"kind": "page", "id": r.get("id"), "title": r.get("title"), "space": (r.get("space") or {}).get("key"),
             "body": r.get("body", ""), "url": r.get("url")}]


def logs(r):
    out = []
    for h in r.get("hits", []):
        s = h.get("_source", h)
        out.append({"kind": "log", "ts": s.get("@timestamp"), "level": s.get("level"), "service": s.get("service"),
                    "pod": s.get("kubernetes.pod_name"), "trace_id": s.get("trace_id"), "message": s.get("message")})
    return out or [{"kind": "empty", "text": "no log lines"}]


def prom_range(r):
    out = []
    for series in (r.get("data") or {}).get("result", []):
        vals = [(int(t), float(v)) for t, v in series.get("values", [])]
        if not vals:
            continue
        ys = [v for _, v in vals]
        w, h, pad = 480, 60, 4
        mx = max(ys) or 1.0
        pts = " ".join(f"{pad + i * (w - 2 * pad) / max(1, len(ys) - 1):.1f},{h - pad - (y / mx) * (h - 2 * pad):.1f}" for i, y in enumerate(ys))
        out.append({"kind": "series", "labels": series.get("metric", {}), "points": pts, "w": w, "h": h,
                    "min": min(ys), "max": max(ys), "n": len(ys), "values": vals})
    return out


def grep(r):
    return [{"kind": "code", "file": m.get("file"), "line": m.get("line"), "text": m.get("text"), "context": m.get("context")}
            for m in r.get("matches", [])] or [{"kind": "empty", "text": "no matches"}]


def git_log(r):
    return [{"kind": "commit", "sha": c.get("sha"), "author": c.get("author"), "date": c.get("date"), "subject": c.get("subject")}
            for c in r.get("commits", [])] or [{"kind": "empty", "text": "no commits in window"}]


def git_show(r):
    return [{"kind": "diff", "text": str(r)[:8000]}]


def pagerduty(r):
    return [{"kind": "incident", "id": i.get("id"), "number": i.get("incident_number"), "title": i.get("title"), "status": i.get("status"),
             "urgency": i.get("urgency"), "created": i.get("created_at"), "resolved": i.get("resolved_at"),
             "assignee": ((i.get("assignments") or [{}])[0].get("assignee") or {}).get("summary"), "url": i.get("html_url"),
             "details": (i.get("body") or {}).get("details")} for i in r.get("incidents", [r] if "incident_number" in r else [])]


def codeowners(r):
    return [{"kind": "owners", "path": r.get("path"), "owners": r.get("owners", [])}]


def read_file(r):
    return [{"kind": "source", "path": r.get("path"), "start": r.get("start"), "content": r.get("content", "")}]


RENDERERS = {
    "jira_get_issue": jira_issue, "jira_search": jira_search,
    "conversations_search_messages": slack_messages, "conversations_replies": slack_messages, "conversations_history": slack_messages,
    "confluence_search": confluence_search, "confluence_get_page": confluence_page,
    "search_logs": logs, "search_logs_simple": logs,
    "query_prometheus_range": prom_range,
    "grep": grep, "git_grep": grep, "git_log": git_log, "git_show": git_show,
    "list_incidents": pagerduty, "get_incident": pagerduty,
    "codeowners": codeowners, "read_file": read_file,
}
