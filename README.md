# mcp_explorer

A web frontend over MCP servers that runs the investigation flows an AI agent would run, **with no AI at
runtime**. An agent is used only during *crystallization*: it explores, its tool-call traces are recorded,
and the recurring flows are compiled into ordinary programs. The UI lets you pick a crystallized flow,
enter starting parameters, and see the evidence.

Design: `docs/PLAN.md`. Research behind it: `docs/research/`.

## Layout

| Path | What |
|---|---|
| `sim/` | Synthetic corporate world (`sim/world.py`) and simulated MCP servers for jira, slack, confluence, chronosphere, logz, pagerduty, git, code; `sim/servers/flows.py` exposes the crystallized flows as MCP tools for the authoring agent |
| `crystal/trace/` | Trace recorder: Claude Code `PostToolUse` hook, scripted agent, trace store, headless-agent driver |
| `crystal/extract/` | Typed ID regex catalog, entity catalog + gazetteer, extractor specs |
| `crystal/flow/` | Flow schema (YAML), Jinja templating, interpreter with fan-out and precision ladders; `lifecycle.py` promotion state + circuit breaker |
| `crystal/induce/` | Trace → flow inducer (no LLM) |
| `crystal/replay/` | Cassette record/replay so regression tests need no servers; `regression.py` runs a flow's test cases |
| `crystal/author.py` | Agent-assisted authoring and repair (`crystal author`, `crystal repair`); the only code that launches the agent |
| `app/` | FastAPI web UI: flow catalog, run dossier (`app/dossier.py`: headline, evidence by type, highlights; `app/diagram.py`: research-flow SVG), the same dossier for recorded agent traces at `/traces` |
| `flows/` | Crystallized flows: `investigate-jira-ticket`, `investigate-slack-thread`, `investigate-slack-dm` (candidates), `<name>.v<N>.yaml` versions induced by `crystal author`, and the raw `induced-*` drafts |
| `traces/` | Recorded sessions (JSONL), `runs/` run records referenced by recorded agent sessions, cassettes (gitignored); `feedback.jsonl` is the repair queue |
| `state/` | `lifecycle.sqlite`, the per-flow runtime state (gitignored) |

## Quick start

```bash
uv sync
uv run python sim/world.py            # generate sim/data/world.json and the sim git repo
uv run python sim/build_catalog.py    # catalog/entities.yaml (the foreign-key hub)
uv run python -m crystal.cli flows
uv run python -m crystal.cli run investigate-jira-ticket key=PAY-101
uv run python -m crystal.cli run investigate-slack-thread channel_id=C542575C5 thread_ts=1786015740.000000
uv run python -m crystal.cli run investigate-slack-dm "text=is payments healthy? a customer says card charges are hanging"
uv run python -m crystal.cli status      # promotion state of every flow
uv run uvicorn app.main:app --port 8765   # then open http://localhost:8765
uv run pytest -q
```

Commands (`uv run python -m crystal.cli <command>`): `flows`, `run`, `induce`, `test`, `status`, `author`, `repair`,
`tools`, `mcp-config`. Only `author`, `repair` and `python -m crystal.trace.driver` launch Claude Code; nothing else
ever calls an LLM.

## Crystallization loop

1. **Record.** Traces come from either the scripted agent (free) or Claude Code with the project hook
   (`.claude/settings.json` + `.mcp.json`; `claude` in this directory records every MCP call to `traces/`):
   ```bash
   uv run python -m crystal.trace.scripted PAY-101 STF-109 SUP-102 PLAT-102 PAY-102 --variants 3
   uv run python -m crystal.trace.scripted --trigger slack_thread C542575C5/1786015740.000000 --variants 3
   uv run python -m crystal.trace.scripted --trigger slack_dm D59227FD8/1786020840.000500 --variants 3
   ```
   The real agent is launched only by an explicit command and costs money (`--budget` caps it; the driver prints
   the cost at the end):
   ```bash
   uv run python -m crystal.trace.driver jira_issue key=PAY-108 --budget 3
   uv run python -m crystal.trace.driver slack_thread channel_id=C542575C5 thread_ts=1787484120.000005 --budget 3
   uv run python -m crystal.trace.driver slack_dm "text=hey, are you seeing checkout failures?" --budget 3
   ```
   The hook parses MCP content-block results into JSON when it records them, and the trace store does the same for
   older traces, so real and scripted sessions have the same shape.
