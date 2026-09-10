"""The /import and /candidates pages: past Claude Code sessions as traces, and the recurring behaviours mined from
the workspace's episodes, each inducible into a draft flow with one click. No LLM anywhere here.

Mounted from crystal/app/main.py with `app.include_router(routes_import.router)`. Templates and the workspace come
from the app at request time (no import cycle at module load)."""
from __future__ import annotations

import re
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from crystal import workspace as ws_mod
from crystal.induce.dataflow import cache_status
from crystal.induce.dataflow import candidates as mine_dataflow
from crystal.induce.mining import candidates as mine_sequences
from crystal.induce.mining import induce_candidate
from crystal.trace.transcripts import import_transcripts, importable, transcripts_dir

router = APIRouter()
TOP = 30


def _workspace(request: Request) -> ws_mod.Workspace:
    st = request.app.state
    return st.workspace if hasattr(st, "workspace") else ws_mod.current()


def _templates():
    from crystal.app.main import TEMPLATES
    return TEMPLATES


def _flow_names(flow_dir: Path) -> set[str]:
    return {p.stem for p in flow_dir.glob("*.yaml")} if flow_dir.is_dir() else set()


@router.get("/import", response_class=HTMLResponse)
async def import_page(request: Request, msg: str = ""):
    ws = _workspace(request)
    base = transcripts_dir()
    rows = importable(base, ws.root) if base.is_dir() else []
    rows.sort(key=lambda r: (r.get("status") != "would import", -r.get("mcp_calls", 0)))
    totals = {"found": len(rows), "importable": sum(r.get("status") == "would import" for r in rows),
              "imported": sum(r.get("status") == "already imported" for r in rows),
              "mcp": sum(r.get("mcp_calls", 0) for r in rows)}
    return _templates().TemplateResponse(request, "import.html", {"rows": rows, "base": str(base), "base_exists": base.is_dir(),
                                                                   "totals": totals, "msg": msg})


@router.post("/import")
async def import_run(request: Request, force: str = Form("")):
    ws = _workspace(request)
    base = transcripts_dir()
    if not base.is_dir():
        return RedirectResponse("/import?msg=" + quote(f"no transcripts directory at {base}"), status_code=303)
    rep = import_transcripts(base, workspace_root=ws.root, all_projects=False, dry_run=False, force=force == "yes")
    servers = ", ".join(f"{s} {n}" for s, n in rep["servers"].items())
    msg = (f"Imported {rep['imported']} session{'s' if rep['imported'] != 1 else ''} ({rep['episodes']} episodes), "
           f"{rep['skipped']} already present. Calls per server: {servers or 'none'}.")
    return RedirectResponse("/import?msg=" + quote(msg), status_code=303)


# Analysing a workspace's episodes is the whole cost of dataflow mining and runs once per trace file. A workspace
# with thousands of episodes would otherwise hold the request open for minutes (behind a tunnel it just times out),
# so the page renders progress and does the work in a background thread.
_ANALYSIS: dict[str, dict] = {}
INLINE_EPISODES = 150        # few enough to analyse inside the request (~10s); more goes to the background


def _analysis_job(key: str, traces, meta) -> dict:
    job = _ANALYSIS.get(key)
    if job and job.get("running"):
        return job
    status = cache_status(traces, workspace_meta=meta)
    if status["missing"] <= INLINE_EPISODES:
        _ANALYSIS.pop(key, None)
        return {"running": False, **status, "missing": 0}   # small enough to do inline; the page just waits
    job = {"running": True, **status}
    _ANALYSIS[key] = job

    def work():
        try:
            mine_dataflow(traces, limit=TOP, workspace_meta=meta)
        except Exception as e:  # noqa: BLE001
            job["error"] = str(e)
        finally:
            job["running"] = False
            job.update(cache_status(traces, workspace_meta=meta))
    threading.Thread(target=work, daemon=True).start()
    return job


@router.get("/candidates/analysis")
async def candidates_analysis(request: Request):
    ws = _workspace(request)
    key = ws.state.slug
    job = _ANALYSIS.get(key)
    if job is None or not job.get("running"):
        job = {"running": False, **cache_status(ws.state.traces, workspace_meta=ws.meta())}
    else:
        job = {**job, **{k: v for k, v in cache_status(ws.state.traces, workspace_meta=ws.meta()).items()}}
    return JSONResponse(job)


@router.get("/candidates", response_class=HTMLResponse)
async def candidates_page(request: Request, msg: str = "", miner: str = "dataflow"):
    ws = _workspace(request)
    st = ws.state
    sequences = miner == "sequences"
    if not sequences:
        job = _analysis_job(st.slug, st.traces, ws.meta())
        if job.get("running") or job.get("missing"):
            return _templates().TemplateResponse(request, "candidates_analysing.html",
                                                 {"job": job, "msg": msg, "miner": miner})
    top = max(1, min(int(request.query_params.get("top") or TOP), 500))
    allc = (mine_sequences(st.traces) if sequences
            else mine_dataflow(st.traces, workspace_meta=ws.meta()))
    cands = allc[:top]
    views = []
    for c in cands:
        v = c.view()
        v["default_name"] = f"mined-{c.servers[0]}-{c.servers[-1]}-{c.rank}" if c.servers else f"mined-{c.rank}"
        views.append(v)
    return _templates().TemplateResponse(request, "candidates.html",
                                         {"cands": views, "msg": msg, "miner": miner, "sequences": sequences,
                                          "flows": sorted(_flow_names(st.flows)), "top": top, "total": len(allc),
                                          "episodes": len({e for c in allc for e in c.episode_ids})})


@router.post("/candidates/{rank}/induce")
async def candidates_induce(request: Request, rank: int, name: str = Form(""), miner: str = Form("dataflow")):
    from crystal.induce.inducer import dump_flow
    ws = _workspace(request)
    st = ws.state
    cands = (mine_sequences(st.traces) if miner == "sequences"
             else mine_dataflow(st.traces, workspace_meta=ws.meta()))
    if rank < 1 or rank > len(cands):
        return RedirectResponse("/candidates?msg=" + quote(f"no candidate #{rank}"), status_code=303)
    cand = cands[rank - 1]
    name = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or (f"mined-{cand.servers[0]}-{cand.servers[-1]}-{rank}" if cand.servers else f"mined-{rank}")
    flow, _report = induce_candidate(cand, name, workspace_meta=ws.meta())
    st.flows.mkdir(parents=True, exist_ok=True)
    (st.flows / f"{name}.yaml").write_text(dump_flow(flow))
    return RedirectResponse(f"/flows/{name}", status_code=303)
