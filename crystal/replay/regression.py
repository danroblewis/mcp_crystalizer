"""Per-flow regression: run the flow's test cases through a cassette and report pass/fail to the lifecycle.

Test cases come from the flow YAML:
  tests:
    - inputs: { key: PAY-101 }
      expect: { slack: { min_hits: 1 }, thread: { min_hits: 1 } }     # optional per-step expectations
If the flow declares none, one case is built from the inputs' `example` values, and the expectation is a clean run
(no step error, every required step has hits) in which at least one step found something: a run where every step
returned zero hits is not evidence that the flow works. The inducer emits `tests:` with `min_hits: 1` for the steps
that had hits in every traced session, one case per traced input.
A flow with no cases at all is reported as an error and leaves the lifecycle untouched (nothing failed).

Cassette: <state dir>/cassettes/<flow>.json, seeded from the sessions the flow was induced from. Modes:
  auto (default)  replay recorded calls, go live for misses and record them
  live            re-record everything against the servers
  offline         replay only; a miss fails the case (no servers started)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from crystal.flow.lifecycle import Lifecycle, classify_run, get_lifecycle
from crystal.flow.runner import FlowRunner, load_flow
from crystal.mcp_client import ServerPool
from crystal.replay.cassette import Cassette, CassettePool, current_cassette_dir
from crystal.trace.record import current_trace_dir
from crystal.trace.store import load_session


def test_cases(flow: dict) -> list[dict]:
    cases = flow.get("tests") or []
    if cases:
        return [{"inputs": dict(c.get("inputs") or {}), "expect": dict(c.get("expect") or {})} for c in cases]
    inputs = {k: v.get("example", v.get("default")) for k, v in (flow.get("inputs") or {}).items()}
    if any(v in (None, "") for k, v in inputs.items() if (flow.get("inputs") or {}).get(k, {}).get("required")):
        return []
    return [{"inputs": inputs, "expect": {}}]


def check_case(record: dict, flow: dict, expect: dict) -> str | None:
    reason = classify_run(record, flow)
    if reason:
        return reason
    steps = {s["id"]: s for s in record["steps"]}
    if not expect and not any((s.get("hits") or 0) for s in record["steps"] if not s.get("skipped")):
        return "every step returned zero hits"
    for sid, exp in (expect or {}).items():
        s = steps.get(sid)
        if s is None:
            return f"expected step {sid} missing from run"
        if s.get("skipped"):
            return f"step {sid} was skipped"
        if "min_hits" in exp and (s.get("hits") or 0) < int(exp["min_hits"]):
            return f"step {sid}: {s.get('hits')} hits < {exp['min_hits']}"
        if "max_hits" in exp and (s.get("hits") or 0) > int(exp["max_hits"]):
            return f"step {sid}: {s.get('hits')} hits > {exp['max_hits']}"
        for k, v in (exp.get("extracts") or {}).items():
            got = s.get("extracts", {}).get(k)
            if got != v and not (isinstance(got, list) and v in got):
                return f"step {sid}: extract {k}={got!r}, expected {v!r}"
    return None


def seed_cassette(flow: dict, path: Path, trace_dir: Path | None = None) -> Cassette:
    c = Cassette(path)
    if not path.exists():
        for sid in flow.get("induced_from") or []:
            p = (trace_dir or current_trace_dir()) / f"{sid}.jsonl"
            if p.exists():
                for call in load_session(p).calls:
                    c.put(call["server"], call["tool"], call.get("input") or {}, call.get("output"), call.get("output_text", ""), bool(call.get("is_error")))
    return c


def regression(flow_or_name, mode: str = "auto", cassette_dir: Path | None = None, lifecycle: Lifecycle | None | bool = None,
               live_pool_factory=ServerPool) -> dict:
    """Run every test case; record one pass/fail in the lifecycle (unless lifecycle=False). Returns the report."""
    flow = flow_or_name if isinstance(flow_or_name, dict) else load_flow(flow_or_name)
    cases = test_cases(flow)
    cdir = cassette_dir or current_cassette_dir()
    cpath = cdir / f"{flow['name']}.json"
    cassette = seed_cassette(flow, cpath)
    report = {"flow": flow["name"], "mode": mode, "cassette": str(cpath), "cases": [], "passed": bool(cases), "misses": 0}
    if not cases:
        report["error"] = "no test cases: add `tests:` or input `example`s to the flow"

    async def go():
        live = live_pool_factory() if mode != "offline" else None
        pmode = {"auto": "auto", "live": "record", "offline": "replay"}[mode]
        async with CassettePool(cassette, live=live, mode=pmode) as pool:
            for case in cases:
                try:
                    rec = await FlowRunner(pool, lifecycle=False).run(flow, case["inputs"], save=False)
                    reason = check_case(rec, flow, case["expect"])
                    summary = rec.get("summary")
                except Exception as e:  # noqa: BLE001
                    reason, summary = f"exception: {e}", None
                report["cases"].append({"inputs": case["inputs"], "passed": reason is None, "reason": reason, "summary": summary})
            report["misses"] = len(pool.misses)
        if mode != "offline" and cassette.entries:
            cassette.save(cpath)

    asyncio.run(go())
    report["passed"] = bool(cases) and all(c["passed"] for c in report["cases"])
    if lifecycle is not False and cases:      # no cases: nothing ran, nothing failed; the report carries the error
        lc = lifecycle or get_lifecycle()
        detail = "; ".join(f"{c['inputs']}: {c['reason']}" for c in report["cases"] if not c["passed"])
        st = lc.record_test(flow, report["passed"], detail)
        report["lifecycle"] = {"status": st["status"], "author_status": st["author_status"], "transition": st.get("transition")}
    return report
