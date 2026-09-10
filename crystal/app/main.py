"""mcp_explorer web UI: pick a crystallized flow, enter starting parameters, get a dossier. No AI at runtime.

  mcp-explorer serve --port 8765   (crystal/cli.py)
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from crystal.app import dossier
from crystal import PROJECT_ROOT
from crystal import workspace as ws_mod
from crystal.flow.lifecycle import describe, get_lifecycle
from crystal.flow.runner import FlowRunner, RUN_DIR, list_flows, load_flow
from crystal.mcp_client import ServerPool
from crystal.trace.record import TRACE_DIR
from crystal.trace.store import load_session

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals["sparkline"] = dossier.sparkline
FEEDBACK = PROJECT_ROOT / "traces" / "feedback.jsonl"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # one workspace per server process: $CRYSTAL_WORKSPACE (else the project = the sim) picks the servers the pool
    # connects to (servers.yaml < ~/.mcp.json < <workspace>/.mcp.json) and the runs/ + traces/ namespace
    app.state.workspace = ws_mod.current()
    app.state.pool = ServerPool()
    print(f"workspace {app.state.workspace}; servers: {', '.join(app.state.pool.registry)}", flush=True)
    await app.state.pool.__aenter__()
    try:
        yield
    finally:
        await app.state.pool.__aexit__(None, None, None)


app = FastAPI(title="mcp_explorer", lifespan=lifespan)


def _run_dir() -> Path:
    return app.state.run_dir if hasattr(app.state, "run_dir") else ws_mod.namespaced(RUN_DIR)


def _trace_dir() -> Path:
    return app.state.trace_dir if hasattr(app.state, "trace_dir") else ws_mod.namespaced(TRACE_DIR)


def _runs(limit: int = 50) -> list[dict]:
    out = []
    d = _run_dir()
    if d.exists():
        for p in sorted(d.glob("*.json"), reverse=True)[:limit]:
            try:
                r = json.loads(p.read_text())
                out.append({"run_id": r["run_id"], "flow": r["flow"], "inputs": r["inputs"], "started": r["started"],
                            "status": r["status"], "hits": sum((s.get("hits") or 0) for s in r["steps"])})
            except Exception:  # noqa: BLE001
                continue
    return out


def _lifecycle(flow: dict) -> dict:
    """Effective runtime state (badge, counters, last failure) next to the author's status from the YAML."""
    lc = get_lifecycle()
    st = lc.view(flow)
    return {**st, **describe(st)}


def _load_flow_quiet(name: str | None, path: str | None = None) -> dict | None:
    """The flow a run was made from, for the diagram edges and the card; None if the YAML is gone or unreadable."""
    for cand in (path, name):
        if not cand:
            continue
        try:
            return load_flow(cand)
        except Exception:  # noqa: BLE001
            continue
    return None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    flows = [f for f in list_flows() if f.get("status") != "broken"]
    for f in flows:
        f["lifecycle"] = _lifecycle(f)
    return TEMPLATES.TemplateResponse(request, "index.html", {"flows": flows, "runs": _runs(15)})


@app.get("/flows/{name}", response_class=HTMLResponse)
async def flow_form(request: Request, name: str):
    flow = load_flow(name)
    flow["lifecycle"] = _lifecycle(flow)
    events = get_lifecycle().events(name, limit=12)
    prefill = {k: v for k, v in request.query_params.items() if k in (flow.get("inputs") or {})}
    return TEMPLATES.TemplateResponse(request, "flow.html", {"flow": flow, "events": events, "prefill": prefill,
                                                            "runs": [r for r in _runs(100) if r["flow"] == name][:10]})


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
    p = _run_dir() / f"{run_id}.json"
    if not p.exists():
        return HTMLResponse("run not found", status_code=404)
    record = json.loads(p.read_text())
    flow = _load_flow_quiet(record.get("flow"), record.get("flow_path"))
    view = dossier.build(record, flow)
    return TEMPLATES.TemplateResponse(request, "dossier.html", {"r": record, "flow": flow, "msg": msg, "trace": False, **view})


@app.get("/runs/{run_id}/json", response_class=PlainTextResponse)
async def run_json(run_id: str):
    p = _run_dir() / f"{run_id}.json"
    if not p.exists():
        return PlainTextResponse("run not found", status_code=404)
    return p.read_text()


@app.get("/traces", response_class=HTMLResponse)
async def traces(request: Request):
    out = []
    for p in sorted(_trace_dir().glob("*.jsonl")):
        try:
            s = load_session(p)
        except Exception:  # noqa: BLE001
            continue
        if not s.calls:
            continue
        out.append({"session_id": s.session_id, "source": s.source, "trigger": s.meta.get("trigger"), "inputs": s.meta.get("inputs") or {},
                    "calls": len(s.calls), "ts": s.calls[0].get("ts")})
    out.sort(key=lambda t: t["ts"] or "", reverse=True)
    return TEMPLATES.TemplateResponse(request, "traces.html", {"traces": out})


@app.get("/traces/{session}", response_class=HTMLResponse)
async def trace_view(request: Request, session: str):
    p = _trace_dir() / f"{session}.jsonl"
    if not p.exists() or "/" in session or ".." in session:
        return HTMLResponse("trace not found", status_code=404)
    record = dossier.session_record(load_session(p))
    view = dossier.build(record, None)
    return TEMPLATES.TemplateResponse(request, "dossier.html", {"r": record, "flow": None, "msg": "", "trace": True, **view})


@app.get("/traces/{session}/json", response_class=PlainTextResponse)
async def trace_json(session: str):
    p = _trace_dir() / f"{session}.jsonl"
    if not p.exists() or "/" in session or ".." in session:
        return PlainTextResponse("trace not found", status_code=404)
    return p.read_text()


@app.post("/runs/{run_id}/feedback")
async def feedback(run_id: str, text: str = Form(""), helpful: str = Form("no")):
    p = _run_dir() / f"{run_id}.json"
    record = json.loads(p.read_text()) if p.exists() else {}
    FEEDBACK.parent.mkdir(exist_ok=True)
    with FEEDBACK.open("a") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "kind": "feedback", "run_id": run_id, "flow": record.get("flow"),
                             "inputs": record.get("inputs"), "helpful": helpful == "yes", "text": text}) + "\n")
    if helpful != "yes" and record.get("flow"):
        # a complaint is a failure signal: the circuit breaker demotes the flow one level
        lc = get_lifecycle()
        try:
            flow = load_flow(record["flow"])
        except Exception:  # noqa: BLE001
            # YAML unreadable right now (mid-edit): keep the author status the store already knows rather than
            # declaring the flow a draft, which would reset its effective state
            known = lc.get(record["flow"])
            flow = {"name": record["flow"], "status": known["author_status"] if known else "draft"}
        st = lc.record_feedback(flow, run_id, text)
        tr = st.get("transition")
        msg = "Recorded.+A+repair+request+is+queued+for+the+flow+author+(crystal+repair)."
        if tr:
            msg += f"+Flow+demoted+{tr[0]}+%E2%86%92+{tr[1]}."
        return RedirectResponse(f"/runs/{run_id}?msg={msg}", status_code=303)
    return RedirectResponse(f"/runs/{run_id}?msg=Thanks,+recorded.", status_code=303)
