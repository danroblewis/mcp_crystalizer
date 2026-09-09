"""A scripted stand-in for the AI agent, used to produce realistic traces without spending tokens.

It investigates a Jira ticket the way the agent does: read the ticket, pick strings out of it, chain tools,
carry values forward. `variant` changes the order and search phrasing a little, as real agent runs do, so the
inducer has to generalise across sessions. Records to traces/<session>.jsonl with source=scripted.

  uv run python -m crystal.trace.scripted PAY-101 STF-109 SUP-102 PLAT-107 --variants 3
  uv run python -m crystal.trace.scripted --trigger slack_thread C542575C5/1786015740.000000 ... --variants 3
  uv run python -m crystal.trace.scripted --trigger slack_dm D59227FD8/1786020840.000500 ... --variants 3

The Slack triggers take channel/ts pairs; `investigate_slack_thread` and `investigate_slack_dm` mirror the
owner's other two flows (added to a thread; DM'd in prose).
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


def _mine(texts: list[str]) -> dict:
    """What an agent picks out of a pile of Slack text: typed ids and the service (gazetteer)."""
    blob = "\n".join(texts)
    gz = gazetteer_for("service")
    hits = gz.find(blob)
    return {"keys": ids.find_all(blob, "jira_key"), "trace_ids": ids.find_all(blob, "trace_id"),
            "classes": ids.find_all(blob, "error_class"), "pods": ids.find_all(blob, "k8s_pod"),
            "shas": [m["value"] for m in ids.typed_mentions(blob) if m["type"] == "sha_short"],
            "sigs": re.findall(r"`([A-Z][A-Za-z]+: [^`]+)`", blob),
            "service": hits[0]["name"] if hits else None, "svc": gz.entities[hits[0]["name"]] if hits else None}


def _recorder_call(rec: Recorder, pool: ServerPool):
    async def call(server, tool, args, why=""):
        if why:
            rec.note(why)
        out, raw, err = await pool.call_raw(server, tool, args)
        rec.record(server, tool, args, out, raw, is_error=err)
        return out
    return call


async def investigate_slack_thread(pool: ServerPool, channel_id: str, thread_ts: str, variant: int = 0) -> Recorder:
    """Trigger: someone added me to a Slack thread. slack -> jira + confluence -> chronosphere + logz."""
    rec = Recorder(f"scripted-thread-{thread_ts}-v{variant}", "scripted",
                   meta={"trigger": "slack_thread", "inputs": {"channel_id": channel_id, "thread_ts": thread_ts}, "variant": variant})
    call = _recorder_call(rec, pool)
    thread = await call("slack", "conversations_replies", {"channel_id": channel_id, "thread_ts": thread_ts, "limit": 100}, "read the thread I was added to")
    msgs = thread["messages"]
    if not msgs:
        rec.note("empty thread, stopping")
        return rec
    m = _mine([x["text"] for x in msgs])
    if not m["service"]:
        rec.note("thread names no known service; stopping")
        return rec
    service, svc = m["service"], m["svc"]
    error_class = m["classes"][0] if m["classes"] else ""
    t_thread = datetime.fromisoformat(msgs[0]["time"].replace("Z", "+00:00"))
    w_start, w_end = t_thread - timedelta(hours=1), t_thread + timedelta(hours=6)
    key = m["keys"][0] if m["keys"] else None
    trace_ids = list(m["trace_ids"])
    error_sig = m["sigs"][0] if m["sigs"] else error_class

    async def issue():
        nonlocal error_sig
        if variant % 3 == 1:
            r = await call("jira", "jira_search", {"jql": f'text ~ "{error_class}" ORDER BY created DESC', "limit": 5}, "find the ticket for this error")
            k2 = r["issues"][0]["key"] if r["issues"] else key
        else:
            k2 = key
        if k2:
            i = await call("jira", "jira_get_issue", {"issue_key": k2}, "read the ticket the thread names")
            mm = re.search(r"^Error:\s*(.+)$", i["fields"]["description"], re.M)
            if mm:
                error_sig = mm.group(1)
            trace_ids.extend(t for t in ids.find_all(i["fields"]["description"], "trace_id") if t not in trace_ids)

    async def confluence():
        if variant % 3 == 0:
            q = f'title ~ "{service} runbook"'
        elif variant % 3 == 1:
            q = f'text ~ "{error_class}" AND space = {svc["confluence_space"]}'
        else:
            q = f'text ~ "{error_class}"'
        c = await call("confluence", "confluence_search", {"query": q, "limit": 5}, "look for the runbook")
        if c["results"]:
            await call("confluence", "confluence_get_page", {"page_id": c["results"][0]["id"]}, "read the runbook")

    async def metrics():
        q = (f'sum(rate(http_requests_total{{service="{service}",code=~"5.."}}[5m]))' if variant % 3 == 1
             else f'rate(http_requests_total{{service="{service}",code=~"5.."}}[5m])')
        await call("chronosphere", "query_prometheus_range", {"query": q, "start": _iso(w_start), "end": _iso(w_end), "step_seconds": 300}, "error rate around the thread")

    async def logs():
        for tid in trace_ids[:4]:
            await call("logz", "search_logs", {"query": f"trace_id:{tid}", "from_time": _iso(w_start), "to_time": _iso(w_end), "size": 20}, "logs for a trace id from the thread")

    async def logs_by_error():
        q = f'"{error_class}" AND service:{service}' if variant % 3 == 1 else f'"{error_sig}" AND service:{service}'
        await call("logz", "search_logs", {"query": q, "from_time": _iso(w_start), "to_time": _iso(w_end), "size": 50}, "all lines with the error")

    async def code():
        await call("code", "grep", {"pattern": error_class, "glob": "**/*.py", "context": 2}, "where the error is raised")

    async def pagerduty():
        await call("pagerduty", "list_incidents", {"service_ids": svc["pagerduty_service_id"], "since": _iso(w_start), "until": _iso(w_end), "limit": 5}, "the page for this incident")

    async def commits():
        await call("git", "git_log", {"path": svc["repo_path"], "since": _iso(t_thread - timedelta(days=4)), "until": _iso(t_thread + timedelta(days=1)), "max_count": 10}, "recent commits to the service")

    order = {0: [issue, confluence, metrics, logs, logs_by_error, pagerduty],
             1: [issue, code, confluence, logs, metrics, logs_by_error],
             2: [confluence, issue, metrics, logs, logs_by_error, pagerduty, commits]}[variant % 3]
    for step in order:
        await step()
    rec.note("done", summary={"service": service, "error_class": error_class, "key": key, "trace_ids": trace_ids})
    return rec


async def investigate_slack_dm(pool: ServerPool, channel_id: str, ts: str, variant: int = 0) -> Recorder:
    """Trigger: someone DM'd me in prose. The flow's input is the DM text (what the driver's prompt carries);
    the channel/ts only locate that text. slack -> confluence -> codebase -> chronosphere + logz + jira."""
    hist, _, _ = await pool.call_raw("slack", "conversations_history", {"channel_id": channel_id, "limit": 50})
    dm = next((x for x in hist["messages"] if x["ts"] == ts), None)
    if not dm:
        raise ValueError(f"no DM {ts} in {channel_id}")
    text = dm["text"]
    rec = Recorder(f"scripted-dm-{ts}-v{variant}", "scripted", meta={"trigger": "slack_dm", "inputs": {"text": text}, "variant": variant})
    call = _recorder_call(rec, pool)

    r = await call("slack", "conversations_search_messages", {"search_query": f'"{text}"', "limit": 5}, "find the DM in Slack to see who sent it and when")
    hit = r["messages"]["matches"][0]
    t_dm = datetime.fromisoformat(hit["time"].replace("Z", "+00:00"))
    w_start, w_end = t_dm - timedelta(hours=6), t_dm + timedelta(hours=2)
    day0, day1 = w_start.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")
    h = await call("slack", "conversations_history", {"channel_id": hit["channel"]["id"], "limit": 20}, "read the rest of the DM conversation for context")
    dm_m = _mine([text])
    ctx_m = _mine([x["text"] for x in h["messages"]])
    key = dm_m["keys"][0] if dm_m["keys"] else None
    error_class = dm_m["classes"][0] if dm_m["classes"] else ""
    service = dm_m["service"]
    trace_ids = [t for t in ctx_m["trace_ids"]]

    # find the incident discussion
    if key:
        q = f"{key} in:#incidents"
    elif variant % 3 == 0 and error_class:
        q = f"{error_class} in:#incidents after:{day0} before:{day1}"
    elif variant % 3 == 2 and error_class and service:
        q = f'{service} {error_class} in:#incidents'
    else:
        q = f"{service} in:#incidents after:{day0} before:{day1}"
    r2 = await call("slack", "conversations_search_messages", {"search_query": q, "limit": 20}, "search the incident channel for what the DM describes")
    matches = r2["messages"]["matches"]
    if not matches:
        rec.note("no incident discussion found; stopping")
        return rec
    thread = await call("slack", "conversations_replies", {"channel_id": matches[0]["channel"]["id"], "thread_ts": matches[0]["thread_ts"], "limit": 100}, "read the incident thread")
    th_m = _mine([x["text"] for x in thread["messages"]])
    service = service or th_m["service"]
    svc = gazetteer_for("service").entities[service]
    error_class = error_class or (th_m["classes"][0] if th_m["classes"] else "")
    key = key or (th_m["keys"][0] if th_m["keys"] else None)
    trace_ids += [t for t in th_m["trace_ids"] if t not in trace_ids]

    async def confluence():
        q = {0: f'title ~ "{service} runbook"', 1: f'text ~ "{error_class}" AND space = {svc["confluence_space"]}', 2: f"{service} runbook"}[variant % 3]
        c = await call("confluence", "confluence_search", {"query": q, "limit": 5}, "look for the runbook")
        if c["results"]:
            await call("confluence", "confluence_get_page", {"page_id": c["results"][0]["id"]}, "read the runbook")

    async def code():
        g = await call("code", "grep", {"pattern": error_class, "glob": "**/*.py", "context": 2}, "where the error is raised")
        files = list(dict.fromkeys(x["file"] for x in g["matches"]))
        if files and variant % 3 == 0:
            await call("code", "codeowners", {"path": files[0]}, "who owns it")
        if files and variant % 3 == 2:
            await call("code", "read_file", {"path": files[0]}, "read the handler")

    async def metrics():
        await call("chronosphere", "query_prometheus_range", {"query": f'rate(http_requests_total{{service="{service}",code=~"5.."}}[5m])', "start": _iso(w_start), "end": _iso(w_end), "step_seconds": 300}, "error rate before the DM")

    async def logs():
        for tid in trace_ids[:4]:
            await call("logz", "search_logs", {"query": f"trace_id:{tid}", "from_time": _iso(w_start), "to_time": _iso(w_end), "size": 20}, "logs for a trace id someone pasted")

    async def logs_by_error():
        await call("logz", "search_logs", {"query": f'"{error_class}" AND service:{service}', "from_time": _iso(w_start), "to_time": _iso(w_end), "size": 50}, "all lines with the error")

    async def issue():
        if variant % 3 == 1:
            await call("jira", "jira_search", {"jql": f"component = {service} AND labels = incident ORDER BY created DESC", "limit": 5}, "incident tickets for the service")
        if key:
            await call("jira", "jira_get_issue", {"issue_key": key}, "read the ticket")

    order = {0: [confluence, code, metrics, logs, logs_by_error, issue],
             1: [confluence, code, logs, metrics, logs_by_error, issue],
             2: [confluence, code, metrics, logs, logs_by_error, issue]}[variant % 3]
    for step in order:
        await step()
    rec.note("done", summary={"service": service, "error_class": error_class, "key": key, "trace_ids": trace_ids})
    return rec


async def main(keys: list[str], variants: int, trigger: str = "jira_issue") -> None:
    async with ServerPool() as pool:
        for key in keys:
            for v in range(variants):
                if trigger == "slack_thread":
                    rec = await investigate_slack_thread(pool, *key.split("/", 1), variant=v)
                elif trigger == "slack_dm":
                    rec = await investigate_slack_dm(pool, *key.split("/", 1), variant=v)
                else:
                    rec = await investigate(pool, key, v)
                print(f"recorded {rec.path.name}: {rec.seq} calls")


if __name__ == "__main__":
    argv = sys.argv[1:]
    n = int(argv[argv.index("--variants") + 1]) if "--variants" in argv else 1
    trigger = argv[argv.index("--trigger") + 1] if "--trigger" in argv else "jira_issue"
    skip = {argv[i + 1] for i, a in enumerate(argv) if a in ("--variants", "--trigger") and i + 1 < len(argv)}
    keys = [a for a in argv if not a.startswith("--") and a not in skip]
    asyncio.run(main(keys or ["PAY-101"], n, trigger))
