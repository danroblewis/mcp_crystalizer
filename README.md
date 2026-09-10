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
| `refine <flow> --yes [--budget 2] [--name n]` | the agent proposes a name, card, titles, inputs and argument bindings for an induced draft; every binding is replayed against the traces before it is kept (**costs money**) |
| `servers`, `tools`, `mcp-config [--write]` | the effective MCP servers, their tools, the merged mcp.json |
| `seed --from <dir>`, `workspaces` | copy starting data into a workspace's state; list known workspaces |
| `import [--all] [--dry-run]` | import past Claude Code sessions (their transcripts) as traces, for free |
| `candidates [induce <n> --name f]` | recurring dataflow across the workspace's episodes (`--sequences`: tool chains); compile one into a flow |
| `hook` | the hook entry point Claude Code calls (reads JSON on stdin) |

Only `record`, `author`, `repair` and `refine` launch Claude Code; they ask for confirmation (or `--yes`), are capped
with `--budget`, and print the cost. Everything else, including serving the UI, running, inducing and testing flows,
never calls an LLM.

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
5. **Refine** (optional, costs money): `mcp-explorer refine <flow> --yes` hands a draft to the agent for a name, a
   card, step titles, inputs and a binding per unresolved argument. Every binding is replayed against the recorded
   episodes and kept only if it reproduces what they actually sent. See below.
6. **Author / repair** (optional, cost money): `author` runs the agent with the instruction to run the best existing
   flow first through the built-in `flows` MCP server and explore only for what it lacked; `repair` does the same for
   each queued complaint. Each produces `<flow>.v<N>.yaml`, never overwriting anything.

## Refining a draft with an agent

An induced flow is mechanically correct and unusable as a product: it is called `mined-code-git-3`, its steps are
called `run_python_3`, its inputs are whatever identifier happened to appear in the traces, and the arguments the
compiler could not derive are frozen at the first value one session used.

```bash
mcp-explorer refine induced-jira-ticket --yes --budget 2      # or the "Refine with an agent" button on the flow page
```

`refine` builds a prompt from the traces alone (no LLM): the draft YAML, the prompts that led to it, each step's
tool `inputSchema` from `tools/list`, the distinct values every supporting episode passed for each unresolved
argument and which episode passed which, which arguments are the same in every episode, the extracts each step
already makes, and the flow's current bindability. The agent answers with one YAML block proposing a `name`, a
`card`, `step_titles`, `inputs`, and one `binding` per unresolved argument (`input`, `derive`, `constant`, or
`unfixable` with a reason).

**The agent only proposes. Nothing it says is trusted.** Every proposal is decided here, deterministically:

* the **name** must be kebab-case and not already a flow in the workspace, else the draft keeps its own;
* the **card** goes through the same `validate_card` as everywhere else — an expected output naming a step that
  does not exist means the agent described a different flow, and the card is dropped;
* each proposed **input** is kept only when an accepted binding actually references it, and an input whose value is
  identical in every recorded episode is rejected: that is a constant, not a parameter;
* every **binding is replayed against the traces**. For each supporting episode we take the value that episode
  really passed for `(step, arg)`, rebuild that episode's context from its own recorded calls (its inputs, and the
  results of the calls before that step run through the flow's extracts plus any the agent proposed — no server is
  contacted), render the proposed template in it, and require the rendered string to equal the recorded value. A
  binding is kept only if it reproduces the recorded value in **every** episode the argument appears in; the first
  mismatch rejects it, naming the episode, the expected value and what the template rendered. A rejected binding
  leaves the argument exactly as the draft had it;
* an honest `unfixable` is recorded with its reason and the argument is left alone — a better outcome than an
  invented template.

The result is a new version, `<name>.v<N>.yaml`, status `draft`, carrying `refined_from:`; nothing is overwritten.
The command prints one row per proposal (accepted or rejected, and why), the bindability before and after, and the
cost. `refine` refuses under `$CRYSTAL_NO_AGENT`, without `claude` on PATH, without `--yes` (or an interactive
confirmation), and while another agent run holds the workspace lock.

## Recording from the UI

The **Record** page (`/record`) is `mcp-explorer record` without the terminal: type a question, pick a trigger name
(the default `prompt` groups plain questions; a known trigger such as `codebase` wraps the question in its prompt
template), cap the budget in USD (default 2, passed to Claude Code as `--max-budget-usd`), optionally name a model,
and tick the box that says it launches Claude Code and costs money. Nothing starts without the box, without
`claude` on PATH (the page says how to install it), or while another run is in progress in the workspace (one at a
time, a lock file in the state dir). The launch runs in the background and the job page follows the trace the
recording hook writes: calls so far, elapsed time, then the cost, turns and the agent's final message, a link to the
session's dossier under `/traces`, and a button to **induce a flow from this session**, which compiles it together
with every earlier session of the same trigger into `<trigger>.v<N>.yaml` (a draft, no LLM) and links to the flow.
The page updates itself with a few lines of JavaScript and works without it (refresh). Every job is persisted as
`records/<job>.json` in the state dir, so the history, with cost per run, survives a server restart.

## Capturing flows from past sessions

