"""Agent-assisted refinement of an induced draft: `mcp-explorer refine <flow> [--yes] [--budget N] [--model M] [--name NEW]`.

An induced flow is mechanically correct and unusable as a product. It is called `mined-code-git-3`, its steps are
`run_python_3`, its inputs are whatever identifier happened to appear in the traces, and the arguments the inducer
could not derive are frozen at the first value the agent used. This command asks Claude Code to look at the prompts
that led to the flow, the tool schemas and the values each recorded episode actually passed, and to PROPOSE a name,
a card, step titles, inputs and a binding for every unresolved argument.

**The agent only proposes. Every proposal is decided here, deterministically.**

  name          kebab-case and not already a flow in the workspace, else the draft's own base name is kept.
  card          `crystal.flow.cards.validate_card`: an expected output naming a step that does not exist is rejected.
  inputs        kept only when an accepted binding template actually references them; the rest are dropped and said so.
  bindings      REPLAYED AGAINST THE TRACES. For each supporting episode we take the value that episode really
                passed for (step, arg), rebuild that episode's context from its own recorded calls (its inputs, and
                the results of the calls before that step run through the flow's extracts plus any the agent
                proposed -- no server is contacted), render the proposed template in it, and require the rendered
                string to equal the recorded value. A binding is accepted only if it reproduces the recorded value
                in every episode where the argument appears; the first mismatch rejects it, naming the episode, the
                expected value and what the template rendered. A rejected binding leaves the argument exactly as the
                draft had it.
  new inputs    a value identical in every episode is a constant, not a parameter: such an input is rejected.
  unfixable     the agent's own admission that nothing derives an argument. Recorded with its reason; the argument
                is left alone. That is a better outcome than an invented template.

The result is written as a NEW version, `<name>.v<N>.yaml` (crystal.author.next_version), status draft, carrying
`refined_from: <the flow it came from>`. Nothing is ever overwritten.

Guards, in order: `--yes` or an interactive confirmation; $CRYSTAL_NO_AGENT refuses outright; `claude` must be on
PATH; and one agent run at a time per workspace (the /record page's lock). The cost is printed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from crystal import state
from crystal.extract.catalog import load_catalog
from crystal.extract.extractors import run_extractor
from crystal.flow.cards import CARD_KEYS, validate_card
from crystal.flow.runner import load_flow
from crystal.flow.templating import render
from crystal.induce.inducer import align_steps, dump_flow
from crystal.induce.mining import _is_authored
from crystal.trace.driver import INSTALL_HINT, NO_AGENT_ENV, claude_path, run_agent
from crystal.trace.store import Session, load_session

DEFAULT_BUDGET = 2
MAX_PROMPTS = 12                 # prompts sampled into the context
PROMPT_CHARS = 400               # each truncated to this
MAX_VALUES = 10                  # distinct observed values shown per unresolved argument
MAX_SCHEMA_PROPS = 24
KINDS = ("input", "derive", "constant", "unfixable")

KEBAB_RX = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
FENCE_RX = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.S)
BARE_INPUT_RX = re.compile(r"^\{\{\s*inputs\.([A-Za-z_]\w*)\s*\}\}$")
INPUT_REF_RX = re.compile(r"inputs\.([A-Za-z_]\w*)")


# ---------------------------------------------------------------- bindability of a whole flow
def flow_bindability(flow: dict) -> float | None:
    """The share of a flow's varying arguments a program derives, the measure `candidates` prints for a mined
    sequence (crystal.induce.mining.bindability) applied to a written flow: an argument that renders a template is
    derived, one the inducer reported unresolved (or that is a block of authored text) is not, and an argument that
    was identical in every episode says nothing either way. None when there is nothing to score."""
    derived = authored = unresolved = 0
    for st in flow.get("steps") or []:
        unres = st.get("unresolved") or {}
        for k, v in (st.get("args") or {}).items():
            if k in unres:
                unresolved += 1
            elif "{{" in json.dumps(v, default=str):
                derived += 1
            elif _is_authored(v):
                authored += 1
    varying = derived + unresolved + authored
    if not varying:
        return 1.0 if flow.get("steps") else None
    return derived / varying


# ---------------------------------------------------------------- context (no LLM, no server calls except tools/list)
def supporting_sessions(flow: dict, trace_dir: Path | None = None) -> tuple[list[Session], list[str]]:
    """The episodes the flow was induced from, in induction order. Returns (sessions, ids whose trace is gone)."""
    d = trace_dir or state.trace_dir()
    found, missing = [], []
    for sid in flow.get("induced_from") or []:
        p = d / f"{sid}.jsonl"
        if p.exists():
            found.append(load_session(p))
        else:
            missing.append(sid)
    return found, missing


def sample_prompts(sessions: list[Session], limit: int = MAX_PROMPTS, chars: int = PROMPT_CHARS) -> list[dict]:
    """The prompts that led to the flow: one per episode, near-identical ones folded together (same text once the
    identifiers and whitespace are normalised), at most `limit`, each truncated."""
    seen: dict[str, dict] = {}
    for s in sessions:
        text = (s.prompt or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", re.sub(r"[A-Z][A-Z0-9]+-\d+|\b\d{4,}\b|[0-9a-f]{7,40}", "#", text)).lower()[:600]
        if key in seen:
            seen[key]["episodes"].append(s.session_id)
            continue
        seen[key] = {"text": text[:chars] + ("…" if len(text) > chars else ""), "episodes": [s.session_id],
                     "inputs": s.meta.get("inputs") or {}}
    return list(seen.values())[:limit]


def tool_schemas(tools: list[str], pool=None) -> tuple[dict, list[str]]:
    """`inputSchema` per `server.tool`, from tools/list. Servers that will not start are skipped and named."""
    import asyncio

    from crystal.mcp_client import ServerPool
    servers = sorted({t.split(".", 1)[0] for t in tools if "." in t})
    out: dict[str, dict] = {}
    skipped: list[str] = []

    async def go(p):
        for server in servers:
            try:
                listed = await p.list_tools(server)
            except BaseException as e:  # noqa: BLE001  (anyio raises ExceptionGroup for a server that will not start)
                if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                skipped.append(f"{server} ({type(e).__name__})")
                continue
            for t in listed:
                name = f"{server}.{t['name']}"
                if name in tools:
                    out[name] = t.get("inputSchema") or {}

    async def main():
        if pool is not None:
            await go(pool)
            return
        async with ServerPool(only=set(servers)) as p:
            await go(p)

    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001 - the whole pool failing must not stop a refine
        skipped.append(f"all servers ({type(e).__name__})")
    return out, skipped


def _distinct(values: list) -> list:
    out = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def episode_calls(flow: dict, sessions: list[Session], catalog: dict) -> dict[str, dict[str, list[dict]]]:
    """{session id: {step id: the calls that episode made for that step}}, from the inducer's own alignment."""
    aligned = align_steps(sessions, catalog)
    per: dict[str, dict[str, list[dict]]] = {s.session_id: {} for s in sessions}
    ids = {st["id"] for st in flow.get("steps") or []}
    for sid, members in aligned.items():
        if sid not in ids:
            continue
        for m in members:
            per.setdefault(m["session"], {}).setdefault(sid, []).extend(m["calls"])
    return per


