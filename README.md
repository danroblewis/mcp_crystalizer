# mcp-explorer

A `uvx` frontend for MCP servers and `mcp.json` files. Run it in a repository and it opens that directory as a
workspace, exactly like starting Claude Code there: the same `.mcp.json`, the same servers, plus a local web UI.
Its job is to let you use your Claude Code sessions to extract **repeatable, cheaply reproducible flows** so you can
use Claude Code less: a hook records what the agent did, `induce` compiles the recordings into a flow (YAML, no LLM),
and the flow then runs against the same MCP servers with no AI at all, rendering a dossier of the evidence it found.

```bash
cd ~/src/your-repo
uvx mcp-explorer                 # serves http://127.0.0.1:8765 for this directory
```

Nothing is written into the repository. All state lives under `~/.mcp-explorer/`, one directory per workspace.

## Install and run

```bash
uvx mcp-explorer --help                       # from PyPI, or from a checkout: uvx --from /path/to/mcp_explorer mcp-explorer
uvx mcp-explorer                              # = `mcp-explorer serve` in the current directory
uvx mcp-explorer serve --port 9000 --open     # pick a port, open the browser
uvx mcp-explorer --workspace ../other-repo    # any command can point at another directory ($CRYSTAL_WORKSPACE works too)
```

Commands (`mcp-explorer [--workspace <dir>] <command>`):

| Command | What |
|---|---|
| `serve [--port N] [--open]` | the web UI for the workspace (default command) |
| `install-hook [--uninstall] [--status]` | add the recording hooks to `~/.claude/settings.json` |
| `record <trigger> k=v ... --yes [--budget 3]` | one headless Claude Code session here, recorded (**costs money**) |
| `induce <trigger> [--name n]` | compile the recorded sessions of a trigger into a draft flow (no AI) |
| `flows`, `run <flow> k=v ...`, `card <flow>` | list flows, run one, show its card |
| `test <flow> [--live\|--offline]`, `status` | regression through recorded responses; promotion state |
| `author ... --yes`, `repair ... --yes` | agent-assisted authoring and repair (**cost money**) |
| `servers`, `tools`, `mcp-config [--write]` | the effective MCP servers, their tools, the merged mcp.json |
| `seed --from <dir>`, `workspaces` | copy starting data into a workspace's state; list known workspaces |
| `hook` | the hook entry point Claude Code calls (reads JSON on stdin) |

Only `record`, `author` and `repair` launch Claude Code; they ask for confirmation (or `--yes`), are capped with
`--budget`, and print the cost. Everything else, including serving the UI, running, inducing and testing flows, never
calls an LLM.

## The loop

1. **Record.** Install the hook once:
   ```bash
   mcp-explorer install-hook
   ```
   From then on every interactive Claude Code session, in any directory, records its prompt (`UserPromptSubmit`),
   every MCP tool call (`PostToolUse`) and the agent's final message (`Stop`) into that directory's workspace state.
   Claude Code's own Read/Grep/Glob/Bash are recorded (as short previews) only once a session has touched an MCP
   server; a session that only edits code leaves nothing behind. Or record one headless session on purpose:
   ```bash
   mcp-explorer record jira_issue key=PAY-108 --yes --budget 3
   ```
   `record` runs `claude -p` in the workspace with a temporary mcp.json of the effective servers
   (`--strict-mcp-config`) and a temporary settings file carrying the same hooks.
2. **Induce.** Compile the sessions of a trigger into a draft flow, no LLM:
   ```bash
   mcp-explorer induce jira_issue --name investigate-jira-ticket
   ```
   Sessions align by what their calls reference, values that vary become inputs, extractors or precision ladders,
   repeated calls over a list become fan-outs, and every draft carries `tests:` (one case per traced input) and a
   skeleton card. The report lists what stayed unresolved.
3. **Run.** From the UI or the CLI:
   ```bash
   mcp-explorer run investigate-jira-ticket key=PAY-101
   ```
   The dossier shows headline facts, coverage against the flow card, the evidence grouped by information type with
   the extracted values highlighted, and a research-flow diagram; tool calls are collapsed at the bottom. The same view
   renders recorded agent sessions at `/traces`.
4. **Trust.** `status: draft | candidate | promoted` in the YAML is the author's intent; the runtime keeps its own
   effective state with a circuit breaker (a step error, a required step with zero hits, a failed `test`, or a
   "this didn't help" from the UI demotes one level; N clean live runs climb back). `mcp-explorer test <flow>` replays
   the flow's cases through a cassette seeded from the sessions it was induced from.
5. **Author / repair** (optional, cost money): `author` runs the agent with the instruction to run the best existing
   flow first through the built-in `flows` MCP server and explore only for what it lacked; `repair` does the same for
   each queued complaint. Each produces `<flow>.v<N>.yaml`, never overwriting anything.

