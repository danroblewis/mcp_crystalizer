"""A scripted stand-in for the AI agent, used to produce realistic traces without spending tokens.

It investigates a Jira ticket the way the agent does: read the ticket, pick strings out of it, chain tools,
carry values forward. `variant` changes the order and search phrasing a little, as real agent runs do, so the
inducer has to generalise across sessions. Records to traces/<session>.jsonl with source=scripted.

  uv run python -m crystal.trace.scripted PAY-101 STF-109 SUP-102 PLAT-107 --variants 3
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone

from crystal.extract import ids
from crystal.extract.catalog import gazetteer_for
from crystal.mcp_client import ServerPool
from crystal.trace.record import Recorder


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def investigate(pool: ServerPool, key: str, variant: int = 0) -> Recorder:
    rec = Recorder(f"scripted-{key}-v{variant}", "scripted", meta={"trigger": "jira_issue", "inputs": {"key": key}, "variant": variant})

    async def call(server, tool, args, why=""):
        if why:
            rec.note(why)
        out, raw, err = await pool.call_raw(server, tool, args)
        rec.record(server, tool, args, out, raw, is_error=err)
        return out

    issue = await call("jira", "jira_get_issue", {"issue_key": key}, "read the ticket")
    desc = issue["fields"]["description"]
    created = datetime.fromisoformat(issue["fields"]["created"].replace("Z", "+00:00"))
    m = re.search(r"^Error:\s*(.+)$", desc, re.M)
    if not m:
        rec.note("ticket has no error line; not an incident, stopping")
        return rec
    error_sig = m.group(1)
    error_class = error_sig.split(":")[0]
    gz = gazetteer_for("service")
    hits = gz.find(" ".join(c["name"] for c in issue["fields"]["components"]) + " " + desc)
    service = hits[0]["name"]
    svc = gz.entities[service]
    trace_ids = ids.find_all(desc, "trace_id")
    w_start, w_end = created - timedelta(hours=1), created + timedelta(hours=6)
    day0, day1 = w_start.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")

    # --- slack: phrasing varies by variant, like an agent would
    if variant % 3 == 1:
        r = await call("slack", "conversations_search_messages", {"search_query": key, "limit": 20}, "search slack for the ticket key")
        if not r["messages"]["matches"]:
            r = await call("slack", "conversations_search_messages", {"search_query": f'"{error_sig}" after:{day0} before:{day1}', "limit": 20}, "no hits; search the error text in the window")
    elif variant % 3 == 2:
        r = await call("slack", "conversations_search_messages", {"search_query": f'{error_class} in:#{svc["incident_channel"]} after:{day0} before:{day1}', "limit": 20}, "search the error class in the incident channel")
    else:
        r = await call("slack", "conversations_search_messages", {"search_query": f'"{error_sig}" in:#{svc["incident_channel"]} after:{day0} before:{day1}', "limit": 20}, "search the exact error text in the incident channel")
    matches = r["messages"]["matches"]
    threads = []
    for m in matches:
        if m["thread_ts"] not in threads:
            threads.append(m["thread_ts"])
    channel_id = matches[0]["channel"]["id"] if matches else None
    thread_text, shas = [], []
    for t in threads[:2]:
        rr = await call("slack", "conversations_replies", {"channel_id": channel_id, "thread_ts": t, "limit": 50}, "read the thread")
        for msg in rr["messages"]:
            thread_text.append(msg["text"])
            trace_ids += [x for x in ids.find_all(msg["text"], "trace_id") if x not in trace_ids]
            shas += [m["value"] for m in ids.typed_mentions(msg["text"]) if m["type"] == "sha_short" and m["value"] not in shas]

    # --- codebase
    g = await call("code", "grep", {"pattern": error_class, "glob": "**/*.py", "context": 2}, "find where the error is raised/logged")
    files = []
    for m in g["matches"]:
        if m["file"] not in files:
            files.append(m["file"])
    if files:
        await call("code", "codeowners", {"path": files[0]}, "who owns that file")
        if variant % 2 == 0:
            await call("code", "read_file", {"path": files[0]}, "read the handler")

    # --- order of the remaining checks varies
    async def confluence():
        c = await call("confluence", "confluence_search", {"query": f'text ~ "{error_class}" AND space = {svc["confluence_space"]}', "limit": 5}, "look for a runbook")
        if c["results"]:
            await call("confluence", "confluence_get_page", {"page_id": c["results"][0]["id"]}, "read the runbook")

    async def metrics():
        await call("chronosphere", "query_prometheus_range",
                   {"query": f'rate(http_requests_total{{service="{service}",code=~"5.."}}[5m])', "start": _iso(w_start), "end": _iso(w_end), "step_seconds": 300}, "error rate around the incident")

    async def logs():
        for tid in trace_ids[:4]:
            await call("logz", "search_logs", {"query": f"trace_id:{tid}", "from_time": _iso(w_start), "to_time": _iso(w_end), "size": 20}, "logs for a trace id from the thread/ticket")
        await call("logz", "search_logs", {"query": f'"{error_sig}" AND service:{service}', "from_time": _iso(w_start), "to_time": _iso(w_end), "size": 50}, "all lines with the error")

    async def commits():
        await call("git", "git_log", {"path": svc["repo_path"], "since": _iso(created - timedelta(days=4)), "until": _iso(created + timedelta(days=1)), "max_count": 10}, "recent commits to the service")
        for s in shas[:1]:
            await call("git", "git_show", {"sha": s}, "the commit named in slack")

    async def pagerduty():
        await call("pagerduty", "list_incidents", {"service_ids": svc["pagerduty_service_id"], "since": _iso(w_start), "until": _iso(w_end), "limit": 5}, "the page for this incident")

    order = [confluence, metrics, logs, commits, pagerduty] if variant % 2 == 0 else [metrics, logs, confluence, pagerduty, commits]
    for step in order:
        await step()
    rec.note("done", summary={"service": service, "error_sig": error_sig, "trace_ids": trace_ids, "files": files, "shas": shas})
    return rec


async def main(keys: list[str], variants: int) -> None:
    async with ServerPool() as pool:
        for key in keys:
            for v in range(variants):
                rec = await investigate(pool, key, v)
                print(f"recorded {rec.path.name}: {rec.seq} calls")


if __name__ == "__main__":
    argv = sys.argv[1:]
    n = int(argv[argv.index("--variants") + 1]) if "--variants" in argv else 1
    keys = [a for a in argv if not a.startswith("--") and not a.isdigit()]
    asyncio.run(main(keys or ["PAY-101"], n))
