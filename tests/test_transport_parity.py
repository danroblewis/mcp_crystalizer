"""The in-process transport (CRYSTAL_INPROCESS=1, examples/sim/servers/*.py's MCPServer connected over in-memory
streams) must return exactly what real stdio does (a subprocess for each server, same as Claude Code's
.mcp.json and the UI use) -- it is only a speed shortcut for the test suite, never a different simulation.
One representative call per sim server, both transports against the same .mcp.json/world.json."""
import asyncio

from crystal.mcp_client import ServerPool

CALLS = [
    ("jira", "jira_get_issue", {"issue_key": "PAY-101"}),
    ("slack", "conversations_search_messages", {"search_query": "PAY-101", "limit": 5}),
    ("confluence", "confluence_search", {"query": "payments", "limit": 5}),
    ("logz", "search_logs", {"query": "", "size": 5}),
    ("chronosphere", "query_prometheus_range", {"query": '{service="payments-api"}', "start": "now-1h", "end": "now"}),
    ("pagerduty", "list_incidents", {"limit": 5}),
    ("git", "git_log", {"max_count": 3}),
    ("code", "glob", {"pattern": "**/*.py"}),
]


def _strip_process_local_noise(server: str, result):
    """jira_get_issue fabricates a numeric id with `hash(issue_key)`, which is randomized per Python
    process (PYTHONHASHSEED) and so legitimately differs between the in-process pool (this test's own
    process) and the stdio pool (its own subprocess) -- unrelated to which transport was used."""
    if server == "jira" and isinstance(result, dict):
        return {k: v for k, v in result.items() if k != "id"}
    return result


async def _call_one(server: str, tool: str, args: dict, inprocess: bool):
    """Its own pool, entered and exited entirely within this one task. anyio's cancel scopes (stdio_client's
    and ClientSession's task groups) are bound to whichever task opened them and must be closed by that same
    task; a shared pool's exit stack can't safely straddle sibling gather() tasks. A separate pool per task
    sidesteps that -- each is a self-contained, single-task nest -- so the 8 stdio subprocesses below can be
    spawned concurrently instead of one at a time (the whole reason this test exists is to spawn them for
    real, so doing that serially would cost as much as the unoptimized suite)."""
    async with ServerPool(inprocess=inprocess) as pool:
        return await pool.call(server, tool, args)


def test_inprocess_matches_stdio():
    """One call per sim server: the in-process transport (this test's own process) against real stdio (a
    subprocess per server, concurrently spawned -- see `_call_one`)."""
    async def go():
        inproc, stdio = await asyncio.gather(
            asyncio.gather(*(_call_one(s, t, a, True) for s, t, a in CALLS)),
            asyncio.gather(*(_call_one(s, t, a, False) for s, t, a in CALLS)),
        )
        return dict(zip((c[0] for c in CALLS), zip(inproc, stdio)))
    results = asyncio.run(go())
    mismatches = {server: (inproc, stdio) for server, (inproc, stdio) in results.items()
                  if _strip_process_local_noise(server, inproc) != _strip_process_local_noise(server, stdio)}
    assert not mismatches


def test_inprocess_pool_reports_its_mode():
    assert ServerPool(inprocess=True).inprocess is True
    assert ServerPool(inprocess=False).inprocess is False
