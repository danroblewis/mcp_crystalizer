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
| **Test suite** | no | Generated from the author's traces. Replays each flow against recorded responses; a failure demotes the flow's effective status (the UI lists every flow with its badges; only the badge changes). |

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
never saw. Follow-up (same day): the inducer aligns steps across sessions by a signature of the bound argument
templates (tool + the text/id/window classes each argument references) instead of (tool, occurrence), so the real
Claude Code session merges with the scripted ones: its late thread read, second git_log, rounded time windows
(`window: {round: 1h}`) and extra calls (issue comments, list_services, get_incident) become shared or optional
steps with zero unresolved bindings; timestamp arguments are decided by majority and never ladder. Extractor
synthesis gained FlashExtract-lite position programs (`crystal/extract/positions.py`, extractor kind `position`)
as the fallback after catalog/regex/window: start and end positions expressed as the k-th meeting point of small
left/right token-class or literal contexts, learned by intersecting the candidate programs of every (text, span)
pair and ranked with free negatives. Known limits: a list element the agent chose by content (the runbook whose
title matched) still binds as `| first`; the UI queues complaints but nothing consumes them.

## Milestone 3 (part): the Slack-thread and Slack-DM flows (2026-09-09)

Both remaining typical flows exist as `status: candidate` in `flows/`:

