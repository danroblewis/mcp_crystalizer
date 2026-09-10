"""MCP server exposing the crystallized flows as tools, so the authoring agent can try a flow before exploring.

Not a simulation: it runs the real flow interpreter (crystal.flow.runner) over the other registered servers and
returns a compact run summary with evidence (never raw results; capped in size). Registered as `flows` in
servers.yaml / .mcp.json. The recorder hook records mcp__flows__run_flow calls like any other, so a trace reads
"ran flow X, then did Y" - the diff the inducer needs.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crystal.app.render import cards  # noqa: E402
from crystal.flow.lifecycle import describe, get_lifecycle  # noqa: E402
from crystal.flow.runner import FlowRunner, list_flows, load_flow  # noqa: E402
from crystal.mcp_client import ServerPool, load_registry  # noqa: E402

mcp = MCPServer("flows", instructions="Crystallized investigation flows. Call list_flows, then run_flow(name, inputs_json) "
                                       "before exploring with the raw tools; it returns the evidence the flow gathered.")

MAX_CHARS = 14000      # cap on the JSON returned by run_flow
CARDS_PER_STEP = 3
TEXT_LIMIT = 280


def _text(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


def _trim(v, limit=TEXT_LIMIT):
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + "…"
    if isinstance(v, list):
        return [_trim(x, limit) for x in v[:12]] + (["…"] if len(v) > 12 else [])
    if isinstance(v, dict):
        return {k: _trim(x, limit) for k, x in v.items() if k not in ("points", "context")}
    return v


def _evidence(tool: str, result, n: int) -> list:
    out = []
    for c in cards(tool, result)[:n]:
        if c.get("kind") == "json":
            c = {"kind": "json", "text": c["text"][:TEXT_LIMIT * 2]}
        out.append(_trim(c))
    return out


def summarize_run(record: dict, cards_per_step: int = CARDS_PER_STEP, max_chars: int = MAX_CHARS) -> dict:
    """Compact, size-capped view of a run record: per step the query, hits, extracts and a few evidence cards."""
    def build(n_cards: int, with_args: bool) -> dict:
        steps = []
        for s in record["steps"]:
            e = {"id": s["id"], "tool": s.get("tool"), "hits": s.get("hits")}
            if s.get("skipped"):
                e["skipped"] = s["skipped"]
                steps.append(e)
                continue
            if s.get("error"):
                e["error"] = str(s["error"])[:300]
            if "items" in s:
                e["items"] = []
                for it in s["items"][:6]:
                    sub = {"item": _trim(it.get("item")), "hits": it.get("hits"), "extracts": _trim(it.get("extracts") or {})}
                    if with_args:
                        sub["args"] = _trim(it.get("args"))
                    if it.get("error"):
                        sub["error"] = str(it["error"])[:200]
                    if n_cards:
                        sub["evidence"] = _evidence(s["tool"], it.get("result"), n_cards)
                    e["items"].append(sub)
                if len(s["items"]) > 6:
                    e["more_items"] = len(s["items"]) - 6
            else:
                if with_args:
                    e["args"] = _trim(s.get("args"))
                if s.get("attempts") and len(s["attempts"]) > 1:
                    e["ladder"] = [{"rung": a.get("rung"), "value": _trim(a.get("value"), 160), "hits": a.get("hits", "skipped")} for a in s["attempts"]]
                e["extracts"] = _trim(s.get("extracts") or {})
                if n_cards:
                    e["evidence"] = _evidence(s["tool"], s.get("result"), n_cards)
            steps.append(e)
        return {"run_id": record["run_id"], "flow": record["flow"], "status": record["status"], "inputs": record["inputs"],
                "lifecycle": record.get("lifecycle"), "steps": steps}

    for n_cards, with_args in ((cards_per_step, True), (2, True), (1, True), (1, False), (0, False)):
        out = build(n_cards, with_args)
        if len(_text(out)) <= max_chars:
            out["truncated"] = (n_cards, with_args) != (cards_per_step, True)
            return out
    out["truncated"] = True
    out["steps"] = [{k: v for k, v in s.items() if k in ("id", "tool", "hits", "error", "skipped")} for s in out["steps"]]
    return out


@mcp.tool(structured_output=False, name="list_flows",
          description="List the crystallized investigation flows: name, trigger, inputs, steps, status. Run one with run_flow before exploring by hand.")
def list_flows_tool() -> str:
    lc = get_lifecycle()
    out = []
    for f in list_flows():
        if f.get("status") == "broken":
            continue
        st = describe(lc.view(f))
        out.append({"name": f["name"], "title": f.get("title"), "description": (f.get("description") or "").strip()[:300],
                    "trigger": (f.get("trigger") or {}).get("type"), "status": f.get("status"), "effective_status": st["status"],
                    "inputs": {k: {"type": v.get("type"), "required": v.get("required", False), "example": v.get("example")}
                               for k, v in (f.get("inputs") or {}).items()},
                    "steps": [{"id": s["id"], "tool": s.get("tool"), **({"forEach": s["forEach"]} if s.get("forEach") else {})} for s in f.get("steps", [])]})
    return _text({"flows": out})


@mcp.tool(structured_output=False, name="run_flow",
          description="Run a crystallized flow end to end (no AI) and return its evidence: per step the query, hit count, "
                      "extracted values and a few evidence cards. inputs_json is a JSON object, e.g. '{\"key\": \"PAY-101\"}'. "
                      "The full run is saved as runs/<run_id>.json.")
async def run_flow_tool(name: str, inputs_json: str = "{}") -> str:
    try:
        inputs = json.loads(inputs_json) if inputs_json.strip() else {}
        if not isinstance(inputs, dict):
            raise ValueError("inputs_json must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return _text({"error": f"bad inputs_json: {e}"})
    try:
        flow = load_flow(name)
    except FileNotFoundError:
        return _text({"error": f"no flow named {name!r}", "flows": [f["name"] for f in list_flows()]})
    registry = {k: v for k, v in load_registry().items() if k != "flows"}   # never recurse into ourselves
    try:
        async with ServerPool(registry) as pool:
            # saved (the author's expansion needs the run record) but lifecycle=False: an agent's exploratory run,
            # possibly with made-up inputs, is not live evidence for or against the flow
            record = await FlowRunner(pool, lifecycle=False).run(flow, inputs, save=True)
    except BaseException as e:  # noqa: BLE001  (anyio raises ExceptionGroup)
        if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        return _text({"error": f"flow failed to run: {_explain(e)}"})
    return _text(summarize_run(record))


def _explain(e: BaseException) -> str:
    subs = getattr(e, "exceptions", None)
    if subs:
        return "; ".join(_explain(s) for s in subs)
    return f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    mcp.run()
