"""mcp_explorer web UI: pick a crystallized flow, enter starting parameters, get the evidence. No AI at runtime.

  uv run uvicorn app.main:app --reload --port 8765
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from app.render import cards
from crystal import PROJECT_ROOT
from crystal.flow.runner import FlowRunner, RUN_DIR, list_flows, load_flow
from crystal.mcp_client import ServerPool

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
FEEDBACK = PROJECT_ROOT / "traces" / "feedback.jsonl"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = ServerPool()
    await app.state.pool.__aenter__()
    try:
        yield
    finally:
        await app.state.pool.__aexit__(None, None, None)


app = FastAPI(title="mcp_explorer", lifespan=lifespan)


def _runs(limit: int = 50) -> list[dict]:
    out = []
    if RUN_DIR.exists():
        for p in sorted(RUN_DIR.glob("*.json"), reverse=True)[:limit]:
            try:
                r = json.loads(p.read_text())
                out.append({"run_id": r["run_id"], "flow": r["flow"], "inputs": r["inputs"], "started": r["started"],
                            "status": r["status"], "hits": sum((s.get("hits") or 0) for s in r["steps"])})
            except Exception:  # noqa: BLE001
                continue
    return out


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    flows = [f for f in list_flows() if f.get("status") != "broken"]
    return TEMPLATES.TemplateResponse(request, "index.html", {"flows": flows, "runs": _runs(15)})


@app.get("/flows/{name}", response_class=HTMLResponse)
async def flow_form(request: Request, name: str):
    flow = load_flow(name)
    return TEMPLATES.TemplateResponse(request, "flow.html", {"flow": flow, "runs": [r for r in _runs(100) if r["flow"] == name][:10]})


@app.get("/flows/{name}/yaml", response_class=PlainTextResponse)
async def flow_yaml(name: str):
    return Path(load_flow(name)["_path"]).read_text()


@app.post("/flows/{name}/run")
async def flow_run(request: Request, name: str):
    form = await request.form()
    flow = load_flow(name)
    inputs = {k: v for k, v in form.items() if k in (flow.get("inputs") or {})}
    record = await FlowRunner(request.app.state.pool).run(flow, inputs)
    return RedirectResponse(f"/runs/{record['run_id']}", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
async def runs(request: Request):
    return TEMPLATES.TemplateResponse(request, "runs.html", {"runs": _runs(200)})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_view(request: Request, run_id: str, msg: str = ""):
    p = RUN_DIR / f"{run_id}.json"
    if not p.exists():
        return HTMLResponse("run not found", status_code=404)
    record = json.loads(p.read_text())
    for s in record["steps"]:
        if "items" in s:
            for it in s["items"]:
                it["cards"] = cards(s["tool"], it.get("result"))
        elif "result" in s:
            s["cards"] = cards(s["tool"], s.get("result"))
    return TEMPLATES.TemplateResponse(request, "run.html", {"r": record, "msg": msg})


@app.get("/runs/{run_id}/json", response_class=PlainTextResponse)
async def run_json(run_id: str):
    return (RUN_DIR / f"{run_id}.json").read_text()


@app.post("/runs/{run_id}/feedback")
async def feedback(run_id: str, text: str = Form(""), helpful: str = Form("no")):
    p = RUN_DIR / f"{run_id}.json"
    record = json.loads(p.read_text()) if p.exists() else {}
    FEEDBACK.parent.mkdir(exist_ok=True)
    with FEEDBACK.open("a") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "run_id": run_id, "flow": record.get("flow"),
                             "inputs": record.get("inputs"), "helpful": helpful == "yes", "text": text}) + "\n")
    return RedirectResponse(f"/runs/{run_id}?msg=Recorded.+A+repair+request+is+queued+for+the+flow+author.", status_code=303)