2. **Induce.** Compile the traces into a draft flow, no LLM:
   ```bash
   uv run python -m crystal.cli induce jira_issue   --name induced-jira-ticket-all
   uv run python -m crystal.cli induce slack_thread --name induced-slack-thread
   uv run python -m crystal.cli induce slack_dm     --name induced-slack-dm
   ```
   Sessions recorded by `crystal author`/`repair` ran an existing flow through the `flows` MCP server; `induce`
   expands each `run_flow` call into the calls that flow made, from the run record archived in `traces/runs/`
   (tracked, unlike `runs/`), so those sessions merge with the others instead of adding a `flows.run_flow` step.
   The report lists unresolved bindings (values that differ across sessions with no explanation), optional
   steps, ladders, fan-outs, how each session's steps were aligned, the window alternatives that lost the
   majority vote, the bindings solved by learned position programs (each gated leave-one-out: a program learned
   from the other examples must reproduce every held-out span), the literal rungs dropped (a value one session
   used that nothing explains never becomes a ladder rung) and the reference cycles cut (sessions that did two
   things in opposite orders: the fallback rung or fan-out term that pointed forward is removed). Steps align across sessions by a
   signature of what their bound arguments reference (tool + text/id/window classes), so an agent that runs
   the same tools in a different order still merges; timestamp arguments never become ladders. Every draft
   carries `tests:` (one case per traced input, `min_hits: 1` on each step that had hits in every session), so
   `crystal test` on a draft checks that it still finds what the agent found.
   Sessions the hook records are only those that touch an MCP server; Claude Code's own Read/Grep/Glob results
   are kept as short previews inside such a session and never on their own (a review session in this checkout
   leaves no trace).
   `flows/induced-jira-ticket-all.yaml` is the merge of the 15 scripted sessions with the two real Claude Code
   sessions (one exploratory, one `crystal author` run); `induced-slack-thread` / `induced-slack-dm` merge 15 / 18
   scripted sessions with one real session each. The hand-fixed candidates are `investigate-*.yaml`.
3. **Test.** `tests/test_inducer.py` runs the induced flow on a ticket that was never traced;
   `tests/test_flow_jira.py` replays the hand-written flow through a cassette; `tests/test_flows_slack.py` runs the
   thread and DM flows on a thread/DMs that were never traced.
4. **Run.** Promote by setting `status: promoted` in the flow YAML (the author's intent). The runtime keeps its
   own *effective* state per flow in `state/lifecycle.sqlite` (gitignored) with a circuit breaker: a run with a step
   error, a required step with zero hits, a failed regression test or a "this didn't help" from the UI demotes the
   flow one level (promoted → candidate → draft); it climbs back after N consecutive clean live runs
   (`candidate_after: 2` and `promote_after: 5` in the YAML, per flow), never above the author's intent. A passing
   regression test is recorded but never counts toward re-promotion (it replays the same responses every time), and
   the authoring agent's `run_flow` runs are saved but never counted either way. The UI lists every flow, drafts
   included, with both badges plus counters and the last failure; if the lifecycle store is locked the run is still
   saved (with `lifecycle: {error}`).
   ```bash
   uv run python -m crystal.cli status                       # table: author intent, effective state, counters
   uv run python -m crystal.cli test investigate-jira-ticket  # regression via cassette (live for misses); recorded
   ```
   `crystal test` runs the flow's `tests:` cases (or one built from the inputs' `example`s, which must then find
   something: a run where every step returns zero hits fails) through `traces/cassettes/<flow>.json`, seeded from
   the sessions the flow was induced from; `--live` re-records, `--offline` never starts a server. A flow with no
   cases at all is an error, not a failure (the lifecycle is untouched).