## mcp.json

The servers a workspace sees are the `mcpServers` format Claude Code, Claude Desktop and Cursor read, layered:

1. **built-ins**: `code` (grep / glob / read_file / codeowners) and `git` (git_log / git_show / git_grep / git_blame)
   over the workspace root, and `flows` (the workspace's crystallized flows as tools, for the authoring agent);
2. `~/.claude.json`, the `mcpServers` you already configured for Claude Code (user scope);
3. `~/.mcp.json`;
4. `<workspace>/.mcp.json`.

Later layers win per server name; `"name": null` or `{"disabled": true}` removes a server. Entries support stdio
(`command` + `args` + `env`), streamable HTTP (`type: http`) and SSE (`type: sse`), `${VAR}` / `${VAR:-default}`
expansion and `{{workspace}}`. `${MCP_EXPLORER_PYTHON}` is always the interpreter mcp-explorer runs under, so a
python server declared as `"command": "${MCP_EXPLORER_PYTHON:-python}"` gets this tool's dependencies under `uv run`
and `uvx` alike. `mcp-explorer servers` shows the effective set and where each entry came from; `mcp-explorer
mcp-config --write` stores the merged result as `<workspace>/.mcp.json` so a plain `claude` there sees the same
servers.

## Where state lives

```
$MCP_EXPLORER_HOME/                     default ~/.mcp-explorer
  workspaces/<dirname>-<8 hex>/          sha1 of the absolute path: two checkouts called `api` never mix
    workspace.json                       root, remote, first seen
    flows/*.yaml                         the workspace's flows (drafts, versions, promoted)
    runs/*.json                          saved runs (the dossier)
    traces/*.jsonl                       recorded sessions; traces/runs/ archives the run records they refer to
    cassettes/<flow>.json                recorded responses for `test`
    catalog.yaml                         the entity catalog (services, teams, channels, people) the extractors use
    lifecycle.sqlite                     promotion state and counters
    feedback.jsonl                       the "this didn't help" queue
```

Workspaces are isolated: the UI, CLI and `flows` server for one directory never show another's flows, runs or
traces. A workspace may ship starting data in `<workspace>/.mcp-explorer/` (`flows/`, `traces/`, `catalog.yaml`);
it is copied into the state dir on first use, and `mcp-explorer seed --from <dir>` does the same from any directory.
`mcp-explorer workspaces` lists everything the tool has state for.

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

Extractors: `jsonpath` (default), `regex`, `ids:<type>` (typed ID catalog), `catalog:<kind>` (gazetteer over the
workspace's `catalog.yaml`), `window` (timestamp ± durations), `position` (a span program learned from traces).
Every flow may carry a `card:` (use case, inputs explained, expected outputs, what is not covered) that the catalog
and the top of the dossier show and that coverage is judged against; `mcp-explorer card <flow> --write` drafts one.
Flows reference workspace facts as `{{ workspace.repo_owner }}`, `{{ workspace.name }}` (from the git remote).

## The sim: an example workspace for development

`examples/sim/` is a simulated corporate world (jira, slack, confluence, chronosphere, logz, pagerduty, plus code
and git over a small generated repo) with recorded sessions, flows and a catalog. It is test data, and the only
"state" in this repository. To develop against it:

```bash
git clone ... mcp_explorer && cd mcp_explorer
uv sync
uv run python examples/sim/world.py             # generates examples/sim/data/world.json and the sim git repo
uv run pytest -q                                # temp state dir seeded from examples/sim; never touches ~
uv run mcp-explorer --workspace examples/sim    # serves the sim: its .mcp.json declares the servers, its
                                                # .mcp-explorer/ seeds the flows, traces and catalog on first use
uv run mcp-explorer --workspace examples/sim run investigate-jira-ticket key=PAY-101
uv run mcp-explorer --workspace examples/sim induce jira_issue --name induced-jira-ticket-all
```

`examples/sim/.mcp.json` runs each sim server as `${MCP_EXPLORER_PYTHON:-python} servers/<name>.py`. The test suite
uses the in-process transport (`CRYSTAL_INPROCESS=1`: python servers are imported and connected over in-memory
streams instead of spawned); `tests/test_transport_parity.py` checks it agrees with real stdio. Scripted (free)
sessions for the sim come from `python -m crystal.trace.scripted PAY-101 ... --variants 3`.

Layout: `crystal/` is the package (CLI, registry, workspace and state dirs, flow interpreter, inducer, extractors,
lifecycle, author/repair, built-in `code`/`git`/`flows` servers, the web app under `crystal/app/`); `examples/sim/`
the sim; `tests/` the suite; `docs/PLAN.md` status and direction, `docs/research/` the background.
