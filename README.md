# mcp_explorer

A web frontend over MCP servers that runs the investigation flows an AI agent would run, **with no AI at
runtime**. An agent is used only during *crystallization*: it explores, its tool-call traces are recorded,
and the recurring flows are compiled into ordinary programs. The UI lets you pick a crystallized flow,
enter starting parameters, and see the evidence.

Design: `docs/PLAN.md`. Research behind it: `docs/research/`.

## Layout

| Path | What |
|---|---|
| `sim/` | Synthetic corporate world (`sim/world.py`) and simulated MCP servers for jira, slack, confluence, chronosphere, logz, pagerduty, git, code |
| `crystal/trace/` | Trace recorder: Claude Code `PostToolUse` hook, scripted agent, trace store |
| `crystal/extract/` | Typed ID regex catalog, entity catalog + gazetteer, extractor specs |
| `crystal/flow/` | Flow schema (YAML), Jinja templating, interpreter with fan-out and precision ladders |
| `crystal/induce/` | Trace → flow inducer (no LLM) |
| `crystal/replay/` | Cassette record/replay so regression tests need no servers |
| `app/` | FastAPI web UI |
| `flows/` | Crystallized flows: `investigate-jira-ticket`, `investigate-slack-thread`, `investigate-slack-dm` (candidates) and the raw `induced-*` drafts |
| `traces/` | Recorded sessions (JSONL) and cassettes; `feedback.jsonl` is the repair queue |

## Quick start

```bash
uv sync
uv run python sim/world.py            # generate sim/data/world.json and the sim git repo
uv run python sim/build_catalog.py    # catalog/entities.yaml (the foreign-key hub)
uv run python -m crystal.cli flows
uv run python -m crystal.cli run investigate-jira-ticket key=PAY-101
uv run python -m crystal.cli run investigate-slack-thread channel_id=C542575C5 thread_ts=1786015740.000000
uv run python -m crystal.cli run investigate-slack-dm "text=is payments healthy? a customer says card charges are hanging"
uv run uvicorn app.main:app --port 8765   # then open http://localhost:8765
uv run pytest -q
```

## Crystallization loop

1. **Record.** Traces come from either the scripted agent (free) or Claude Code with the project hook
   (`.claude/settings.json` + `.mcp.json`; `claude` in this directory records every MCP call to `traces/`):
   ```bash
   uv run python -m crystal.trace.scripted PAY-101 STF-109 SUP-102 PLAT-102 PAY-102 --variants 3
   uv run python -m crystal.trace.scripted --trigger slack_thread C542575C5/1786015740.000000 --variants 3
   uv run python -m crystal.trace.scripted --trigger slack_dm D59227FD8/1786020840.000500 --variants 3
   ```
   The real agent is launched only by an explicit command and costs money (`--budget` caps it):
   ```bash
   uv run python -m crystal.trace.driver slack_thread channel_id=C542575C5 thread_ts=1787484120.000005 --budget 3
   uv run python -m crystal.trace.driver slack_dm "text=hey, are you seeing checkout failures?" --budget 3
   ```
2. **Induce.** Compile the traces into a draft flow, no LLM:
   ```bash
   uv run python -m crystal.cli induce jira_issue --name induced-jira-ticket
   ```
   The report lists unresolved bindings (values that differ across sessions with no explanation), optional
   steps, ladders and fan-outs.
3. **Test.** `tests/test_inducer.py` runs the induced flow on a ticket that was never traced;
   `tests/test_flow_jira.py` replays the hand-written flow through a cassette; `tests/test_flows_slack.py` runs the
   thread and DM flows on a thread/DMs that were never traced.
4. **Run.** Promote by setting `status: promoted` in the flow YAML; the UI shows the status.
5. **Repair.** "This didn't help" on a run page appends to `traces/feedback.jsonl`. Handing that to the
   agent is milestone 2; nothing in this repo invokes an LLM on its own.

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
`catalog/entities.yaml`), `window` (timestamp ± durations). `all: true` returns every match.

## Pointing at real servers

Replace entries in `servers.yaml` with the real MCP servers (the sim tools copy the public servers' tool
names and parameter shapes) and rebuild `catalog/entities.yaml` from their data. Flows reference tools as
`server.tool`, so nothing else changes.
