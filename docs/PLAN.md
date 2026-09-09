# mcp_explorer: plan

_Written 2026-09-09. Decisions taken with the project owner are in the last section._

## Goal

A web frontend over a company's MCP servers that runs the investigation flows an AI agent would
normally run, with no AI at runtime. An agent is used only during **crystallization**: it explores,
its tool-call traces are recorded, and the recurring flows are compiled into ordinary programs.
The UI lets a user pick a crystallized flow, enter starting parameters, and see the evidence the
flow gathered.

The model is Microsoft's Progressive Crystallization (arXiv 2607.07052) plus the tool-making
paper's repair loop (arXiv 2607.08010), with two additions: the agent reaches for existing flows
before exploring, and a user's "this didn't help" signal can launch the agent to repair a flow.
Background research lives in `docs/research/`.

## Three actors

| Actor | Uses AI | Role |
|---|---|---|
| **Flow runner** (UI + interpreter) | no | Runs a crystallized flow end to end against MCP servers, renders an evidence view. |
| **Flow author** (agent, offline) | yes, bounded | Invoked on a miss or a complaint. Tries existing flows first; explores only for what they lack; writes or updates a flow. |
| **Test suite** | no | Generated from the author's traces. Replays each flow against recorded responses and decides whether a flow may appear in the UI. |

## Architecture

```
sim/            simulated corporate MCP servers over one synthetic, cross-linked incident world
crystal/        the library
  trace/        hook that records every MCP call; trace store
  extract/      typed ID regex catalog, gazetteer over the entity catalog, span synthesis from traces
  flow/         flow schema (YAML), interpreter (templates, extract, forEach, precision ladders)
  induce/       traces -> draft flow (deterministic binding search); agent-assisted authoring wrapper
  replay/       cassette record/replay for MCP responses; flow regression tests
app/            FastAPI + plain HTML/JS: flow catalog, parameter form, run view, run history
flows/          crystallized flows (YAML), each with status: draft | candidate | promoted
traces/         recorded agent runs (JSONL) and cassettes
```

### Flow schema (the crystallized artefact)

```yaml
name: investigate-jira-ticket
status: promoted
trigger: { type: jira_issue }
inputs:
  key: { type: jira_key, required: true }
steps:
  - id: issue
    tool: jira.get_issue
    args: { key: "{{ inputs.key }}" }
    extract:
      service:   { from: fields.description, using: catalog:service }
      error_sig: { from: fields.description, using: regex, pattern: '(?m)^(?:Error|Exception):\s*(.+)$' }
      window:    { from: fields.created, using: window, before: 1h, after: 4h }
  - id: slack
    tool: slack.search_messages
    args:
      query: { ladder: [ '"{{ issue.error_sig }}" in:#{{ catalog.service[issue.service].slack_channel }}',
                         '{{ issue.error_sig | tokens | and }}',
                         '{{ inputs.key }}' ] }
    extract:
      trace_ids: { from: matches[*].text, using: regex, pattern: 'trace[_-]?id[=: ]+([0-9a-f]{16,32})', all: true }
  - id: logs
    forEach: slack.trace_ids
    tool: logz.search_logs
    args: { query: 'trace_id:{{ item }}', from: "{{ issue.window.start }}", to: "{{ issue.window.end }}" }
  - id: code
    tool: code.grep
    args: { pattern: "{{ issue.error_sig }}" }
  - id: metrics
    tool: chronosphere.query_prometheus_range
    args: { query: 'rate(http_requests_total{service="{{ issue.service }}",code=~"5.."}[5m])',
            start: "{{ issue.window.start }}", end: "{{ issue.window.end }}" }
```

Principles baked into the schema:
- **Every fuzzy decision resolves at crystallization time.** Tool order is fixed; values are bound by
  typed extractors; free-text search is a precision ladder the runner descends until it gets hits.
- **Fan out instead of choosing.** Where the agent picked one candidate, the flow runs all of them.
- **Output is an evidence view, not prose.** Each step's results are shown with the query that
  produced them.
- Names follow Arazzo (`inputs`, step ids, `outputs`) so files stay convertible.

### Crystallization pipeline

1. **Record.** A Claude Code `PostToolUse` hook (matcher `mcp__.*`) appends every call to
   `traces/<session>.jsonl`: tool, input, response, timestamps. Raw responses double as cassettes.
2. **Induce.** Group traces by trigger. For every argument of every call, search prior results for
   the value (exact, then substring, then typed-regex match) and label the binding: constant,
   user input, copied, transformed, unresolved. Emit a draft flow with slots. No LLM.
3. **Synthesize extractors.** For "copied from unstructured text" bindings, try in order: a typed
   regex from the catalog; the entity gazetteer; a FlashExtract-style position program learned from
   the `(text, span)` pairs across traces; only then an agent-written regex. Gate each with held-out
   traces.