| Flow | Trigger / inputs | Steps |
|---|---|---|
| `investigate-slack-thread` | `slack_thread`: `channel_id`, `thread_ts` | thread -> (jira_search if no key) -> issue -> confluence -> runbook -> code -> metrics -> logs per trace id -> logs by error -> pagerduty -> commits |
| `investigate-slack-dm` | `slack_dm`: `text` (the DM, as the driver's prompt carries it) | dm (find it in Slack: who/when) -> history (rest of the DM conversation) -> incidents (search #incidents by key / error class / service around the DM time) -> thread -> confluence -> runbook -> code -> owners -> metrics -> logs per trace id -> logs by error -> (jira_search if nobody named a ticket) -> issue |

What was added to make them inducible and runnable:

- **World.** Every person has a DM channel (`D…`, `is_im`) with "me"; each incident produces one DM from a colleague
  outside the owning team phrased one of three ways: ticket key only ("hey, are you on PAY-101?"), error class +
  service alias ("customers report SessionExpired ... in storefront checkout"), or service alias + symptom ("is
  warehouse sync healthy? stock levels look stale"), a follow-up with a trace id a customer pasted, plus noise DMs.
  Services carry prose aliases (`checkout`, `payments`, `warehouse sync`, `notification service`) that the catalog
  gazetteer resolves. The Slack sim lists ims in `channels_list`, serves `conversations_history`/`replies` on `D…`
  ids, and every message now carries an ISO `time` next to `ts` (as real search results do) so the inducer can anchor
  time windows on a Slack message.
- **Scripted agent.** `investigate_slack_thread(channel_id, thread_ts)` and `investigate_slack_dm(channel_id, ts)`
  with three order/phrasing variants each; 15 + 18 sessions recorded over incidents INC-001..006. The DM variant
  records `inputs: {text}` only; the channel/ts merely locate the text.
- **Induction.** Both triggers induce with zero unresolved bindings. The raw drafts are kept as
  `flows/induced-slack-thread.yaml` / `flows/induced-slack-dm.yaml` (re-induced after the real sessions were added).
  Hand fixes applied in the promoted YAML: `{% if %}` guards on every ladder rung so a value the trigger lacks blanks
  the rung instead of searching the whole workspace (the inducer emits `{{ dm.service }} in:#…` which renders as
  ` in:#…` when the DM names no service); `hits:` paths; `when:` on steps that only make sense with a key/service;
  precedence chains (`dm.service or thread.service or incidents.service`) in place of the inducer's flat ladders;
  dropped two accidental bindings (`sum({{ runbook.chronosphere }})` as a PromQL rung, `runbook.error_class` as the
  grep pattern, both true in the traces and wrong in general); `unique | list | head(1)` instead of `unique | head(1)`
  (Jinja's `unique` is a generator).
- **Tests.** `tests/test_flows_slack.py` runs each flow live on inputs that were never traced (SUP-105's thread; the
  PAY-108 service-only DM; the STF-119 key-only DM) and asserts the evidence: thread found, ticket found, logs per
  trace id, metrics, runbook, no step errors.

### Observed agent behaviour (real Claude Code vs. scripted agent)

One headless Claude Code session per trigger, recorded through the hook (`--budget 3`; actual cost $0.90 for
`slack_thread`, 18 calls/23 turns, and $0.84 for `slack_dm`, 21 calls/24 turns; no retries needed). Traces:
`traces/b4bdae3e-….jsonl` (thread, STF-116) and `traces/845f1217-….jsonl` (DM, PLAT-104).

**Thread trigger.** Same skeleton as the scripted agent (replies -> get_issue -> logz per trace id -> logz by error
-> pagerduty -> chronosphere -> confluence -> get_page), but:

- It read `jira_get_issue_comments` and immediately `git_show`ed the short sha named in the thread (step 4), before
  any logs or metrics. The scripted agent never reads comments and only lists commits.
- Time bounds are whole days derived from the thread date (`2026-08-22T00:00Z`..`2026-08-24T00:00Z`) rather than
  ±hours around the parent message; the metric query used `status=~"5.."` and a ratio
  (`sum(rate(...5..)) / sum(rate(...))`) where the scripted agent uses `code=~"5.."` and a plain rate.
- `pagerduty.list_incidents` was called with dates only (no `service_ids`), then again for the service over a month.
- It found the handler by deriving the path from the service name (`code.read_file services/checkout_web/handler.py`)
  instead of `grep`, then ran `git_log` on that file.
- It widened scope on its own: searched Slack (`SessionExpired checkout-web`, no `in:`/dates) and Jira
  (`component = checkout-web AND text ~ "SessionExpired"`) for recurrences and produced a table of three incidents.
  Nothing in the scripted flows does recurrence analysis; this is the one behaviour worth adding as extra steps.

**DM trigger.** The real agent's first move matches the scripted one (search Slack for tokens of the DM:
`RateLimited notification`, which found the DM and its `time`), but it then spent six calls on a wrong premise:
it took "since about 10am" to mean *today* and queried PagerDuty, Logz and Chronosphere for 2026-09-09 with a
guessed service name `notification-service` (`service:notification-service AND RateLimited`,
`http_requests_total{service="notification-service",status="429"}`), all empty. It re-anchored only after
`jira_search text ~ "RateLimited" AND updated >= -14d` returned PLAT-104 with its dates, then followed the scripted
shape: logz `service:notifier AND RateLimited` in an 8h-18h day window, a trace-id search, metrics
(`error_rate{service="notifier"}`, a metric name it invented; the sim tolerates it), a Slack search
`notifier in:#incidents after:2026-08-29 before:2026-09-01` (the exact shape the scripted agent uses), git_log on the
service path, jira comments, `title ~ "notifier runbook"`, read_file, thread replies, git_show, get_page. It never
called `conversations_history` on the DM channel. It finished with two "is it still happening" checks (`now-48h`
metrics; logs after the incident) that the scripted agent does not do. Lessons taken into the flow: resolve the
service through the catalog gazetteer (aliases) instead of guessing a name, anchor the window on the DM's `time`, and
put the key/class/service search of #incidents before anything time-bound.

**Inducer effect of the real sessions.** Adding one real session per trigger left `unresolved` empty but added
optional steps seen in 1/16 or 1/19 sessions (comments, git_show, second pagerduty, recurrence searches, "still
happening" checks). The candidate YAML ignores those; they are visible in the `induced-*.yaml` drafts. Two problems
seen when the flows were first induced went away in the milestone-2 merge: whole-day bounds no longer become
hardcoded-date ladders (windows can round their anchor, `round: 1d`, and timestamp arguments never ladder), and real
responses recorded as content blocks (`[{type: text, text: …}]`) are parsed into JSON by the hook and the trace
store, so no induced extract reads `[*].text` any more.

## Milestone 2: lifecycle and agent-assisted authoring (done 2026-09-09)

Merged from three parallel branches (inducer alignment + position programs; the Slack flows above; lifecycle +
author/repair). What exists:

- **Promotion lifecycle + circuit breaker** (`crystal/flow/lifecycle.py`, `state/lifecycle.sqlite`, gitignored). The
  YAML `status` is the author's intent; the runtime keeps an effective status next to it with clean/failed counters,
  a clean streak, test results, complaints and an event log. A step error, a required step with zero hits, a failed
  run, a failed regression test or a UI "This didn't help" demotes one level; N consecutive clean live runs
  re-promote (`candidate_after: 2`, `promote_after: 5`, per flow), never above the author's intent. A passing test
  is recorded but is not live evidence (it never advances the streak), and the authoring agent's `run_flow` runs
  are saved without touching the lifecycle. `crystal status` prints the table; the UI shows both badges, counters,
  the last failure and recent events.
- **Regression per flow** (`crystal/replay/regression.py`, `crystal test <flow>`): runs the flow's `tests:` cases
  (or one built from the inputs' examples, which must find at least something) through `traces/cassettes/<flow>.json`,
  seeded from the sessions in `induced_from`; live for cassette misses unless `--offline`; the result feeds the
  lifecycle. The inducer writes the `tests:` block itself: one case per traced input with `min_hits: 1` on every step
  that had hits in every session, so a draft whose steps quietly return nothing fails its own regression.
- **Flows as tools for the agent** (`sim/servers/flows.py`, registered as `flows` in `servers.yaml`/`.mcp.json`):
  `list_flows()` and `run_flow(name, inputs_json)` execute the interpreter and return a size-capped evidence summary.
- **`crystal author <trigger> k=v --yes`** (`crystal/author.py`): the prompt lists the existing flows ordered by trust
  and tells the agent to call `run_flow` FIRST and use raw tools only for gaps. The run record the agent's `run_flow`
  produced is archived in `traces/runs/` next to the trace, the call is expanded into the flow's own calls (ladder
  rungs and fan-out items included) and the session is induced together with every earlier session of the trigger
  into `flows/<base>.v<N>.yaml`, never overwriting; the new version is regression-tested. `crystal induce` applies
  the same expansion, so author sessions merge instead of contributing a `flows.run_flow` step.
  One real run (`author jira_issue key=PAY-108`, $0.50): the agent ran the flow first, then made exactly two raw
  calls (logs for a third trace id the flow surfaced but never queried; `pagerduty.get_incident`) and named a false
  extract in the flow (a pod hash taken for a git sha). `flows/investigate-jira-ticket.v2.yaml` is the result,
  re-induced with the merged inducer (the first version, produced before the merge, carried timestamp ladders,
  hardcoded-date windows and the pod hash as a literal rung).
- **`crystal repair [--all|<run_id>] --yes`**: hands each queued complaint (flow YAML, inputs, compact evidence,
  complaint text) to the agent, induces a version the same way and appends a `handled` record. Tested with a fake
  driver only; never run for real yet.
- **Inducer** (from milestone 1's follow-up): signature-based alignment, canonical step ids across sessions,
  majority-vote windows with rounding, id-sized copies, position programs (whose ranking is a total order: two
  boundary programs that tie on anchor, negatives, runs and |k| are separated by whether they name the span's own
  token class, then by size and class generality, so learning no longer depends on the hash seed).
  `crystal/trace/driver.py` is a reusable `run_agent()` used by `author`, `repair` and the standalone driver.

Over the merged corpus, all three triggers induce with zero unresolved bindings (jira 17 sessions / 19 steps, thread
16 / 17, DM 19 / 25) and every flow in `flows/` passes `crystal test` or its pytest. Real-agent spend for the
milestone: $0.90 + $0.84 (Slack traces) + $0.50 (author) = $2.24.

What remains:

- The inducer treats a fan-out item that erred (the flow's git_show on the pod hash) like any other call: in the
  17-session jira draft it splits `commit` into a 16/17 step and a 2/17 `commit_2`. The pod hash itself no longer
  ships as a rung (an unexplained literal from one session is dropped and reported), but fixing the false extract
  in `investigate-jira-ticket.yaml` (the sha regex matches pod hashes) would remove the split; the candidate flows
  are unaffected. Reference cycles between merged steps (the thread agent searched Jira before reading the issue,
  the scripted one after) are cut by dropping the forward fallback rung; the report names each cut.
- Position programs have a held-out gate but no real corpus exercises them: all three drafts report
  `position_programs: {}`, so the feature is validated by synthetic tests only.
- The `PostToolUse` hook now records Claude Code's Read/Grep/Glob only inside a session that already has a trace
  (driver-launched, or after an MCP call), as short previews; sessions recorded before this change carry none.
- The three candidate flows are still hand-fixed copies of the induced drafts (`{% if %}` rung guards, `hits:`,
  `when:`, precedence chains). Those fixes are the next things the inducer should learn to emit.
- Flow cards (below) are not produced yet; `author` ends with prose. Content-based list selection (the runbook whose
  title matched) still binds as `| first`. Recurrence analysis (the real thread agent's Jira/Slack searches for the
  same error class) is not in any candidate flow.
- The dossier layout (below) is built (`app/dossier.py`, `app/diagram.py`, `app/templates/dossier.html`; the same
  view renders recorded agent traces at `/traces/<session>`); the flow-card `coverage`/`headline` interface is used
  when `crystal/flow/cards.py` exists and computed from extracts otherwise. The `flows` MCP server starts the
  other sim servers as subprocesses on every `run_flow`.
- No `--offline` guard exists for `author`/`repair` beyond `--budget` and `--yes`; each real run costs money.

## Later milestones

- **M2 (done 2026-09-09).** Promotion lifecycle + circuit breaker; `crystal author` / `crystal repair`; see above.
- **M3.** Slack thread and Slack DM flows (done above as candidates) and position-program span synthesis (done)
  remain to be exercised on real servers; still open: local SQLite FTS5 index over results for cross-run search,
  flow cards, the dossier UI.
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

## Real-codebase experiments (2026-09-10)

Two real workspaces, each with one real Claude Code session recorded through the hook, crystallized with no LLM, and
re-run with no AI against the real servers.

| Workspace | Servers | Agent run | Crystallized flow |
|---|---|---|---|
| `aabbcdl/AgentArena` (stand-in: the requested `kadajett/AgentArena` returns 404) | github (npx, no token), git, code, deepwiki | 14 calls, $1.50 | `codebase-agentarena`: 13 steps, runs end to end; PRs, issues, commits, PR files, README |
| `modelcontextprotocol/python-sdk` | github, deepwiki (http), context7 (http), git, code | 12 calls, $1.31 | `codebase-python-sdk`: 11 steps incl. a DeepWiki answer, 15 merged PRs, a 4-file fan-out |

Findings:
- The agent knew the repo owner/name without any tool call (Claude Code shows it the git remote). Single-session
  induction bound `owner` to the first commit author, a plausible-but-wrong coincidence. Fix: workspace metadata
  (remote owner/name, from `git remote`) is now a first-class binding source (`{{ workspace.repo_owner }}`).
- The `question` input is prose; nothing in the flow binds to it. A codebase flow is really one flow per question
  shape ("what are the open PRs about", "how does X work"); the trigger should carry a question type, not free text.
- No renderers exist yet for GitHub issue/PR results; the dossier falls back to JSON cards, and the headline is
  incident-shaped. The dossier needs a "codebase" profile: repo facts, open PRs, recent commits, files that matter.
- DeepWiki only serves indexed repos; Context7 and DeepWiki need no auth; the GitHub server reads public repos
  without a token (rate-limited).
