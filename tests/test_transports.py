"""Remote transports: ServerPool over `type: http` (streamable HTTP) and `type: sse`, against a real MCPServer served
by uvicorn in a background thread, with headers from the registry entry reaching the server. The public servers the
README names (deepwiki, context7) are only probed when CRYSTAL_NET_TESTS=1."""
import asyncio
import os
import socket
import threading

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware

from crystal.mcp_client import ServerPool


def _server() -> MCPServer:
    mcp = MCPServer("remote-test")

    @mcp.tool(structured_output=False, name="echo", description="echo")
    def echo(text: str) -> str:
        return '{"echo": "%s"}' % text

    return mcp


class CaptureHeaders(BaseHTTPMiddleware):
    seen: list[str] = []

    async def dispatch(self, request, call_next):
        if request.headers.get("x-crystal"):
            CaptureHeaders.seen.append(request.headers["x-crystal"])
        return await call_next(request)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def remote():
    """One uvicorn serving both apps: /mcp (streamable HTTP) and /sse (SSE)."""
    from starlette.applications import Starlette
    from starlette.routing import Mount
    http_app = _server().streamable_http_app()
    sse_app = _server().sse_app()
    app = Starlette(routes=[Mount("/sse-root", app=sse_app), Mount("/", app=http_app)], lifespan=http_app.router.lifespan_context)
    app.add_middleware(CaptureHeaders)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.05)
    else:
        pytest.fail("uvicorn did not start")
    yield {"http": f"http://127.0.0.1:{port}/mcp", "sse": f"http://127.0.0.1:{port}/sse-root/sse"}
    server.should_exit = True
    t.join(timeout=5)


def _run(registry: dict, server: str):
    async def go():
        async with ServerPool(registry, inprocess=True) as pool:   # inprocess flag must not matter for remote entries
            tools = await pool.list_tools(server)
            out = await pool.call(server, "echo", {"text": "hi"})
            return pool.transport(server), [t["name"] for t in tools], out
    return asyncio.run(go())


def test_streamable_http_transport(remote):
    CaptureHeaders.seen.clear()
    reg = {"r": {"type": "http", "url": remote["http"], "headers": {"X-Crystal": "http-token"}}}
    transport, tools, out = _run(reg, "r")
    assert transport == "http" and tools == ["echo"] and out == {"echo": "hi"}
    assert "http-token" in CaptureHeaders.seen


def test_sse_transport(remote):
    CaptureHeaders.seen.clear()
    reg = {"r": {"type": "sse", "url": remote["sse"], "headers": {"X-Crystal": "sse-token"}}}
    transport, tools, out = _run(reg, "r")
    assert transport == "sse" and tools == ["echo"] and out == {"echo": "hi"}
    assert "sse-token" in CaptureHeaders.seen


def test_url_without_type_means_http(remote):
    transport, tools, _ = _run({"r": {"url": remote["http"]}}, "r")
    assert transport == "http" and tools == ["echo"]


def test_unknown_server_is_a_clear_error():
    async def go():
        async with ServerPool({"a": {"command": "true"}}) as pool:
            await pool.call("nope", "x", {})
    with pytest.raises(KeyError, match="no MCP server named 'nope'"):
        asyncio.run(go())


@pytest.mark.skipif(not os.environ.get("CRYSTAL_NET_TESTS"), reason="set CRYSTAL_NET_TESTS=1 to probe the public servers")
def test_public_http_servers():
    async def go():
        async with ServerPool({"deepwiki": {"type": "http", "url": "https://mcp.deepwiki.com/mcp"},
                               "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}}) as pool:
            dw = {t["name"] for t in await pool.list_tools("deepwiki")}
            c7 = {t["name"] for t in await pool.list_tools("context7")}
            page = await pool.call("deepwiki", "read_wiki_structure", {"repoName": "modelcontextprotocol/python-sdk"})
            return dw, c7, page
    dw, c7, page = asyncio.run(go())
    assert {"ask_question", "read_wiki_contents", "read_wiki_structure"} <= dw
    assert "resolve-library-id" in c7
    assert "python-sdk" in str(page)