def observed_args(flow: dict, per: dict[str, dict[str, list[dict]]]) -> dict[str, dict[str, dict]]:
    """{step id: {arg: {"values": [{value, episodes}], "constant": bool}}} over the supporting episodes."""
    out: dict[str, dict[str, dict]] = {}
    for step in flow.get("steps") or []:
        sid = step["id"]
        by_arg: dict[str, dict[str, list[str]]] = {}
        for ep, steps in per.items():
            for c in steps.get(sid) or []:
                for k, v in (c.get("input") or {}).items():
                    key = json.dumps(v, sort_keys=True, default=str)
                    by_arg.setdefault(k, {}).setdefault(key, [])
                    if ep not in by_arg[k][key]:
                        by_arg[k][key].append(ep)
        out[sid] = {k: {"values": [{"value": json.loads(kv), "episodes": eps} for kv, eps in vals.items()],
                        "constant": len(vals) == 1}
                    for k, vals in by_arg.items()}
    return out


def gather(flow: dict, trace_dir: Path | None = None, catalog: dict | None = None, schemas: bool = True) -> dict:
    """Everything the agent is shown, collected without an LLM: the draft YAML, the prompts behind it, the tool
    schemas, the values each episode passed for every unresolved argument, which arguments are constant, the
    extracts each step already makes, and the flow's current bindability."""
    catalog = catalog if catalog is not None else load_catalog()
    sessions, missing = supporting_sessions(flow, trace_dir)
    per = episode_calls(flow, sessions, catalog) if sessions else {}
    obs = observed_args(flow, per) if sessions else {}
    tools = [st.get("tool") for st in flow.get("steps") or [] if st.get("tool")]
    sch, skipped = tool_schemas(tools) if schemas else ({}, ["tool schemas not gathered"])
    unresolved = []
    for step in flow.get("steps") or []:
        for arg in sorted(step.get("unresolved") or {}):
            seen = (obs.get(step["id"]) or {}).get(arg) or {"values": []}
            unresolved.append({"step": step["id"], "tool": step.get("tool"), "arg": arg,
                               "current": step.get("args", {}).get(arg),
                               "observed": [{"value": v["value"], "episodes": v["episodes"][:6]}
                                            for v in seen["values"][:MAX_VALUES]],
                               "n_values": len(seen["values"])})
    constants = {st["id"]: sorted(k for k, v in (obs.get(st["id"]) or {}).items() if v["constant"])
                 for st in flow.get("steps") or []}
    shown = {k: v for k, v in flow.items() if not k.startswith("_") and k != "tests"}
    return {"flow": flow, "flow_yaml": dump_flow(shown),
            "sessions": sessions, "episodes": [s.session_id for s in sessions], "missing_episodes": missing,
            "prompts": sample_prompts(sessions), "schemas": sch, "skipped_servers": skipped,
            "episode_inputs": [{"episode": s.session_id, "trigger": s.meta.get("trigger"),
                                "inputs": s.meta.get("inputs") or {}} for s in sessions],
            "per_episode": per, "observed": obs, "unresolved": unresolved, "constants": constants,
            "extracts": {st["id"]: list((st.get("extract") or {}).keys()) for st in flow.get("steps") or []},
            "bindability": flow_bindability(flow), "catalog": catalog}