4. **Test.** Replay the flow against the traces' cassettes; assert it produces the values the agent
   carried forward. A flow is `candidate` when the suite passes and `promoted` after N clean live runs.
5. **Repair.** On a runtime failure, a test regression, or a user complaint, demote the flow and hand
   the agent the flow, inputs, outputs, and the complaint. The agent's fix is a new flow version;
   the old traces plus the new run become its tests.

## Milestone 1: one flow end to end (done 2026-09-09)

Deliverable: the "investigate a Jira ticket" flow runs from the UI against simulated servers with
no AI, and its regression test passes.

1. **Synthetic world.** Deterministic generator: a handful of services and teams, people, Slack
   channels, and incidents. Each incident yields a Jira ticket, a Slack thread with a trace id,
   a PagerDuty incident, log lines with pod names and trace ids, an error-rate metric spike,
   a Confluence runbook page, and a commit in a tiny real git repo whose source contains the error
   string and the logger call.
2. **Simulated MCP servers** (stdio, FastMCP), one per corporate tool, mimicking the public
   servers' tool names and parameter shapes: jira, slack, confluence, chronosphere, logz, pagerduty,
   git, and a code server (grep/read over the sim repo). Responses are JSON-in-text like the real ones.
3. **Trace recorder.** Hook script, project `.claude/settings.json` and `.mcp.json`, and a driver
   that runs `claude -p` with recording. A scripted-agent mode produces traces in the same format
   without spending tokens, for development.
4. **Flow interpreter.** YAML schema above; Jinja templates; extractors (regex, jsonpath, catalog,
   window); forEach; ladders; MCP client over stdio; run records saved as JSON.
5. **Inducer.** Traces to draft flow via binding search; typed slots from the regex catalog.
6. **Replay tests.** Cassette client for the runner; pytest that replays every promoted flow.
7. **UI.** Flow catalog, generated parameter form, run page with per-step evidence and queries,
   run history, and a "this didn't help" button that files a repair request (queue only in M1).

Status: all seven parts exist. The hand-written flow passes its regression suite live and via cassette;
the inducer compiles 15 scripted sessions into a flow with zero unresolved bindings that runs on tickets it
never saw. Known limits: extractor synthesis stops at typed regexes, label-anchored lines, catalog lookups and
windows (no FlashExtract-style position programs yet); the inducer aligns steps by (tool, occurrence), which
breaks if the agent reorders two uses of the same tool; the UI queues complaints but nothing consumes them.

## Later milestones

- **M2.** Agent-assisted authoring: `crystal author` runs the agent with the flow catalog exposed as
  tools, so it tries flows before exploring; repair command consumes the complaint queue.
- **M3.** The other two typical flows (Slack thread, Slack DM); span synthesis from traces;
  local SQLite FTS5 index over results for cross-run search.
- **M4.** Point at real servers: swap sim servers for the public Atlassian, Slack, Grafana,
  Chronosphere, PagerDuty servers; schema-drift check on startup; nightly live regression.

## Decisions taken (2026-09-09)

- Python backend with a plain HTML/JS UI served by FastAPI.
- Simulated servers with generated data for milestone 1; public servers later.
- Claude Code headless with hooks as the flow author; no Agent SDK code.
- Milestone 1 is one flow end to end, every layer thin.
- Hard constraint carried over: no additional index or search servers. SQLite and local files only.
- The agent is never invoked by this codebase without an explicit user command; runtime is AI-free.

## UI direction (owner feedback, 2026-09-09)

The run page must read like a **dossier**: a condensed report on the thing being investigated, the way an agent's
markdown summary reads, not a log of tool calls.

- Headline first: what happened, when, which service, who owns it, what changed just before, the incident status,
  whether it is a repeat. These are derived from extracts and typed results, with no LLM.
- Evidence grouped by information type (issue, thread, log patterns, metric, commits, runbook, incident), each type
  with its own renderer, compact cards side by side where width allows.
- Extracted values highlighted inside the content; large results collapse to diff-style excerpts around matches, with
  expand links.
- A flow diagram of the research: steps, what flowed where, fan-outs; the same view for agent traces, subagents and
  workflows, with fan-out summaries (which items hit, union of what they found).
- Tool call details (queries, ladder attempts) collapsed into a "how" panel, off by default.

## Flow cards (owner request, 2026-09-09)

When `crystal author` (or `repair`) runs the agent, ask it to finish with a structured **flow card** rather than prose,
and store it in the flow YAML: `use_case` (when a person reaches for this flow), `inputs_explained`, `expected_outputs`
(a named list, one per step group), `not_covered`, `example`. The inducer drafts the skeleton deterministically from the
tools and extract types; the agent writes the prose and the judgment. The UI shows the card in the catalog and at the top
of the dossier, and `expected_outputs` doubles as acceptance criteria: the run page opens with "found N of M expected
things" and names the gaps, which makes "This didn't help" specific. Cards regenerate on repair so they never drift.