5. **Author / repair (the only commands that launch the agent; each costs money, capped with `--budget`).**
   ```bash
   uv run python -m crystal.cli author jira_issue key=PAY-108 --yes --budget 3
   uv run python -m crystal.cli repair            # list the queue; then: repair --all --yes  |  repair <run_id> --yes
   ```
   `author` runs Claude Code headless with the existing flows listed (ordered by the runtime's effective status, so
   a flow the circuit breaker tripped is never the one to run first) and the instruction to run the best one FIRST
   through the `flows` MCP server (`sim/servers/flows.py`: `list_flows`, `run_flow(name, inputs_json)`, which executes
   the interpreter and returns a compact evidence summary), then explore with the raw tools only for what the flow
   lacked. The recorded trace reads "ran flow X, then did Y"; the run record is copied to `traces/runs/`, the
   `run_flow` call is expanded into the calls the flow made, and the inducer compiles that session plus every earlier session of the trigger
   into `flows/<base>.v<N>.yaml` (`status: draft`, `base`, `authored:` provenance). Nothing is overwritten.
   `repair` does the same for each unhelpful run queued in `traces/feedback.jsonl`, handing the agent the flow YAML,
   the inputs, a compact evidence summary and the complaint; once a version exists it appends a `handled` record
   (never deletes). A failed agent run (error, budget, no tool calls) leaves the complaint pending. A `run_flow`
   call whose run record is missing (or that errored) is dropped from the session with a warning on stderr.
   `flows/investigate-jira-ticket.v2.yaml` is that author session's version, re-induced with the current inducer.
   Without `--yes` both commands ask for confirmation on a terminal and refuse when non-interactive.

## Flow YAML in one screen

```yaml
steps:
  - id: issue
    tool: jira.jira_get_issue
    args: { issue_key: "{{ inputs.key }}" }
    extract:
      service:   { from: "fields.components[*].name", using: "catalog:service" }
      error_sig: { from: fields.description, using: regex, pattern: '^Error:\s*(.+)$' }
      window:    { from: fields.created, using: window, before: 1h, after: 6h }
  - id: slack
    tool: slack.conversations_search_messages
    hits: messages.matches
    args:
      search_query:
        ladder:                      # first rung with hits wins; blank rungs are skipped
          - '"{{ issue.error_sig }}" in:#{{ catalog.service[issue.service].incident_channel }} after:{{ issue.window.start_date }}'
          - '{{ inputs.key }}'
  - id: logs
    tool: logz.search_logs
    forEach: (issue.trace_ids + thread.trace_ids) | unique | list    # fan out instead of choosing
    args: { query: "trace_id:{{ item }}", from_time: "{{ issue.window.start }}", to_time: "{{ issue.window.end }}" }
```

Extractors: `jsonpath` (default), `regex`, `ids:<type>` (typed ID catalog), `catalog:<kind>` (gazetteer over
`catalog/entities.yaml`), `window` (timestamp ± durations; `round: 1h` floors the anchor first, for agents that
use 12:00Z instead of created−1h), `position` (a span program learned from traces, `crystal/extract/positions.py`:
start and end positions given as the k-th place where a left-context and a right-context token regex meet, e.g.
`{start: {left: ['lit:on', WS], right: [], k: 1}, end: {left: [], right: [WS, 'lit:at'], k: 1}}`). `all: true`
returns every match.

## Pointing at real servers

Replace entries in `servers.yaml` with the real MCP servers (the sim tools copy the public servers' tool
names and parameter shapes) and rebuild `catalog/entities.yaml` from their data. Flows reference tools as
`server.tool`, so nothing else changes.
