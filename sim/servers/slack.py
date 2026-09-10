"""Simulated Slack MCP server (tool names after korotovsky/slack-mcp-server)."""
from datetime import datetime, timezone
from mcp.server.mcpserver import MCPServer
from common import world, text, slack_query, match_terms, parse_time

mcp = MCPServer("sim-slack", instructions="Simulated Slack. Search supports quoted phrases, bare terms (AND), in:#channel, from:@user, after:/before: YYYY-MM-DD.")


def _im_channels() -> list[dict]:
    """Direct-message channels look like Slack `im` conversations: D… id, no real name (we show @user)."""
    return [{"id": d["id"], "name": f"@{n}", "is_im": True, "user": d["user"]} for n, d in world().get("dms", {}).items()]


def _channel_by_name(name: str) -> dict | None:
    name = name.lstrip("#")
    for c in [*world()["channels"].values(), *_im_channels()]:
        if c["name"] == name or c["name"] == "@" + name:
            return c
    return None


def _channel_by_id(cid: str) -> dict | None:
    for c in [*world()["channels"].values(), *_im_channels()]:
        if c["id"] == cid:
            return c
    return None


def _all_messages():
    w = world()
    out = []
    for inc in w["incidents"]:
        ch = _channel_by_name(inc["slack"]["channel"])
        thread = inc["slack"]["thread_ts"]
        for i, m in enumerate(inc["slack"]["messages"]):
            out.append({"channel": ch, "ts": m["ts"], "thread_ts": thread, "user": m["user"], "user_name": m["user_name"],
                        "text": m["text"], "reply_count": len(inc["slack"]["messages"]) - 1 if i == 0 else 0, "is_reply": i > 0})
    for m in w["noise"]["slack"]:
        thread_ts = m.get("thread_ts", m["ts"])
        out.append({"channel": _channel_by_name(m["channel"]), "ts": m["ts"], "thread_ts": thread_ts, "user": m["user"],
                    "user_name": m["user_name"], "text": m["text"], "reply_count": 0, "is_reply": thread_ts != m["ts"]})
    for d in w.get("deploys", []):
        out.append({"channel": _channel_by_name(d["channel"]), "ts": d["thread_ts"], "thread_ts": d["thread_ts"],
                    "user": "", "user_name": "deploybot", "text": d["message"], "reply_count": 0, "is_reply": False})
    for ch in _im_channels():
        for m in w["dms"][ch["name"].lstrip("@")]["messages"]:
            out.append({"channel": ch, "ts": m["ts"], "thread_ts": m["ts"], "user": m["user"], "user_name": m["user_name"],
                        "text": m["text"], "reply_count": 0, "is_reply": False})
    return out


def _fmt(m: dict) -> dict:
    ch = m["channel"]
    when = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).replace(microsecond=0)
    return {"ts": m["ts"], "thread_ts": m["thread_ts"], "time": when.isoformat().replace("+00:00", "Z"),
            "channel": {"id": ch["id"], "name": ch["name"], **({"is_im": True} if ch.get("is_im") else {})},
            "user": m["user"], "username": m["user_name"], "text": m["text"],
            "permalink": f"https://sim.slack.com/archives/{ch['id']}/p{m['ts'].replace('.', '')}",
            "reply_count": m["reply_count"]}


@mcp.tool(structured_output=False, name="conversations_search_messages", description="Search messages. search_query supports quoted phrases, bare terms (all must match), and modifiers in:#channel from:@user after:YYYY-MM-DD before:YYYY-MM-DD. Returns up to `limit` matches (max 100).")
def conversations_search_messages(search_query: str = "", filter_in_channel: str = "", filter_users_from: str = "",
                                  filter_date_after: str = "", filter_date_before: str = "", limit: int = 20, cursor: str = "") -> str:
    terms, mods = slack_query(search_query)
    chan = filter_in_channel or mods.get("in", "")
    after = parse_time(filter_date_after or mods.get("after", "") or None)
    before = parse_time(filter_date_before or mods.get("before", "") or None)
    if before and len((filter_date_before or mods.get("before", "")).strip()) == 10:
        from datetime import timedelta
        before = before + timedelta(days=1)  # day-granular 'before:' is inclusive of that day
    user = (filter_users_from or mods.get("from", "")).lstrip("@")
    hits = []
    for m in _all_messages():
        if chan and m["channel"]["name"] != chan.lstrip("#") and m["channel"]["id"] != chan:
            continue
        if user and m["user_name"] != user and m["user"] != user:
            continue
        when = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)
        if after and when < after:
            continue
        if before and when > before:
            continue
        if terms and not match_terms(m["text"], terms, "and"):
            continue
        hits.append(m)
    hits.sort(key=lambda m: float(m["ts"]), reverse=True)
    page = int(cursor or 1)
    limit = max(1, min(int(limit), 100))
    chunk = hits[(page - 1) * limit: page * limit]
    return text({"total": len(hits), "page": page, "messages": {"matches": [_fmt(m) for m in chunk]},
                 "next_cursor": str(page + 1) if page * limit < len(hits) else ""})


@mcp.tool(structured_output=False, name="conversations_replies", description="Fetch a thread: the parent message and its replies. channel_id is the C… (or D… for a DM) id, thread_ts the parent ts.")
def conversations_replies(channel_id: str, thread_ts: str, limit: int = 100) -> str:
    ch = _channel_by_id(channel_id) or _channel_by_name(channel_id)
    msgs = [m for m in _all_messages() if ch and m["channel"]["id"] == ch["id"] and m["thread_ts"] == thread_ts]
    msgs.sort(key=lambda m: float(m["ts"]))
    return text({"messages": [_fmt(m) for m in msgs[:limit]]})


@mcp.tool(structured_output=False, name="conversations_history", description="Recent top-level messages in a channel or DM (channel_id C…, D… for a DM, #name or @user). limit max 200.")
def conversations_history(channel_id: str, limit: int = 50) -> str:
    ch = _channel_by_id(channel_id) or _channel_by_name(channel_id)
    msgs = [m for m in _all_messages() if ch and m["channel"]["id"] == ch["id"] and not m["is_reply"]]
    msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
    return text({"messages": [_fmt(m) for m in msgs[:min(int(limit), 200)]]})


@mcp.tool(structured_output=False, name="channels_list", description="List channels (C…) and direct-message conversations (D…, is_im=true, name @user) with ids and names.")
def channels_list(channel_types: str = "public_channel,im") -> str:
    kinds = {k.strip() for k in channel_types.split(",") if k.strip()}
    out = [{**c, "is_im": False} for c in world()["channels"].values()] if kinds & {"public_channel", "private_channel"} else []
    if "im" in kinds:
        out += _im_channels()
    return text({"channels": out})


@mcp.tool(structured_output=False, name="users_list", description="List users with ids, names and teams.")
def users_list() -> str:
    w = world()
    return text({"users": [*w["people"].values(), *([w["me"]] if w.get("me") else [])]})


if __name__ == "__main__":
    mcp.run()