# ---------------------------------------------------------------- the prompt
def _schema_line(name: str, schema: dict) -> str:
    props = (schema or {}).get("properties") or {}
    req = set((schema or {}).get("required") or [])
    bits = []
    for k, spec in list(props.items())[:MAX_SCHEMA_PROPS]:
        t = (spec or {}).get("type") or "any"
        bits.append(f"{k}: {t}{'*' if k in req else ''}")
    return f"  {name}({', '.join(bits)})"


TEMPLATE_EXAMPLE = """name: <kebab-case-flow-name>            # what this flow is, not how it was mined
card:
  use_case: <when a person reaches for this flow, 1-2 sentences>
  inputs_explained: {<input_name>: <one line>}
  expected_outputs:
    - {name: <evidence group>, steps: [<step_id>], description: <what a person gets>, required: true}
  not_covered: [<what this does not look at>]
  example: {inputs: {<input_name>: <value>}, found: <one sentence>}
step_titles: {<step_id>: "<human title>"}
inputs: {<input_name>: {type: string, description: <what it is>, example: <value>, required: true}}
bindings:
  - step: <step_id>
    arg: <argument name, exactly as the tool schema spells it>
    kind: input | derive | constant | unfixable
    template: "{{ inputs.case_file }}"     # for input/derive/constant: the Jinja the argument should render
    extract: {step: <earlier_step_id>, name: <name>, spec: {from: <jsonpath>, using: <extractor>, pattern: <regex>}}
    reason: "<why>"                        # REQUIRED for unfixable"""


