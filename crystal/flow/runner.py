"""Flow interpreter: runs a crystallized flow against MCP servers with no AI.

Flow YAML:
  name, status, trigger, inputs{name: {type, required, default}}, catalog_kinds (optional)
  steps: list of
    id, tool "server.tool", args {..templates..}, when (template -> truthy), forEach "<expr over ctx>",
    max_items, extract {name: extractor spec}, hits (jsonpath to the list that decides whether a ladder rung hit),
    title (for the UI)
  Any arg value may be {ladder: [rung, rung, ...]}: rungs are rendered in order, blank rungs are skipped,
  the first rung whose result has hits wins; if none hit, the last non-blank rung's result is kept.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from crystal import state
from crystal.extract.catalog import load_catalog
from crystal.extract.extractors import run_extractor, select
from crystal.flow.templating import ENV, render
from crystal.mcp_client import ServerPool


def current_run_dir() -> Path:
    """The current workspace's runs/ under $MCP_EXPLORER_HOME."""
    return state.run_dir()


_VERSION_SUFFIX = re.compile(r"\.v(\d+)$")


def flow_path(name: str, flow_dir: Path | None = None) -> Path:
    """The file for a flow name. `author`, `repair` and `refine` write `<name>.v<N>.yaml`, while the flow's own
    `name:` field stays unversioned, so a link built from that name has no `<name>.yaml` to open: fall back to the
    newest version of it."""
    d = flow_dir or state.flow_dir()
    exact = d / f"{name}.yaml"
    if exact.exists():
        return exact
    versions = []
    for p in d.glob(f"{name}.v*.yaml"):
        m = _VERSION_SUFFIX.search(p.stem)
        if m:
            versions.append((int(m.group(1)), p))
    return max(versions)[1] if versions else exact


def load_flow(name_or_path: str | Path, flow_dir: Path | None = None) -> dict:
    """A flow by name (from the workspace's flow dir, newest version when only versions exist) or by path."""
    p = Path(name_or_path)
    if not p.exists():
        p = flow_path(str(name_or_path), flow_dir)
    flow = yaml.safe_load(p.read_text())
    flow["_path"] = str(p)
    flow["slug"] = p.stem                       # how the flow is addressed in URLs and on the CLI
    return flow


def list_flows(flow_dir: Path | None = None) -> list[dict]:
    """Every flow in the workspace's flow dir (an empty list when the workspace has none yet)."""
    out = []
    d = flow_dir or state.flow_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        try:
            f = yaml.safe_load(p.read_text())
            f["_path"] = str(p)
            f["slug"] = p.stem
            out.append(f)
        except Exception as e:  # noqa: BLE001
            out.append({"name": p.stem, "slug": p.stem, "status": "broken", "error": str(e), "_path": str(p)})
    return out


def count_hits(result: Any, hits_path: str | None) -> int:
    """How many 'things' a result contains. Uses `hits` jsonpath if given, else the first list in the result."""
    if hits_path:
        vals = select(result, hits_path)
        if len(vals) == 1 and isinstance(vals[0], list):
            return len(vals[0])
        return len(vals)
    if isinstance(result, dict):
        if "error" in result and len(result) == 1:
            return 0
        found = None
        for v in result.values():
            if isinstance(v, list):
                return len(v)
            if isinstance(v, dict):
                n = _first_list_len(v)
                if n is not None:
                    found = n if found is None else found
        if found is not None:
            return found
        return 1 if result else 0
    if isinstance(result, list):
        return len(result)
    return 1 if result else 0


def _first_list_len(d: dict) -> int | None:
    for v in d.values():
        if isinstance(v, list):
            return len(v)
        if isinstance(v, dict):
            n = _first_list_len(v)
            if n is not None:
                return n
    return None


def _truthy(v: Any) -> bool:
    return bool(v) and str(v).strip().lower() not in ("", "false", "none", "0", "[]", "{}")


