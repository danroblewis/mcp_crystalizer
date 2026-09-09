"""Typed regex catalog for ID-shaped values. Each entry: name -> (regex, description).
Order matters for typing a raw string: earlier, more specific shapes win."""
from __future__ import annotations

import re

ID_PATTERNS: dict[str, tuple[str, str]] = {
    "jira_key":     (r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b", "Jira issue key, e.g. PAY-101"),
    "sha40":        (r"\b[0-9a-f]{40}\b", "full git commit SHA"),
    "trace_id":     (r"\b[0-9a-f]{32}\b", "32-hex trace id"),
    "uuid":         (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "UUID"),
    "trace_id16":   (r"\b[0-9a-f]{16}\b", "16-hex span/trace id"),
    "sha_short":    (r"\b[0-9a-f]{7,12}\b", "abbreviated git SHA"),
    "k8s_pod":      (r"\b[a-z0-9]+(?:-[a-z0-9]+)*-[a-z0-9]{8,10}-[a-z0-9]{5}\b", "kubernetes pod name (deploy-rs-pod)"),
    "iso_ts":       (r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b", "ISO-8601 timestamp"),
    "date":         (r"\b\d{4}-\d{2}-\d{2}\b", "calendar date"),
    "slack_ts":     (r"\b1[6-9]\d{8}\.\d{6}\b", "Slack message ts"),
    "slack_channel_id": (r"\bC[0-9A-F]{8,10}\b", "Slack channel id"),
    "slack_user_id":    (r"\bU[0-9A-F]{8,10}\b", "Slack user id"),
    "pd_incident_id":   (r"\bQ[0-9A-Z]{13}\b", "PagerDuty incident id"),
    "pd_service_id":    (r"\bP[0-9A-Z]{6}\b", "PagerDuty service id"),
    "http_status":  (r"\b[1-5]\d{2}\b", "HTTP status code"),
    "ipv4":         (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IPv4 address"),
    "email":        (r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "email"),
    "url":          (r"https?://[^\s<>\"')]+", "URL"),
    "slack_channel":(r"(?<![\w/])#[a-z0-9][a-z0-9_-]{1,40}\b", "Slack channel name"),
    "error_class":  (r"\b[A-Z][A-Za-z0-9]+(?:Error|Exception|Timeout|Mismatch|Drift|Expired|NotFound|Limited|Snapshot)\b", "exception-like class name"),
}
_COMPILED = {k: re.compile(v[0]) for k, v in ID_PATTERNS.items()}


def find_all(text: str, kind: str) -> list[str]:
    """All matches of one typed pattern, in order, deduplicated."""
    seen, out = set(), []
    for m in _COMPILED[kind].finditer(text or ""):
        v = m.group(0)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def type_of(value: str) -> str | None:
    """Best-guess type of a whole string, or None."""
    for k, rx in _COMPILED.items():
        if rx.fullmatch(value or ""):
            return k
    return None


def typed_mentions(text: str) -> list[dict]:
    """Every typed ID-shaped span in a text: [{type, value, start, end}], longest/most specific first per span."""
    spans = []
    for k, rx in _COMPILED.items():
        for m in rx.finditer(text or ""):
            spans.append({"type": k, "value": m.group(0), "start": m.start(), "end": m.end()})
    # drop spans fully contained in an earlier-typed span
    spans.sort(key=lambda s: (s["start"], -(s["end"] - s["start"])))
    out = []
    for s in spans:
        if any(o["start"] <= s["start"] and s["end"] <= o["end"] and o is not s for o in out):
            continue
        out.append(s)
    return out