You do not need the hook to have been installed to get traces: Claude Code keeps a transcript of every session under
`~/.claude/projects/<project>/<session-id>.jsonl`, and every MCP call in it (tool call, arguments, result, the prompt
it was made under) is exactly what the hook would have recorded.

```bash
mcp-explorer import --dry-run          # the sessions run in this directory that made MCP calls, and what they called
mcp-explorer import                    # write them into this workspace's traces (source: transcript)
mcp-explorer import --all              # every project on this machine, each into its own workspace
```

`import` prints one row per session (prompts, MCP calls, subagents, episodes, calls per server, the cwd) and only
imports sessions that touched an MCP server; a session that only edited code is not an investigation. It is
idempotent (`--force` rewrites), reads `~/.claude` and never writes there, and `--transcripts <dir>` (or
`$MCP_EXPLORER_TRANSCRIPTS`) points it elsewhere. A session whose directory no longer exists still imports, flagged.

**Episodes.** A long session holds many prompts; each prompt that made MCP calls starts an episode, and each subagent
(the Agent tool, whose transcript sits in `<session-id>/subagents/`) is an episode of its own. A session with more
than one episode is written as the whole session `<sessionId>` for reference plus `<sessionId>-e<N>` (the main
thread's calls under the N-th prompt) and `<sessionId>-e<N>-a<agentId>` (each subagent spawned under it), with the
prompt (for a subagent, the instruction it was given) as `meta.prompt` and the inputs. Induction and mining work on
episodes. If the hook already recorded the session, its trace interleaves the subagents' calls under the parent
session id with no attribution; importing the transcript then *reattributes* that trace instead of importing it
twice: hook records are joined to the transcript on `tool_use_id`, each call gets its `agent`, and the episode
files are written from the hook's records (`--reattribute` redoes the join).

**Candidates.** Across all episodes of a workspace, hook-recorded, `record`ed or imported, `candidates` mines the
recurring **dataflow**: not which tools were called, but what information passed between them. For every argument
of every call the inducer's binder answers "where could this value have come from" -- the request, an earlier
result (an exact copy, a typed id inside it, a catalog entity, a `Label: value` line), the entity catalog, or
timestamp arithmetic -- and each answer is an edge `source --value_type--> target`:

```
prompt --ids:jira_key--> jira.jira_get_issue --ids:error_class--> logz.search_logs --copy:paths--> code.read_file
```

An argument nothing explains produces no edge. An episode is the *set* of its edges, and the candidates are the
frequent connected closed edge sets (support >= 2 episodes), ranked by support x edges, preferring graphs rooted
at a `prompt` edge -- a rooted graph names the flow's real input. Mining the dataflow rather than the sequence
fixes three things at once: a candidate is **bindable by construction** (every edge is a binding that really
happened, so a chain whose arguments the agent invented cannot form at all), an unrelated call in the middle no
longer breaks a pattern (it is simply not on the graph), and the same task with its steps in a different order is
**one** candidate instead of three:

```bash
mcp-explorer candidates                       # ranked list; --top N, --min-support N, --json (edges included)
mcp-explorer candidates induce 1 --name triage-ticket
mcp-explorer candidates --sequences           # the fallback miner: frequent contiguous chains of tool names,
                                              # for episodes whose arguments carry no dataflow at all
```

`candidates induce <n>` slices every supporting episode to the calls that carry the pattern's edges, in their
recorded order (with the calls they bind to pulled in, so the slice is self-contained), and runs the same inducer
over those partial sessions, so the draft flow (status `draft`, in the workspace's flows dir) contains exactly the
shared behaviour with its arguments bound the usual way. Each episode's edges are cached under the state dir
(`dataflow/<trace-stem>.json`, keyed by the trace's size and mtime), because the binder runs over every episode --
a working machine has thousands -- not just the ones that get induced. The UI has the same two pages: `/import`
lists the importable transcripts of the workspace with an Import button, `/candidates` the mined behaviours (the
sequence miner's, for now) with an "Induce as flow" button. No LLM is involved anywhere in import, mining or
induction.

## Replaying Claude Code's own tools

A recorded session is mostly `Read`, `Grep`, `Glob` and `Bash` -- Claude Code's own tools, not MCP ones -- so a flow
induced from it has nowhere to send them. Two things make those steps run:

* **Import maps what the built-in servers already do.** `Read`, `Grep`, `Glob` and read-only `git` commands are
  recorded as `code.read_file`, `code.grep`, `code.glob` and `git.git_log` / `git_show` / `git_grep` / `git_blame`.
* **A built-in `claude-code` server answers the rest**, under the names and argument shapes the transcripts use, so
  older drafts and anything that did not map keep working. Its `Bash` is **read-only**: a command is replayed only
  when its program is on the allowlist (`ls`, `cat`, `grep`, `find`, `git log`, `python3 -c`, ...), it does not
  redirect or chain, and package/VCS tools are limited to their read-only subcommands. Everything else is refused
  with an explanation rather than run, because a flow runs unattended and a session's `rm -rf build` must never be
  replayed. `CRYSTAL_ALLOW_SHELL=1` lifts the check; it is never the default.

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
    records/*.json                       Claude Code runs launched from the UI's /record page (status, cost, session)
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