class FlowRunner:
    def __init__(self, pool: ServerPool, catalog: dict | None = None, recorder=None, lifecycle=None):
        self.pool = pool
        self.catalog = catalog if catalog is not None else load_catalog()
        self.recorder = recorder  # optional crystal.trace.record.Recorder
        # promotion lifecycle: None = the default store when the run is saved; False = never record; or a Lifecycle
        self.lifecycle = lifecycle

    async def _call(self, server: str, tool: str, args: dict) -> tuple[Any, str | None, float]:
        t0 = time.perf_counter()
        try:
            out, raw, is_err = await self.pool.call_raw(server, tool, args)
            if not is_err and isinstance(out, str) and out.lstrip().lower().startswith(("error:", "fatal:")):
                is_err = True   # servers that report failures as plain text (e.g. git_show on a bad sha)
            err = str(out) if is_err else None
        except Exception as e:  # noqa: BLE001
            out, raw, err = None, "", str(e)
        dt = (time.perf_counter() - t0) * 1000
        if self.recorder:
            self.recorder.record(server, tool, args, out, raw, is_error=err is not None, duration_ms=dt)
        return out, err, dt

    async def _run_call(self, step: dict, ctx: dict) -> dict:
        """Render args (descending any ladders), call the tool, extract. Returns the step record."""
        server, tool = step["tool"].split(".", 1)
        raw_args = step.get("args", {})
        ladder_keys = [k for k, v in raw_args.items() if isinstance(v, dict) and "ladder" in v]
        base = {k: v for k, v in raw_args.items() if k not in ladder_keys}
        rendered_base = render(base, ctx)
        attempts = []
        if ladder_keys:
            # Several args may carry ladders (the inducer emits one per arg whose binding varied across sessions).
            # Attempt i takes rung i of every laddered arg (a key with fewer rungs keeps its last; a blank rung falls
            # back to the key's first non-blank one). The primary key (longest ladder) decides blank-skipping.
            hits_path = step.get("hits")
            key = max(ladder_keys, key=lambda k: len(raw_args[k]["ladder"]))
            n_rungs = len(raw_args[key]["ladder"])
            result, err, dt, used, tried = None, None, 0.0, None, []
            for i in range(n_rungs):
                vals = {}
                for k in ladder_keys:
                    rungs = raw_args[k]["ladder"]
                    v = render(rungs[min(i, len(rungs) - 1)], ctx)
                    if not str(v).strip() and k != key:
                        v = next((x for x in (render(r, ctx) for r in rungs) if str(x).strip()), v)
                    vals[k] = v
                val = vals[key]
                if not str(val).strip():
                    attempts.append({"rung": i, "value": val, "skipped": "blank"})
                    continue
                args = {**rendered_base, **vals}
                if args in tried:
                    attempts.append({"rung": i, "value": val, "skipped": "duplicate"})
                    continue
                tried.append(args)
                result, err, dt = await self._call(server, tool, args)
                n = 0 if err else count_hits(result, hits_path)
                attempts.append({"rung": i, "value": val, "hits": n, "error": err, **({"values": vals} if len(ladder_keys) > 1 else {})})
                used = args
                if n > 0:
                    break
            args = used or rendered_base
        else:
            args = rendered_base
            result, err, dt = await self._call(server, tool, args)
        extracts = {}
        if result is not None and not err:
            for name, spec in (step.get("extract") or {}).items():
                try:
                    extracts[name] = run_extractor(spec, result, self.catalog)
                except Exception as e:  # noqa: BLE001
                    extracts[name] = None
                    err = (err or "") + f" extract[{name}]: {e}"
        return {"tool": step["tool"], "args": args, "result": result, "error": err, "duration_ms": round(dt, 1),
                "attempts": attempts, "extracts": extracts, "hits": 0 if err else count_hits(result, step.get("hits"))}

    async def run(self, flow: dict, inputs: dict, save: bool = True) -> dict:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        from crystal.workspace import current as _current_workspace
        ctx: dict[str, Any] = {"inputs": self._coerce_inputs(flow, inputs), "catalog": self.catalog,
                               "workspace": _current_workspace().meta()}
        record = {"run_id": run_id, "flow": flow["name"], "flow_path": flow.get("_path"), "inputs": ctx["inputs"],
                  "started": datetime.now(timezone.utc).isoformat(), "steps": [], "status": "ok"}
        for step in flow["steps"]:
            sid = step["id"]
            entry: dict[str, Any] = {"id": sid, "title": step.get("title", sid), "tool": step.get("tool")}
            if step.get("when") is not None and not _truthy(render(str(step["when"]), ctx)):
                entry["skipped"] = "when=false"
                record["steps"].append(entry)
                ctx[sid] = {"skipped": True, "items": [], "result": None}
                continue
            if "forEach" in step:
                items = render("{{ (" + step["forEach"] + ") | tojson }}", ctx)
                try:
                    items = json.loads(items)
                except json.JSONDecodeError:
                    items = []
                if not isinstance(items, list):
                    items = [items]
                items = [i for i in items if i not in (None, "")][: int(step.get("max_items", 10))]
                entry["items"] = []
                for item in items:
                    sub = await self._run_call(step, {**ctx, "item": item})
                    sub["item"] = item
                    entry["items"].append(sub)
                # step namespace: list of item results + merged extracts (lists)
                merged: dict[str, list] = {}
                for sub in entry["items"]:
                    for k, v in sub["extracts"].items():
                        merged.setdefault(k, [])
                        if isinstance(v, list):
                            merged[k].extend(x for x in v if x not in merged[k])
                        elif v is not None and v not in merged[k]:
                            merged[k].append(v)
                ctx[sid] = {"items": entry["items"], "results": [s["result"] for s in entry["items"]], **merged}
                entry["hits"] = sum(s["hits"] for s in entry["items"])
                failed = [s for s in entry["items"] if s["error"]]
                if failed and len(failed) == len(entry["items"]):
                    entry["error"] = "; ".join(s["error"] for s in failed)          # every item failed: the step failed
                elif failed:
                    entry["partial_errors"] = len(failed)                            # some items failed (red herrings): step still stands
            else:
                sub = await self._run_call(step, ctx)
                entry.update(sub)
                ctx[sid] = {"result": sub["result"], "args": sub["args"], **sub["extracts"]}
            if entry.get("error") and step.get("required", False):
                record["status"] = "failed"
                record["steps"].append(entry)
                break
            record["steps"].append(entry)
        record["finished"] = datetime.now(timezone.utc).isoformat()
        record["summary"] = {s["id"]: {"hits": s.get("hits"), "error": s.get("error"), "skipped": s.get("skipped")} for s in record["steps"]}
        if save:
            self._record_lifecycle(record, flow)
            d = current_run_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{run_id}.json").write_text(json.dumps(record, indent=1, default=str))
        return record

    def _record_lifecycle(self, record: dict, flow: dict) -> None:
        """Saved runs are live runs: feed the outcome to the circuit breaker. Unsaved runs (tests, dry runs) do not count.
        The shared sqlite store may be locked by another writer (UI, CLI, flows server); that must not lose the run."""
        if self.lifecycle is False:
            return
        from crystal.flow.lifecycle import get_lifecycle
        try:
            lc = self.lifecycle or get_lifecycle()
            st = lc.record_run(record, flow)
        except Exception as e:  # noqa: BLE001
            record["lifecycle"] = {"error": f"{type(e).__name__}: {e}"}
            return
        record["lifecycle"] = {"outcome": st["outcome"], "reason": st["reason"], "status": st["status"],
                               "author_status": st["author_status"], "transition": st.get("transition")}

    @staticmethod
    def _coerce_inputs(flow: dict, inputs: dict) -> dict:
        out = {}
        for name, spec in (flow.get("inputs") or {}).items():
            v = inputs.get(name, spec.get("default"))
            if v in (None, "") and spec.get("required"):
                raise ValueError(f"missing required input {name!r}")
            out[name] = v
        for k, v in inputs.items():
            out.setdefault(k, v)
        return out


async def run_flow(name: str, inputs: dict, recorder=None, save: bool = True, lifecycle=None) -> dict:
    flow = load_flow(name)
    async with ServerPool() as pool:
        return await FlowRunner(pool, recorder=recorder, lifecycle=lifecycle).run(flow, inputs, save=save)


def run_flow_sync(name: str, inputs: dict, **kw) -> dict:
    return asyncio.run(run_flow(name, inputs, **kw))
