"""Simulated Logz.io MCP server (tool shapes after the official Logz.io MCP: search_logs takes a Lucene/query_string query)."""
from datetime import datetime, timedelta, timezone
from mcp.server.mcpserver import MCPServer
from common import world, text, lucene_match, parse_time, latest_time

mcp = MCPServer("sim-logz", instructions="Simulated Logz.io. search_logs(query: Lucene query_string subset; field:value, phrases, AND/OR, trailing *). Default time range is the last 2 days unless from_time/to_time are given.")


def _all_logs():
    for inc in world()["incidents"]:
        for l in inc["logs"]:
            yield l


def _search(query: str, from_time: str, to_time: str, size: int, log_type: str = ""):
    end = parse_time(to_time, latest_time())
    start = parse_time(from_time, end - timedelta(days=2))
    hits = []
    for l in _all_logs():
        when = datetime.fromisoformat(l["@timestamp"].replace("Z", "+00:00"))
        if when < start or when > end:
            continue
        if log_type and l["service"] != log_type:
            continue
        if query and not lucene_match(l, query):
            continue
        hits.append(l)
    hits.sort(key=lambda l: l["@timestamp"], reverse=True)
    return hits, start, end


@mcp.tool(structured_output=False, name="search_logs", description="Search logs with a Lucene/query_string query, e.g. `trace_id:abc AND level:ERROR` or `\"PaymentGatewayTimeout\"`. from_time/to_time ISO-8601 or now-2h. size max 1000. Without a time range only the last 2 days are searched.")
def search_logs(query: str, from_time: str = "", to_time: str = "", size: int = 50, log_type: str = "") -> str:
    hits, start, end = _search(query, from_time, to_time, size, log_type)
    size = max(1, min(int(size), 1000))
    return text({"total": len(hits), "range": {"from": start.isoformat(), "to": end.isoformat()},
                 "hits": [{"_source": l} for l in hits[:size]]})


@mcp.tool(structured_output=False, name="search_logs_simple", description="Full-text search for a term or phrase across the last 2 days.")
def search_logs_simple(search_term: str, size: int = 50) -> str:
    return search_logs(f'"{search_term}"' if " " in search_term else search_term, "", "", size)


@mcp.tool(structured_output=False, name="get_log_structures", description="Fields available on log documents.")
def get_log_structures() -> str:
    return text({"fields": ["@timestamp", "level", "service", "kubernetes.pod_name", "trace_id", "logger", "message"]})


if __name__ == "__main__":
    mcp.run()
