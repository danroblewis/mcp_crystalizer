# mcp_explorer

A CLI utility, run with `uvx`, that opens the **current directory as a workspace** (exactly like starting Claude Code
there), reads the same `mcp.json` files an AI coding agent would, launches a local web UI, and lets people use their
Claude Code sessions to extract **repeatable, cheaply reproducible flows** so they can use Claude Code less. The
runtime that runs a flow never calls an LLM.

## Invariants (do not break these)

1. **The workspace is the current directory.** `mcp-explorer` run in a directory serves that directory: its code, its
   git history, its `.mcp.json`. Nothing about the tool assumes it is running inside this repository. `--workspace`
   only overrides the default.
2. **No state lives in this repository.** Flows, runs, traces, cassettes, the entity catalog, lifecycle counters and
   feedback all live under `$MCP_EXPLORER_HOME` (default `~/.mcp-explorer/`), namespaced per workspace. This repo
   holds code, tests, docs and examples only. Anything checked in under `examples/` is test/development data.
3. **Workspaces are isolated.** The UI, CLI and flow catalog for one workspace never show another workspace's flows,
   runs or traces. A test against another repo must look like a fresh install.
4. **The runtime never invokes an LLM.** Only explicit commands (`author`, `repair`, `record`, `refine`) launch Claude Code, they
   require `--yes` or an interactive confirmation, and they print the cost. Running a flow, inducing a flow, testing a
   flow and serving the UI are all AI-free.
5. **`mcp.json` is the universal config.** The `mcpServers` format used by Claude Code, Claude Desktop and Cursor,
   layered: built-in defaults < `~/.mcp.json` < `<workspace>/.mcp.json`, with `${VAR}` expansion, `disabled`
   entries, and stdio / streamable HTTP / SSE transports. Built-in servers (`code`, `git`, `flows`) always point at
   the workspace.
6. **Flows are the product.** A flow is YAML: typed inputs, steps over `server.tool`, extractors, fan-out, precision
   ladders, a card (use case, expected outputs), regression tests, a lifecycle status. Flows are versioned, never
   overwritten by automation, and promoted only by a track record.
7. **Traces are the raw material.** A Claude Code hook records every MCP call (and, when installed user-wide, every
   session's prompt) into the workspace's trace dir. Induction compiles traces into flows with no LLM. Real-agent
   traces are precious: never delete them.
8. **The UI is a dossier, not a log.** Headline facts, coverage against the card, evidence grouped by information
   type with extracted values highlighted, a research-flow diagram; tool calls collapsed. Same view for agent traces.
9. **No extra servers.** SQLite, JSON, YAML and in-process libraries only. No search or vector service.

## Layout

```
crystal/           the package (CLI, registry, workspace/state dirs, flow interpreter, inducer, extractors,
                   lifecycle, author/repair, built-in code/git/flows servers, web app under crystal/app)
examples/sim/      the simulated corporate world: MCP servers, data generator, catalog, flows, traces. Test data.
tests/             the test suite (fast: in-process MCP transport, temp state dir seeded from examples/sim)
docs/              PLAN.md (status and direction), research/, sim-world.md
```

## Working here

- `uv sync`; `uv run pytest -q` must stay green and fast (tests use `CRYSTAL_INPROCESS=1` and a temp
  `MCP_EXPLORER_HOME`; they never touch the developer's home or the network unless `CRYSTAL_NET_TESTS=1`).
- `uv run mcp-explorer` from any directory serves that directory. `uv run mcp-explorer --workspace examples/sim ...`
  exercises the sim.
- Real-agent runs cost money: use them deliberately, cap with `--budget`, report the cost. Use Sonnet for routine
  subagent work (data generation, verification); keep the session model for design and compiler logic.
- When the owner says "start a workflow" they mean fan out parallel agents for speed, with one merge at the end.
- Commit messages end with the attribution trailer given in the session.