def refine_prompt(ctx: dict) -> str:
    flow = ctx["flow"]
    lines = [
        f"A draft flow called `{flow.get('name')}` was compiled MECHANICALLY from {len(ctx['episodes'])} recorded "
        "agent episodes, with no LLM involved. It works, but it is not a product: its name says how it was mined, its "
        "steps are named after their tools, and the arguments the compiler could not derive are frozen at whatever "
        "value one episode happened to use.",
        "",
        "Your job is to PROPOSE a better name, a card, step titles, inputs, and a binding for each unresolved "
        "argument. You are proposing only: every binding you propose will be replayed against these same recorded "
        "episodes and kept ONLY if it reproduces, exactly, the value that episode really passed. An invented "
        "template is not a lucky guess, it is a rejected proposal.",
        "",
        "## The draft flow (its `tests:` block is omitted here)", "```yaml", ctx["flow_yaml"].strip()[:12000], "```", "",
    ]
    lines.append(f"## The prompts that led to it ({len(ctx['prompts'])} distinct of {len(ctx['episodes'])} episodes)")
    if ctx["prompts"]:
        for p in ctx["prompts"]:
            lines.append(f"- ({len(p['episodes'])}x, inputs {json.dumps(p['inputs'], default=str)[:120]}) "
                         f"\"{p['text']}\"".replace("\n", " "))
    else:
        lines.append("(these episodes are recorded sessions with no prompt text kept; what they were triggered on:)")
        for e in ctx["episode_inputs"][:MAX_PROMPTS]:
            lines.append(f"  {e['episode']}: trigger {e['trigger']}, inputs {json.dumps(e['inputs'], default=str)[:120]}")
    lines += ["", "This is what the flow is FOR. Name it after that, not after its tools."]

    lines += ["", "## Tool schemas (the real parameter names)"]
    for st in flow.get("steps") or []:
        tool = st.get("tool")
        if tool in ctx["schemas"]:
            lines.append(_schema_line(f"{st['id']} -> {tool}", ctx["schemas"][tool]))
        else:
            lines.append(f"  {st['id']} -> {tool} (schema unavailable)")
    if ctx["skipped_servers"]:
        lines.append(f"  (no schema for: {', '.join(ctx['skipped_servers'])}; do not guess parameter names for those steps)")

    lines += ["", "## What each step already extracts (these are the names a `derive` template may reference)"]
    for sid, names in ctx["extracts"].items():
        lines.append(f"  {sid}: {', '.join(names) if names else '(nothing)'}")

    lines += ["", f"## Unresolved arguments ({len(ctx['unresolved'])}) -- the point of this exercise"]
    if not ctx["unresolved"]:
        lines.append("(none: every argument is derived or constant. Propose the name, card, step titles and inputs only.)")
    for u in ctx["unresolved"]:
        lines.append(f"- step `{u['step']}` ({u['tool']}), argument `{u['arg']}`: {u['n_values']} distinct value(s) "
                     f"across the episodes; the draft hardcodes {json.dumps(u['current'], default=str)[:160]}")
        for v in u["observed"]:
            lines.append(f"    {json.dumps(v['value'], default=str)[:200]}   <- episodes {', '.join(v['episodes'])}")

    lines += ["", "## Arguments that are the SAME in every episode (constants, not parameters)"]
    for sid, keys in ctx["constants"].items():
        if keys:
            lines.append(f"  {sid}: {', '.join(keys)}")

    lines += ["", f"## Current bindability: "
                  f"{'unknown' if ctx['bindability'] is None else str(round(ctx['bindability'] * 100)) + '%'} "
                  "of the varying arguments are derived rather than hardcoded.", ""]

    lines += [
        "## Rules you must follow",
        "1. An input is only worth proposing if the recorded episodes ACTUALLY DIFFER in that value. A value that is "
        "identical in every episode is a constant, not a parameter, and proposing it as an input will be rejected.",
        "2. If nothing in the request or in an earlier step's result derives an argument, say `kind: unfixable` with "
        "a `reason`. Do NOT invent a template. An honest `unfixable` is kept; a wrong template is thrown away.",
        "3. `derive` templates may only reference `inputs.<name>`, `workspace.<name>`, `catalog....` or "
        "`<earlier step id>.<extract name>` (add an `extract:` if the name you need does not exist yet). The step "
        "must come EARLIER in the flow than the step you are binding.",
        "4. Use the exact argument names from the tool schemas above and the exact step ids from the draft.",
        "5. The name must be kebab-case and must not be the name of an existing flow.",
        "",
        "## Finish your message with ONE fenced ```yaml block containing exactly this mapping",
        "```yaml", TEMPLATE_EXAMPLE, "```",
        "Include only the keys you have something to say about. Do not use any MCP tool: everything you need is above.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- parsing the proposal
def parse_proposal(text: str | None) -> dict | None:
    """The last fenced ```yaml block that parses to a mapping carrying any of the proposal's keys."""
    if not text:
        return None
    keys = ("name", "card", "step_titles", "inputs", "bindings")
    for block in reversed(FENCE_RX.findall(text)):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and any(k in data for k in keys):
            return data
    return None


# ---------------------------------------------------------------- deterministic validation
def _flow_names(flow_dir: Path | None) -> set[str]:
    d = flow_dir or state.flow_dir()
    if not d.is_dir():
        return set()
    out = set()
    for p in d.glob("*.yaml"):
        stem = p.stem
        m = re.match(r"^(.*)\.v\d+$", stem)
        out.add(m.group(1) if m else stem)
        out.add(stem)
    return out


def check_name(proposed, flow: dict, flow_dir: Path | None, override: str | None = None) -> dict:
    """kebab-case, and not a flow the workspace already has. Falls back to the draft's own base name."""
    from crystal.author import base_name
    fallback = base_name(flow.get("base") or flow.get("name") or "refined-flow")
    want = override if override is not None else proposed
    if not want:
        return {"proposed": proposed, "used": fallback, "accepted": False, "reason": "no name proposed; keeping the draft's"}
    want = str(want).strip()
    if not KEBAB_RX.match(want):
        return {"proposed": want, "used": fallback, "accepted": False, "reason": "not kebab-case ([a-z0-9] words joined by '-')"}
    if want != fallback and want in _flow_names(flow_dir):
        return {"proposed": want, "used": fallback, "accepted": False, "reason": f"a flow called {want!r} already exists in this workspace"}
    return {"proposed": want, "used": want, "accepted": True, "reason": "--name override" if override else ""}


def check_card(proposed, flow: dict) -> dict:
    """The agent's card is kept whole or not at all: an expected output naming a step that does not exist means the
    agent was describing a flow that is not this one."""
    if not isinstance(proposed, dict) or not any(k in proposed for k in CARD_KEYS):
        return {"accepted": False, "reason": "no card proposed; keeping the inducer's skeleton", "card": None}
    problems = validate_card(flow, proposed)
    if problems:
        return {"accepted": False, "reason": "; ".join(problems), "card": None}
    card = {k: proposed[k] for k in CARD_KEYS if k in proposed}
    card["authored_by"] = "agent"
    return {"accepted": True, "reason": "", "card": card}


def check_titles(proposed, flow: dict) -> list[dict]:
    ids = {st["id"] for st in flow.get("steps") or []}
    out = []
    for sid, title in (proposed or {}).items() if isinstance(proposed, dict) else []:
        if sid not in ids:
            out.append({"step": sid, "title": str(title), "accepted": False, "reason": "no such step"})
        elif not str(title).strip():
            out.append({"step": sid, "title": "", "accepted": False, "reason": "empty title"})
        else:
            out.append({"step": sid, "title": str(title).strip(), "accepted": True, "reason": ""})
    return out


def _recorded(per_ep: dict, step_id: str, arg: str) -> list:
    """The values one episode passed for (step, arg), one per recorded call that carried the argument."""
    return [c["input"][arg] for c in (per_ep.get(step_id) or []) if arg in (c.get("input") or {})]


def _extracts(specs: dict, result, catalog: dict) -> dict:
    out = {}
    for name, spec in (specs or {}).items():
        try:
            out[name] = run_extractor(spec, result, catalog)
        except Exception:  # noqa: BLE001 - an extractor that cannot run yields nothing, exactly as at runtime
            out[name] = None
    return out


def episode_contexts(flow: dict, session: Session, per_ep: dict, catalog: dict,
                     extra_extracts: dict, extra_inputs: dict) -> dict[str, dict]:
    """{step id: the context as it stood just before that step ran}, rebuilt from the episode's OWN recorded calls
    the way crystal.flow.runner builds it: no server is contacted. `extra_extracts` are the extracts the agent
    proposed, applied on top of the flow's own."""
    ctx: dict = {"inputs": {**(session.meta.get("inputs") or {}), **extra_inputs},
                 "catalog": catalog, "workspace": session.meta.get("workspace_meta") or {}}
    snaps: dict[str, dict] = {}
    for step in flow.get("steps") or []:
        sid = step["id"]
        snaps[sid] = dict(ctx)
        calls = per_ep.get(sid) or []
        if not calls:
            continue
        specs = {**(step.get("extract") or {}), **(extra_extracts.get(sid) or {})}
        if step.get("forEach") and len(calls) > 1:
            merged: dict[str, list] = {}
            for c in calls:
                for k, v in _extracts(specs, c.get("output"), catalog).items():
                    merged.setdefault(k, [])
                    for x in (v if isinstance(v, list) else [v]):
                        if x is not None and x not in merged[k]:
                            merged[k].append(x)
            ctx[sid] = {"items": [{"result": c.get("output")} for c in calls],
                        "results": [c.get("output") for c in calls], **merged}
        else:
            c = calls[-1]     # a ladder's winning rung is the last call the agent made for that step
            ctx[sid] = {"result": c.get("output"), "args": c.get("input") or {},
                        **_extracts(specs, c.get("output"), catalog)}
    return snaps


def _binding_key(b: dict) -> tuple[str, str]:
    return str(b.get("step") or ""), str(b.get("arg") or "")


def _shape_bindings(proposal: dict, flow: dict) -> tuple[list[dict], list[dict]]:
    """Split the proposal's bindings into well-formed ones and ones rejected on their shape alone."""
    ids = {st["id"] for st in flow.get("steps") or []}
    order = {st["id"]: i for i, st in enumerate(flow.get("steps") or [])}
    ok, bad = [], []
    raw = proposal.get("bindings")
    for b in raw if isinstance(raw, list) else []:
        if not isinstance(b, dict):
            bad.append({"step": "?", "arg": "?", "kind": "?", "template": None, "accepted": False,
                        "reason": "not a mapping"})
            continue
        step, arg = _binding_key(b)
        rec = {"step": step, "arg": arg, "kind": str(b.get("kind") or "").strip(),
               "template": b.get("template"), "reason_given": b.get("reason"), "extract": b.get("extract"),
               "accepted": False, "reason": "", "episodes": 0, "checked": 0}
        if step not in ids:
            rec["reason"] = f"no step {step!r} in the flow"
        elif not arg:
            rec["reason"] = "no argument named"
        elif rec["kind"] not in KINDS:
            rec["reason"] = f"kind must be one of {', '.join(KINDS)}"
        elif rec["kind"] == "unfixable":
            if not str(rec["reason_given"] or "").strip():
                rec["reason"] = "unfixable without a reason"
            else:
                ok.append(rec)
                continue
        elif rec["template"] is None:
            rec["reason"] = f"kind {rec['kind']} needs a template"
        else:
            ex = rec["extract"]
            if ex is not None:
                if not isinstance(ex, dict) or not ex.get("step") or not ex.get("name") or not isinstance(ex.get("spec"), dict):
                    rec["reason"] = "extract must be {step, name, spec}"
                elif ex["step"] not in ids:
                    rec["reason"] = f"extract names unknown step {ex['step']!r}"
                elif order[ex["step"]] >= order[step]:
                    rec["reason"] = f"extract step {ex['step']!r} does not run before {step!r}"
            if not rec["reason"]:
                ok.append(rec)
                continue
        bad.append(rec)
    return ok, bad


def validate_bindings(flow: dict, proposal: dict, ctx: dict) -> tuple[list[dict], dict[str, dict]]:
    """Replay every well-formed binding against the recorded episodes. Returns (binding records, seeded new inputs).

    A new input has no value in the episodes (it did not exist when they were recorded), so the argument whose
    binding is the bare `{{ inputs.<name> }}` DEFINES it: that episode's recorded value for that argument is the
    value the input would have had. Every other use of the input is then a real check. An input whose defining
    values are identical in every episode is rejected: it is a constant, not a parameter.
    """
    bindings, rejected = _shape_bindings(proposal, flow)
    sessions = ctx["sessions"]
    per = ctx["per_episode"]
    catalog = ctx["catalog"]
    known_inputs = set((flow.get("inputs") or {}).keys())

    # 1. which binding defines which new input, and the per-episode value that gives it
    defines: dict[str, dict] = {}
    for b in bindings:
        if b["kind"] != "input" or not isinstance(b["template"], str):
            continue
        m = BARE_INPUT_RX.match(b["template"].strip())
        if m and m.group(1) not in known_inputs and m.group(1) not in defines:
            defines[m.group(1)] = b
    seeds: dict[str, dict[str, str]] = {s.session_id: {} for s in sessions}
    input_notes: dict[str, str] = {}
    for name, b in list(defines.items()):
        vals = {}
        for s in sessions:
            got = _distinct(_recorded(per.get(s.session_id, {}), b["step"], b["arg"]))
            if len(got) == 1:
                vals[s.session_id] = str(got[0])
        if not vals:
            input_notes[name] = f"no episode passed `{b['arg']}` on step `{b['step']}`, so nothing defines this input"
            defines.pop(name)
            continue
        if len(set(vals.values())) < 2:
            input_notes[name] = ("every episode passed the same value "
                                 f"({json.dumps(next(iter(vals.values())))[:60]}): that is a constant, not a parameter")
            defines.pop(name)
            continue
        for sid, v in vals.items():
            seeds[sid][name] = v

    # 2. the extracts the agent proposed, applied when rebuilding each episode's context
    extra_extracts: dict[str, dict] = {}
    for b in bindings:
        ex = b.get("extract")
        if isinstance(ex, dict) and ex.get("step"):
            extra_extracts.setdefault(ex["step"], {})[str(ex["name"])] = ex["spec"]

    # 3. per-episode contexts, then the replay
    snaps = {s.session_id: episode_contexts(flow, s, per.get(s.session_id, {}), catalog, extra_extracts,
                                            seeds.get(s.session_id, {})) for s in sessions}
    for b in bindings:
        if b["kind"] == "unfixable":
            b.update(accepted=False, reason=f"agent says unfixable: {str(b['reason_given']).strip()}", unfixable=True)
            continue
        for name in set(INPUT_REF_RX.findall(str(b["template"]))) - known_inputs:
            if name in input_notes:
                b["reason"] = f"input {name!r} rejected: {input_notes[name]}"
        if b["reason"]:
            continue
        if b["kind"] == "constant" and "{{" in str(b["template"]):
            b["reason"] = "kind constant but the template is not a literal"
            continue
        checked = passed = 0
        for s in sessions:
            values = _recorded(per.get(s.session_id, {}), b["step"], b["arg"])
            if not values:
                continue
            checked += 1
            try:
                got = render(b["template"], snaps[s.session_id][b["step"]])
            except Exception as e:  # noqa: BLE001 - a template that will not render is simply rejected
                b["reason"] = f"template does not render ({type(e).__name__}: {e})"
                break
            bad = next((v for v in values if str(got) != str(v)), None)
            if bad is not None or len(_distinct(values)) > 1:
                want = json.dumps(bad if bad is not None else values[0], default=str)[:120]
                extra = f" (that episode passed {len(_distinct(values))} different values for this argument)" if len(_distinct(values)) > 1 else ""
                b["reason"] = f"episode {s.session_id}: expected {want}, template rendered {json.dumps(str(got))[:120]}{extra}"
                break
            passed += 1
        b["checked"], b["episodes"] = checked, passed
        if b["reason"]:
            continue
        if not checked:
            b["reason"] = "no episode passed this argument on this step, so nothing can confirm the template"
            continue
        current = next((st.get("args", {}).get(b["arg"]) for st in flow.get("steps") or [] if st["id"] == b["step"]), None)
        same = str(current) == str(b["template"])
        b.update(accepted=True, changes=not same,
                 reason=f"reproduces the recorded value in {passed}/{checked} episode(s)"
                        + ("; the draft already had this value" if same else ""))

    return bindings + rejected, {"defines": defines, "notes": input_notes}


def check_inputs(proposed, flow: dict, bindings: list[dict], input_info: dict) -> list[dict]:
    """A proposed input is kept only when an ACCEPTED binding's template references it. Everything else is dropped
    and said so (an input nothing uses is a field on a form that changes nothing)."""
    out = []
    referenced_ok: set[str] = set()
    referenced_any: set[str] = set()
    for b in bindings:
        names = set(INPUT_REF_RX.findall(str(b.get("template") or "")))
        referenced_any |= names
        if b.get("accepted"):
            referenced_ok |= names
    for name, spec in (proposed or {}).items() if isinstance(proposed, dict) else []:
        rec = {"name": name, "spec": spec if isinstance(spec, dict) else {}, "accepted": False, "reason": ""}
        if name in (flow.get("inputs") or {}):
            rec["reason"] = "the flow already has this input"
        elif name in input_info["notes"]:
            rec["reason"] = input_info["notes"][name]
        elif name in referenced_ok:
            rec.update(accepted=True, reason="referenced by an accepted binding")
        elif name in referenced_any:
            rec["reason"] = "every binding that referenced it was rejected"
        else:
            rec["reason"] = "no binding template references it"
        out.append(rec)
    return out


# ---------------------------------------------------------------- writing the new version
def _typed(template, current):
    """A constant proposal keeps the argument's original type when it had one (`limit: 20`, not `"20"`)."""
    if isinstance(current, bool) or not isinstance(current, (int, float)) or not isinstance(template, str):
        return template
    try:
        return type(current)(template)
    except ValueError:
        return template


def apply_proposal(flow: dict, name: str, report: dict) -> dict:
    """The refined flow: a deep copy of the draft with the accepted bindings applied, the accepted inputs added, the
    accepted titles set and the accepted card attached. Nothing rejected leaves a trace in the steps."""
    new = json.loads(json.dumps({k: v for k, v in flow.items() if not k.startswith("_")}, default=str))
    steps = {st["id"]: st for st in new.get("steps") or []}
    for b in report["bindings"]:
        if not b.get("accepted"):
            continue
        st = steps[b["step"]]
        st.setdefault("args", {})[b["arg"]] = _typed(b["template"], st.get("args", {}).get(b["arg"]))
        if st.get("unresolved"):
            st["unresolved"].pop(b["arg"], None)
            if not st["unresolved"]:
                st.pop("unresolved")
        ex = b.get("extract")
        if isinstance(ex, dict) and ex.get("step") in steps:
            steps[ex["step"]].setdefault("extract", {})[str(ex["name"])] = ex["spec"]
    for t in report["step_titles"]:
        if t["accepted"]:
            steps[t["step"]]["title"] = t["title"]
    for i in report["inputs"]:
        if not i["accepted"]:
            continue
        spec = i["spec"]
        new.setdefault("inputs", {})[i["name"]] = {
            "type": str(spec.get("type") or "string"), "required": bool(spec.get("required", True)),
            **({"label": str(spec["description"])} if spec.get("description") else {}),
            **({"example": spec["example"]} if spec.get("example") not in (None, "") else {})}
    if report["card"]["accepted"]:
        new["card"] = report["card"]["card"]
    new["name"] = name
    new["title"] = name.replace("-", " ")
    new["status"] = "draft"
    new["refined_from"] = flow.get("name")
    return new


def write_version(flow: dict, name: str, report: dict, flow_dir: Path | None = None, note: dict | None = None) -> tuple[Path, dict]:
    from crystal.author import next_version
    out, n = next_version(name, flow_dir)
    new = apply_proposal(flow, name, report)
    new["version"] = n
    new["base"] = name
    new["refined"] = {"at": datetime.now(timezone.utc).isoformat(), "from": flow.get("name"),
                      "accepted": [f"{b['step']}.{b['arg']}" for b in report["bindings"] if b.get("accepted")],
                      "rejected": [f"{b['step']}.{b['arg']}" for b in report["bindings"] if not b.get("accepted")],
                      "unfixable": {f"{b['step']}.{b['arg']}": str(b.get("reason_given") or "")
                                    for b in report["bindings"] if b.get("unfixable")},
                      **(note or {})}
    out.write_text(dump_flow(new))
    return out, new


# ---------------------------------------------------------------- guards
@contextmanager
def _one_at_a_time(workspace, take: bool = True):
    """The /record page's lock, so a refine and a recording never run at once in one workspace. `take=False` when
    the caller already holds it (the UI takes it when it creates the job). If the web app is not importable the
    refine still runs, unlocked."""
    if not take:
        yield None
        return
    try:
        from crystal.app.routes_record import Busy, acquire_lock, release_lock
    except Exception:  # noqa: BLE001
        yield None
        return
    job = "refine-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    try:
        acquire_lock(workspace, job)
    except Busy as e:
        raise RuntimeError(f"a run is already in progress in this workspace (job {e.job}); one at a time") from None
    try:
        yield job
    finally:
        release_lock(workspace, job)


def preflight(driver_fn=run_agent) -> None:
    """Refuse before anything is spawned. Only applies to the real driver: the tests pass a fake one."""
    if driver_fn is not run_agent:
        return
    if os.environ.get(NO_AGENT_ENV, "").strip():
        raise RuntimeError(f"agent launches disabled ({NO_AGENT_ENV} is set); `refine` needs the agent")
    if not claude_path():
        raise RuntimeError(INSTALL_HINT)


# ---------------------------------------------------------------- the command
def refine(flow_name: str, budget=DEFAULT_BUDGET, model: str | None = None, name: str | None = None,
           driver_fn=run_agent, flow_dir: Path | None = None, trace_dir: Path | None = None,
           catalog: dict | None = None, schemas: bool = True, workspace=None, write: bool = True,
           lock: bool = True) -> dict:
    """Gather the context, ask the agent for a proposal, decide every part of it here, write `<name>.v<N>.yaml`."""
    preflight(driver_fn)
    flow = load_flow(flow_name, flow_dir)
    ctx = gather(flow, trace_dir, catalog, schemas=schemas)
    if not ctx["sessions"]:
        raise RuntimeError(f"{flow.get('name')} has no supporting episodes in the trace dir "
                           f"(induced_from: {', '.join(flow.get('induced_from') or []) or 'nothing'}); "
                           "nothing could be replayed, so nothing can be validated")
    prompt = refine_prompt(ctx)
    trigger = "refine"      # never the flow's own trigger: a refine session must not be induced into that flow
    from crystal import workspace as ws_mod
    ws = ws_mod.workspace(workspace) if workspace is not None else ws_mod.current()
    with _one_at_a_time(ws, take=lock):
        info = driver_fn(trigger, {"flow": flow.get("name")}, prompt, budget=budget, model=model,
                         meta={"command": "refine", "flow": flow.get("name")})
    proposal = parse_proposal(info.get("result")) or {}
    report = decide(flow, proposal, ctx, flow_dir=flow_dir, override_name=name)
    report.update(agent=info, cost_usd=info.get("cost_usd"), session_id=info.get("session_id"),
                  proposal_found=bool(proposal), episodes=ctx["episodes"], missing_episodes=ctx["missing_episodes"])
    if write:
        path, new = write_version(flow, report["name"]["used"], report, flow_dir,
                                  note={"session": info.get("session_id"), "cost_usd": info.get("cost_usd")})
        report.update(path=str(path), flow=new)
        report["bindability"]["after"] = flow_bindability(new)
    return report


def decide(flow: dict, proposal: dict, ctx: dict, flow_dir: Path | None = None, override_name: str | None = None) -> dict:
    """Every deterministic decision, in one place: name, card, titles, bindings (replayed), inputs."""
    bindings, input_info = validate_bindings(flow, proposal, ctx)
    report = {"name": check_name(proposal.get("name"), flow, flow_dir, override_name),
              "card": check_card(proposal.get("card"), flow),
              "step_titles": check_titles(proposal.get("step_titles"), flow),
              "bindings": bindings,
              "inputs": check_inputs(proposal.get("inputs"), flow, bindings, input_info),
              "bindability": {"before": flow_bindability(flow), "after": None}}
    return report


# ---------------------------------------------------------------- output
def _pct(v) -> str:
    return "-" if v is None else f"{round(v * 100)}%"


def format_report(report: dict, flow_name: str) -> str:
    rows: list[tuple[str, str, str, str]] = []
    n = report["name"]
    rows.append(("name", str(n.get("proposed") or "(none)"), "accepted" if n["accepted"] else "rejected",
                 n["reason"] or f"using {n['used']}"))
    c = report["card"]
    rows.append(("card", "agent prose", "accepted" if c["accepted"] else "rejected", c["reason"] or ""))
    for t in report["step_titles"]:
        rows.append((f"title {t['step']}", t["title"][:40], "accepted" if t["accepted"] else "rejected", t["reason"]))
    for i in report["inputs"]:
        rows.append((f"input {i['name']}", str(i["spec"].get("type") or "string"),
                     "accepted" if i["accepted"] else "dropped", i["reason"]))
    for b in report["bindings"]:
        what = f"bind {b['step']}.{b['arg']}"
        verdict = "accepted" if b.get("accepted") else ("unfixable" if b.get("unfixable") else "rejected")
        rows.append((what, str(b.get("template") or "")[:40], verdict, b.get("reason", "")))
    w = max([len(r[0]) for r in rows] + [10])
    out = [f"{'proposal':{w}}  {'value':40}  {'verdict':9}  why",
           f"{'-' * w}  {'-' * 40}  {'-' * 9}  {'-' * 40}"]
    for a, b, c_, d in rows:
        out.append(f"{a:{w}}  {b:40}  {c_:9}  {d[:100]}")
    bd = report["bindability"]
    out.append("")
    out.append(f"bindability: {_pct(bd['before'])} -> {_pct(bd['after'])}   (share of varying arguments a program derives)")
    if report.get("path"):
        out.append(f"wrote {report['path']}  (status draft, refined_from: {flow_name})")
    out.append(f"COST: ${report.get('cost_usd') or 0:.4f}")
    return "\n".join(out)


def refine_main(args: list[str]) -> int:
    from crystal.author import _opt, confirm, positionals
    pos = positionals(args)
    if not pos:
        print("usage: refine <flow> [--yes] [--budget 2] [--model m] [--name new-flow-name]")
        return 1
    flow_name = pos[0]
    try:
        flow = load_flow(flow_name)
    except Exception as e:  # noqa: BLE001
        print(f"no flow {flow_name!r} in {state.flow_dir()} ({type(e).__name__})")
        return 1
    try:
        preflight()
    except RuntimeError as e:
        print(e)
        return 1
    n_unres = sum(len(st.get("unresolved") or {}) for st in flow.get("steps") or [])
    budget = _opt(args, "--budget", str(DEFAULT_BUDGET))
    print(f"refine {flow.get('name')} ({len(flow.get('steps') or [])} steps, {n_unres} unresolved arguments, "
          f"{len(flow.get('induced_from') or [])} supporting episodes, bindability {_pct(flow_bindability(flow))}); budget ${budget}")
    print("The agent proposes a name, a card, step titles, inputs and argument bindings; every binding is replayed "
          "against the recorded episodes here and kept only if it reproduces what they actually sent.")
    if not confirm(args, f"mcp-explorer refine {flow_name}"):
        return 2
    try:
        report = refine(flow_name, budget=budget, model=_opt(args, "--model"), name=_opt(args, "--name"))
    except RuntimeError as e:
        print("error:", e)
        return 1
    if not report["proposal_found"]:
        print("warning: the agent's message had no parseable ```yaml proposal block; nothing was proposed.",
              file=sys.stderr)
    print()
    print(format_report(report, flow.get("name")))
    return 0
