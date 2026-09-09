"""Simulated Confluence MCP server (tool names after sooperset/mcp-atlassian)."""
from mcp.server.mcpserver import MCPServer
from common import world, text, cql_match, match_terms, tokenize

mcp = MCPServer("sim-confluence", instructions="Simulated Confluence. confluence_search takes CQL (text ~, title ~, space, label) or plain text.")


def _fmt(p: dict, body: bool = False) -> dict:
    out = {"id": p["id"], "title": p["title"], "space": {"key": p["space"]}, "labels": p["labels"],
           "created": p["created"], "last_modified": p["last_modified"], "url": f"https://sim.atlassian.net/wiki/pages/{p['id']}"}
    if body:
        out["body"] = p["body"]
    else:
        out["excerpt"] = p["body"][:240]
    return out


@mcp.tool(structured_output=False, name="confluence_search", description="Search pages. `query` may be CQL (e.g. text ~ \"PaymentGatewayTimeout\" AND space = PAYMENTS) or plain text (wrapped as siteSearch). limit max 50.")
def confluence_search(query: str, limit: int = 10, spaces_filter: str = "") -> str:
    pages = world()["confluence_pages"]
    is_cql = any(op in query for op in ("~", " = ", " in ", "lastModified", "type ="))
    if is_cql:
        hits = [p for p in pages if cql_match(p, query)]
    else:
        hits = [p for p in pages if match_terms(p["title"] + " " + p["body"], tokenize(query), "or")]
    if spaces_filter:
        allowed = [s.strip().upper() for s in spaces_filter.split(",")]
        hits = [p for p in hits if p["space"].upper() in allowed]
    hits.sort(key=lambda p: p["last_modified"], reverse=True)
    return text({"total": len(hits), "results": [_fmt(p) for p in hits[:min(int(limit), 50)]]})


@mcp.tool(structured_output=False, name="confluence_get_page", description="Get a page by id or by exact title. Returns the full body.")
def confluence_get_page(page_id: str = "", title: str = "") -> str:
    for p in world()["confluence_pages"]:
        if (page_id and p["id"] == str(page_id)) or (title and p["title"].lower() == title.lower()):
            return text(_fmt(p, body=True))
    return text({"error": "page not found"})


if __name__ == "__main__":
    mcp.run()
