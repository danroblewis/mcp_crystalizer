"""Simulated PagerDuty MCP server (tool shapes after PagerDuty/pagerduty-mcp-server)."""
from datetime import datetime, timezone
from mcp.server.mcpserver import MCPServer
from common import world, text, parse_time

mcp = MCPServer("sim-pagerduty", instructions="Simulated PagerDuty. No free-text search; filter by service_ids, statuses, since/until.")


def _fmt(inc: dict) -> dict:
    pd = inc["pagerduty"]
    svc = world()["services"][inc["service"]]
    return {"id": pd["id"], "incident_number": pd["incident_number"], "title": pd["title"], "status": pd["status"],
            "urgency": pd["urgency"], "created_at": pd["created_at"], "resolved_at": pd["resolved_at"],
            "service": {"id": pd["service_id"], "summary": inc["service"]},
            "assignments": [{"assignee": {"summary": pd["assignee"]}}],
            "teams": [{"summary": svc["team"]}],
            "html_url": f"https://sim.pagerduty.com/incidents/{pd['id']}",
            "body": {"details": f"{inc['error_sig']}\ntrace_id={inc['trace_ids'][0]}\npod={inc['pods'][0]}"}}


@mcp.tool(structured_output=False, name="list_incidents", description="List incidents. service_ids comma-separated, statuses comma-separated (triggered,acknowledged,resolved), since/until ISO-8601. limit max 100.")
def list_incidents(service_ids: str = "", statuses: str = "", since: str = "", until: str = "", limit: int = 25) -> str:
    sids = [s.strip() for s in service_ids.split(",") if s.strip()]
    sts = [s.strip() for s in statuses.split(",") if s.strip()]
    s = parse_time(since or None)
    u = parse_time(until or None)
    out = []
    for inc in world()["incidents"]:
        pd = inc["pagerduty"]
        if sids and pd["service_id"] not in sids:
            continue
        if sts and pd["status"] not in sts:
            continue
        when = datetime.fromisoformat(pd["created_at"].replace("Z", "+00:00"))
        if s and when < s:
            continue
        if u and when > u:
            continue
        out.append(_fmt(inc))
    out.sort(key=lambda i: i["created_at"], reverse=True)
    return text({"incidents": out[:min(int(limit), 100)], "total": len(out)})


@mcp.tool(structured_output=False, name="get_incident", description="Get one incident by id (Q…) or incident number.")
def get_incident(incident_id: str) -> str:
    for inc in world()["incidents"]:
        pd = inc["pagerduty"]
        if pd["id"] == incident_id or str(pd["incident_number"]) == str(incident_id):
            return text(_fmt(inc))
    return text({"error": "not found"})


@mcp.tool(structured_output=False, name="list_services", description="PagerDuty services with ids and owning teams.")
def list_services() -> str:
    return text({"services": [{"id": v["pagerduty_service_id"], "name": k, "team": v["team"]} for k, v in world()["services"].items()]})


if __name__ == "__main__":
    mcp.run()
