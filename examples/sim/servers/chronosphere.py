"""Simulated Chronosphere MCP server (tool shapes after chronosphereio/chronosphere-mcp prometheus tools)."""
import re
from datetime import datetime, timedelta, timezone
from mcp.server.mcpserver import MCPServer
from common import world, text, parse_time, latest_time

mcp = MCPServer("sim-chronosphere", instructions="Simulated Chronosphere metrics. PromQL subset: any expression containing a {service=\"x\"} selector; returns an error-rate series with spikes during incidents.")

_SEL = re.compile(r'service\s*(=|=~)\s*"([^"]+)"')


def _services_for(query: str) -> list[str]:
    m = _SEL.search(query)
    names = list(world()["services"])
    if not m:
        return names
    op, val = m.groups()
    if op == "=":
        return [n for n in names if n == val]
    return [n for n in names if re.fullmatch(val, n)]


def _series(service: str, start: datetime, end: datetime, step: int):
    spikes = [(datetime.fromisoformat(i["metric_spike"]["start"].replace("Z", "+00:00")),
               datetime.fromisoformat(i["metric_spike"]["end"].replace("Z", "+00:00")))
              for i in world()["incidents"] if i["service"] == service]
    vals = []
    t = start
    while t <= end:
        base = 0.4
        v = base
        for s, e in spikes:
            if s <= t <= e:
                v = 18.5
        vals.append([int(t.timestamp()), f"{v:.3f}"])
        t += timedelta(seconds=step)
    return vals


@mcp.tool(structured_output=False, name="query_prometheus_range", description="Range query. start/end ISO-8601 or now-1h. step_seconds default 60. Returns matrix result per service label.")
def query_prometheus_range(query: str, start: str = "now-1h", end: str = "now", step_seconds: int = 60, limit: int = 100) -> str:
    e = parse_time(end, latest_time())
    s = parse_time(start, e - timedelta(hours=1))
    step = max(15, int(step_seconds))
    if (e - s).total_seconds() / step > 11000:
        return text({"error": "too many points; increase step_seconds"})
    result = [{"metric": {"__name__": "http_requests_total:error_rate", "service": svc, "env": "prod"},
               "values": _series(svc, s, e, step)} for svc in _services_for(query)]
    return text({"status": "success", "data": {"resultType": "matrix", "result": result[:int(limit)]}})


@mcp.tool(structured_output=False, name="query_prometheus_instant", description="Instant query at `time` (ISO-8601 or now).")
def query_prometheus_instant(query: str, time: str = "now") -> str:
    t = parse_time(time, latest_time())
    result = [{"metric": {"service": svc}, "value": _series(svc, t, t, 60)[0]} for svc in _services_for(query)]
    return text({"status": "success", "data": {"resultType": "vector", "result": result}})


@mcp.tool(structured_output=False, name="list_prometheus_label_values", description="Values for a label (service, env, code).")
def list_prometheus_label_values(label_name: str, limit: int = 100) -> str:
    vals = {"service": list(world()["services"]), "env": ["prod", "staging"], "code": ["200", "500", "502", "503"]}.get(label_name, [])
    return text({"status": "success", "data": vals[:int(limit)]})


if __name__ == "__main__":
    mcp.run()
