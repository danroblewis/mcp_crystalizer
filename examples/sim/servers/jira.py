"""Simulated Jira MCP server (tool names after sooperset/mcp-atlassian)."""
import json
from mcp.server.mcpserver import MCPServer
from common import world, text, jql_match

mcp = MCPServer("sim-jira", instructions="Simulated Jira. JQL subset: key, project, component, status, labels, text ~, summary ~, updated >= -Nd.")


def _all_issues():
    w = world()
    return [i["jira"] for i in w["incidents"]] + w["noise"]["jira"]


def _fmt(issue: dict, full: bool = True) -> dict:
    out = {
        "key": issue["key"],
        "id": str(abs(hash(issue["key"])) % 100000),
        "fields": {
            "summary": issue["summary"], "status": {"name": issue["status"]}, "issuetype": {"name": issue["issuetype"]},
            "priority": {"name": issue["priority"]}, "project": {"key": issue["project"]},
            "components": [{"name": c} for c in issue["components"]], "labels": issue["labels"],
            "reporter": {"displayName": issue["reporter"]}, "assignee": {"displayName": issue["assignee"]},
            "created": issue["created"], "updated": issue["updated"],
            "issuelinks": issue.get("issuelinks", []),
        },
    }
    if full:
        out["fields"]["description"] = issue["description"]
    return out


@mcp.tool(structured_output=False, name="jira_get_issue", description="Get a Jira issue by key (e.g. PAY-101). Returns fields incl. description.")
def jira_get_issue(issue_key: str) -> str:
    for i in _all_issues():
        if i["key"].lower() == issue_key.lower():
            return text(_fmt(i))
    return text({"error": f"Issue {issue_key} not found"})


@mcp.tool(structured_output=False, name="jira_search", description="Search issues with JQL. Returns up to `limit` issues (default 10, max 50) with summary fields; description is included.")
def jira_search(jql: str, limit: int = 10, start_at: int = 0) -> str:
    limit = max(1, min(int(limit), 50))
    hits = [i for i in _all_issues() if jql_match(i, jql)]
    hits.sort(key=lambda i: i["updated"], reverse=True)
    page = hits[start_at:start_at + limit]
    return text({"total": len(hits), "start_at": start_at, "issues": [_fmt(i) for i in page]})


@mcp.tool(structured_output=False, name="jira_get_issue_comments", description="Comments on an issue (simulation: derived from the incident's Slack summary, or a real comments array when the world has one).")
def jira_get_issue_comments(issue_key: str) -> str:
    for inc in world()["incidents"]:
        if inc["jira"]["key"].lower() == issue_key.lower():
            if inc["jira"].get("comments"):
                return text({"comments": inc["jira"]["comments"]})
            return text({"comments": [{"author": inc["jira"]["assignee"], "created": inc["jira"]["updated"],
                                       "body": f"Resolved by rolling back {inc['commit'][:10]}. See Slack thread in {inc['slack']['channel']}."}]})
    return text({"comments": []})


if __name__ == "__main__":
    mcp.run()
