"""Agent launches from the UI: /record (one recorded Claude Code session) and /flows/<f>/refine (refine a draft).

The page is the web form of `mcp-explorer record`: a question, a trigger name, a budget cap, a model, and an
explicit confirmation ("this launches Claude Code and costs money"). The launch runs crystal.trace.driver.run_agent
in a background thread, so the request returns at once with a job page that tails the trace file the recording
hook writes (the session id, hence the file name, is chosen before the process starts). When the process ends the
job record carries the cost, the turns and the agent's final message, links to the trace dossier, and offers
"induce a flow from this session" (author.induce_version: this session plus every earlier session of the same
trigger -> <trigger>.v<N>.yaml, status draft; no LLM).

Guardrails, in the order they are checked: the checkbox, `claude` on PATH, a positive budget (passed to the driver
as --max-budget-usd), one run at a time per workspace (records/.lock in the state dir, stale locks recovered), and
every job persisted as records/<job>.json so a server restart keeps the history.

The **Refine with an agent** button on a flow page posts to /flows/<flow>/refine and reuses all of that: the same
lock, the same background thread, the same job records and the same job page. That job runs `crystal.refine.refine`,
which asks the agent for a name, a card, step titles, inputs and argument bindings and then DECIDES each proposal
deterministically -- every binding is replayed against the episodes the flow was induced from and kept only when it
reproduces what they actually sent. The job page shows that accepted/rejected table and links to the new version.

Nothing here runs without a POST from the form, and the UI never calls an LLM on its own (invariant 4).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from crystal import workspace as ws_mod
from crystal.trace import driver
from crystal.trace.store import load_session

router = APIRouter()

DEFAULT_TRIGGER = "prompt"
DEFAULT_BUDGET = 2.0
TERMINAL = ("done", "error", "interrupted")
_THREADS: dict[str, threading.Thread] = {}      # jobs running in this process
_WRITE = threading.Lock()                       # job records are read-modify-written from request and job threads


# ---------------------------------------------------------------- where things live
def _workspace(request: Request) -> ws_mod.Workspace:
    st = request.app.state
    return st.workspace if hasattr(st, "workspace") else ws_mod.current()


def records_dir(ws: ws_mod.Workspace) -> Path:
    d = ws.state.ensure().dir / "records"
    d.mkdir(exist_ok=True)
    return d


def _templates():
    from crystal.app.main import TEMPLATES     # lazy: main mounts this router, so no import cycle at load time
    return TEMPLATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(job: str) -> bool:
    return bool(job) and "/" not in job and ".." not in job and not job.startswith(".")


# ---------------------------------------------------------------- job records
def load_job(ws: ws_mod.Workspace, job: str) -> dict | None:
    p = records_dir(ws) / f"{job}.json"
    if not _safe(job) or not p.exists():
        return None
    try:
        return _reconcile(ws, json.loads(p.read_text()))
    except json.JSONDecodeError:
        return None


def save_job(ws: ws_mod.Workspace, rec: dict) -> Path:
    p = records_dir(ws) / f"{rec['job']}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2, default=str) + "\n")
    os.replace(tmp, p)
    return p


def _update(ws: ws_mod.Workspace, job: str, **fields) -> dict:
    with _WRITE:
        p = records_dir(ws) / f"{job}.json"
        rec = json.loads(p.read_text()) if p.exists() else {"job": job}
        rec.update(fields)
        save_job(ws, rec)
    return rec


def list_jobs(ws: ws_mod.Workspace, limit: int = 100) -> list[dict]:
    out = []
    for p in sorted(records_dir(ws).glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(_reconcile(ws, json.loads(p.read_text())))
        except json.JSONDecodeError:
            continue
    return out


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _reconcile(ws: ws_mod.Workspace, rec: dict) -> dict:
    """A job that says `running` but whose server process is gone (restart, crash) is marked interrupted, so the
    history stays honest and the lock it held can be recovered."""
    if rec.get("status") == "running":
        thread = _THREADS.get(rec["job"])
        mine = rec.get("server_pid") == os.getpid()
        alive = (thread is not None and thread.is_alive()) if mine else _pid_alive(rec.get("server_pid"))
        if not alive:
            rec = _update(ws, rec["job"], status="interrupted", finished=_now(),
                          error="the server stopped while this run was in progress; the trace holds what was recorded")
    return rec


# ---------------------------------------------------------------- the lock: one run at a time per workspace
class Busy(Exception):
    def __init__(self, job: str):
        super().__init__(f"a run is already in progress: {job}")
        self.job = job


def lock_path(ws: ws_mod.Workspace) -> Path:
    return records_dir(ws) / ".lock"


def acquire_lock(ws: ws_mod.Workspace, job: str) -> None:
    p = lock_path(ws)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                held = {}
            other = load_job(ws, held.get("job", "")) if held.get("job") else None
            if other is not None and other.get("status") == "running":
                raise Busy(other["job"])
            p.unlink(missing_ok=True)     # stale: its job is over (or the record is gone); try once more
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"job": job, "server_pid": os.getpid(), "started": _now()}))
        return
    raise Busy(job)


def release_lock(ws: ws_mod.Workspace, job: str) -> None:
    p = lock_path(ws)
    try:
        if json.loads(p.read_text()).get("job") == job:
            p.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass


def running_job(ws: ws_mod.Workspace) -> dict | None:
    for j in list_jobs(ws, limit=20):
        if j.get("status") == "running":
            return j
    return None


# ---------------------------------------------------------------- launching
def build_prompt(trigger: str, question: str) -> str:
    """The trigger's prompt template when it has one and the question fills it; else the question verbatim (the
    `prompt` trigger: what a user would type into Claude Code)."""
    tpl = driver.PROMPTS.get(trigger)
    if tpl:
        try:
            return tpl.format(question=question, inputs=json.dumps({"question": question}))
        except (KeyError, IndexError):
            pass
    return question


def start_job(ws: ws_mod.Workspace, question: str, trigger: str, budget: float, model: str | None) -> dict:
    """Write the job record, take the lock, and run the driver in a background thread. Raises Busy."""
    job = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    sid = str(uuid.uuid4())
    acquire_lock(ws, job)
    rec = {"job": job, "session_id": sid, "trigger": trigger, "question": question, "prompt": build_prompt(trigger, question),
           "budget": budget, "model": model, "status": "running", "started": _now(), "finished": None,
           "server_pid": os.getpid(), "pid": None, "cost_usd": None, "num_turns": None, "duration_ms": None,
           "is_error": None, "result": None, "error": None, "trace_path": str(ws.state.traces / f"{sid}.jsonl"),
           "workspace": ws.slug, "induced": None}
    save_job(ws, rec)
    t = threading.Thread(target=_run, args=(ws, rec), name=f"record-{job}", daemon=True)
    _THREADS[job] = t
    t.start()
    return rec


def start_refine_job(ws: ws_mod.Workspace, flow_name: str, budget: float, model: str | None,
                     new_name: str | None) -> dict:
    """The same job machinery as a recording, running `crystal.refine.refine` instead. Raises Busy."""
    job = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    sid = str(uuid.uuid4())
    acquire_lock(ws, job)
    rec = {"job": job, "kind": "refine", "session_id": sid, "trigger": "refine", "flow": flow_name,
           "new_name": new_name, "question": f"refine the draft flow {flow_name}", "prompt": "",
           "budget": budget, "model": model, "status": "running", "started": _now(), "finished": None,
           "server_pid": os.getpid(), "pid": None, "cost_usd": None, "num_turns": None, "duration_ms": None,
           "is_error": None, "result": None, "error": None, "trace_path": str(ws.state.traces / f"{sid}.jsonl"),
           "workspace": ws.slug, "induced": None, "refine": None}
    save_job(ws, rec)
    t = threading.Thread(target=_run_refine, args=(ws, rec), name=f"refine-{job}", daemon=True)
    _THREADS[job] = t
    t.start()
    return rec


def _run_refine(ws: ws_mod.Workspace, rec: dict) -> None:
    """The refine job: the agent proposes, crystal.refine decides, a new version is written. Costs money."""
    from crystal import refine as refine_mod
    job = rec["job"]
    try:
        def driver_fn(trigger, inputs, prompt, budget=None, model=None, meta=None, **kw):
            _update(ws, job, prompt=prompt)
            return driver.run_agent(trigger, inputs, prompt, budget=budget, model=model, meta=meta,
                                    session_id=rec["session_id"], quiet=True, workspace=ws.root,
                                    on_start=lambda i: _update(ws, job, pid=i.get("pid")))
        report = refine_mod.refine(rec["flow"], budget=rec["budget"], model=rec["model"], name=rec["new_name"],
                                   driver_fn=driver_fn, flow_dir=ws.state.flows, trace_dir=ws.state.traces,
                                   workspace=ws.root, lock=False)     # the job already holds the workspace lock
        info = report.get("agent") or {}
        _update(ws, job, status="done", finished=_now(), cost_usd=report.get("cost_usd"), num_turns=info.get("num_turns"),
                duration_ms=info.get("duration_ms"), is_error=bool(info.get("is_error")), result=info.get("result") or "",
                refine=refine_view(report))
    except Exception as e:  # noqa: BLE001
        _update(ws, job, status="error", finished=_now(), error=f"{type(e).__name__}: {e}")
    finally:
        release_lock(ws, job)
        _THREADS.pop(job, None)


def refine_view(report: dict) -> dict:
    """The accepted/rejected table the job page shows: one row per proposal, plus the bindability and the new flow."""
    rows = [{"what": "name", "value": str(report["name"].get("proposed") or "(none)"),
             "verdict": "accepted" if report["name"]["accepted"] else "rejected",
             "why": report["name"]["reason"] or f"using {report['name']['used']}"},
            {"what": "card", "value": "agent prose",
             "verdict": "accepted" if report["card"]["accepted"] else "rejected", "why": report["card"]["reason"]}]
    for t in report["step_titles"]:
        rows.append({"what": f"title {t['step']}", "value": t["title"],
                     "verdict": "accepted" if t["accepted"] else "rejected", "why": t["reason"]})
    for i in report["inputs"]:
        rows.append({"what": f"input {i['name']}", "value": str(i["spec"].get("type") or "string"),
                     "verdict": "accepted" if i["accepted"] else "dropped", "why": i["reason"]})
    for b in report["bindings"]:
        rows.append({"what": f"bind {b['step']}.{b['arg']}", "value": str(b.get("template") or ""),
                     "verdict": "accepted" if b.get("accepted") else ("unfixable" if b.get("unfixable") else "rejected"),
                     "why": b.get("reason", "")})
    flow = report.get("flow") or {}
    return {"rows": rows, "bindability": report["bindability"], "path": report.get("path"),
            "flow": flow.get("name"), "version": flow.get("version"), "from": flow.get("refined_from"),
            "episodes": len(report.get("episodes") or []), "proposal_found": report.get("proposal_found"),
            "accepted": sum(1 for r in rows if r["verdict"] == "accepted"), "total": len(rows)}


def _run(ws: ws_mod.Workspace, rec: dict) -> None:
    job = rec["job"]
    try:
        info = driver.run_agent(rec["trigger"], {"question": rec["question"]}, rec["prompt"], budget=rec["budget"],
                                model=rec["model"], meta={"command": "record-ui", "job": job}, session_id=rec["session_id"],
                                quiet=True, workspace=ws.root,
                                on_start=lambda i: _update(ws, job, pid=i.get("pid")))
        _update(ws, job, status="done", finished=_now(), cost_usd=info.get("cost_usd"), num_turns=info.get("num_turns"),
                duration_ms=info.get("duration_ms"), is_error=bool(info.get("is_error")), result=info.get("result") or "",
                error=(info.get("stderr") or "")[-800:] if info.get("returncode") else None)
    except Exception as e:  # noqa: BLE001
        _update(ws, job, status="error", finished=_now(), error=f"{type(e).__name__}: {e}")
    finally:
        release_lock(ws, job)
        _THREADS.pop(job, None)


# ---------------------------------------------------------------- what the pages show
def trace_view(rec: dict) -> dict:
    """The calls recorded so far, tailed from the trace file the hook appends to."""
    p = Path(rec.get("trace_path") or "")
    if not p.exists():
        return {"calls": [], "n_calls": 0, "prompt": None, "result": None}
    try:
        s = load_session(p)
    except Exception:  # noqa: BLE001
        return {"calls": [], "n_calls": 0, "prompt": None, "result": None}
    calls = [{"seq": c.get("seq"), "server": c.get("server"), "tool": c.get("tool"), "ts": (c.get("ts") or "")[11:19],
              "input": json.dumps(c.get("input"), default=str)[:160], "is_error": bool(c.get("is_error")),
              "duration_ms": c.get("duration_ms")} for c in s.calls]
    return {"calls": calls, "n_calls": len(calls), "prompt": s.prompt, "result": s.result}


def _elapsed(rec: dict) -> float:
    try:
        a = datetime.fromisoformat(rec["started"])
        b = datetime.fromisoformat(rec["finished"]) if rec.get("finished") else datetime.now(timezone.utc)
        return max(0.0, (b - a).total_seconds())
    except (KeyError, TypeError, ValueError):
        return 0.0


def status_view(rec: dict) -> dict:
    tv = trace_view(rec)
    return {"job": rec["job"], "status": rec.get("status"), "session_id": rec.get("session_id"), "elapsed_s": round(_elapsed(rec), 1),
            "calls": tv["calls"], "n_calls": tv["n_calls"], "cost_usd": rec.get("cost_usd"), "num_turns": rec.get("num_turns"),
            "is_error": rec.get("is_error"), "error": rec.get("error"), "result": rec.get("result") or tv["result"],
            "trace_url": f"/traces/{rec['session_id']}" if tv["n_calls"] else None,
            "induced": rec.get("induced"), "refine": rec.get("refine"), "done": rec.get("status") in TERMINAL}


def _page_state(ws: ws_mod.Workspace) -> dict:
    return {"claude": driver.claude_path(), "install_hint": driver.INSTALL_HINT, "running": running_job(ws),
            "triggers": sorted(driver.PROMPTS), "default_trigger": DEFAULT_TRIGGER, "default_budget": DEFAULT_BUDGET}


def _form_page(request: Request, ws: ws_mod.Workspace, status: int = 200, error: str = "", form: dict | None = None):
    ctx = {**_page_state(ws), "jobs": _job_rows(ws), "error": error, "form": form or {}}
    return _templates().TemplateResponse(request, "record.html", ctx, status_code=status)


def _job_rows(ws: ws_mod.Workspace) -> list[dict]:
    rows = []
    for j in list_jobs(ws):
        rows.append({**j, "n_calls": trace_view(j)["n_calls"], "elapsed_s": round(_elapsed(j))})
    return rows


# ---------------------------------------------------------------- routes
@router.get("/record", response_class=HTMLResponse)
async def record_form(request: Request):
    return _form_page(request, _workspace(request))


@router.post("/record")
async def record_start(request: Request):
    ws = _workspace(request)
    form = await request.form()
    fields = {"question": (form.get("question") or "").strip(), "trigger": (form.get("trigger") or "").strip() or DEFAULT_TRIGGER,
              "budget": (form.get("budget") or "").strip() or str(DEFAULT_BUDGET), "model": (form.get("model") or "").strip()}

    def refuse(msg: str, status: int = 400):
        return _form_page(request, ws, status, msg, fields)

    if form.get("confirm") != "yes":
        return refuse("Not started: tick the confirmation box. This launches Claude Code in the workspace and costs money.")
    if not fields["question"]:
        return refuse("Not started: the question is empty.")
    if not driver.claude_path():
        return refuse(driver.INSTALL_HINT, 409)
    try:
        budget = float(fields["budget"])
    except ValueError:
        return refuse("Not started: the budget must be a number of US dollars.")
    if not 0 < budget <= 1000:
        return refuse("Not started: the budget must be between 0 and 1000 USD.")
    trigger = fields["trigger"]
    if not trigger.replace("_", "").replace("-", "").isalnum():
        return refuse("Not started: the trigger must be a short name (letters, digits, _ or -).")
    try:
        rec = start_job(ws, fields["question"], trigger, budget, fields["model"] or None)
    except Busy as e:
        return refuse(f"Not started: a run is already in progress in this workspace (job {e.job}). Wait for it to finish.", 409)
    return RedirectResponse(f"/record/{rec['job']}", status_code=303)


@router.get("/record/{job}", response_class=HTMLResponse)
async def record_job(request: Request, job: str, msg: str = ""):
    ws = _workspace(request)
    rec = load_job(ws, job)
    if rec is None:
        return HTMLResponse("job not found", status_code=404)
    view = status_view(rec)
    ctx = {"j": rec, "s": view, "msg": msg, "prompt": trace_view(rec)["prompt"] or rec.get("prompt")}
    return _templates().TemplateResponse(request, "record_job.html", ctx)


@router.get("/record/{job}/status")
async def record_status(request: Request, job: str):
    rec = load_job(_workspace(request), job)
    if rec is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse(status_view(rec))


@router.post("/flows/{name}/refine")
async def flow_refine(request: Request, name: str):
    """Hand a draft flow to the agent for a name, a card, titles, inputs and argument bindings. COSTS MONEY: the
    checkbox, `claude` on PATH and the workspace lock all have to agree first. Every proposal is decided by
    crystal.refine against the traces, not by the agent."""
    from urllib.parse import quote_plus

    from crystal.flow.runner import load_flow
    ws = _workspace(request)
    form = await request.form()

    def refuse(msg: str):
        return RedirectResponse(f"/flows/{name}?msg={quote_plus(msg)}", status_code=303)

    try:
        flow = load_flow(name, ws.state.flows)
    except Exception:  # noqa: BLE001
        return HTMLResponse("flow not found", status_code=404)
    if form.get("confirm") != "yes":
        return refuse("Not started: tick the box. Refining launches Claude Code in this workspace and costs money.")
    if not driver.claude_path():
        return refuse(driver.INSTALL_HINT)
    if not flow.get("induced_from"):
        return refuse("Not started: this flow was not induced from recorded episodes, so no proposal could be "
                      "replayed against anything. Refine only works on induced drafts.")
    try:
        budget = float((form.get("budget") or "").strip() or DEFAULT_BUDGET)
    except ValueError:
        return refuse("Not started: the budget must be a number of US dollars.")
    if not 0 < budget <= 1000:
        return refuse("Not started: the budget must be between 0 and 1000 USD.")
    new_name = (form.get("new_name") or "").strip() or None
    try:
        rec = start_refine_job(ws, flow["name"], budget, (form.get("model") or "").strip() or None, new_name)
    except Busy as e:
        return refuse(f"Not started: a run is already in progress in this workspace (job {e.job}).")
    return RedirectResponse(f"/record/{rec['job']}", status_code=303)


@router.post("/record/{job}/induce")
async def record_induce(request: Request, job: str):
    """Induce <trigger>.v<N>.yaml (draft) from this session plus every earlier session of the same trigger."""
    from crystal.author import induce_version
    ws = _workspace(request)
    rec = load_job(ws, job)
    if rec is None:
        return HTMLResponse("job not found", status_code=404)
    if rec.get("status") not in TERMINAL:
        return RedirectResponse(f"/record/{job}?msg=The+run+is+still+in+progress.", status_code=303)
    p = Path(rec["trace_path"])
    sess = load_session(p) if p.exists() else None
    if sess is None or not sess.calls:
        return RedirectResponse(f"/record/{job}?msg=Nothing+to+induce:+the+session+recorded+no+tool+calls.", status_code=303)
    sess.meta.setdefault("workspace_meta", ws.meta())
    base = rec["trigger"].replace("_", "-")
    st = ws.state
    try:
        res = induce_version(rec["trigger"], base, sess, trace_dir=st.traces, flow_dir=st.flows, run_dir=st.runs,
                             note={"command": "record-ui", "job": job, "inputs": {"question": rec["question"]}, "cost_usd": rec.get("cost_usd")})
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/record/{job}?msg=Induction+failed:+{type(e).__name__}", status_code=303)
    flow = res["flow"]
    _update(ws, job, induced={"flow": flow["name"], "path": res.get("path"), "sessions": res.get("sessions"),
                              "unresolved": len(res.get("unresolved") or []), "at": _now()})
    return RedirectResponse(f"/record/{job}?msg=Induced+{flow['name']}+from+{res.get('sessions')}+session(s).", status_code=303)
