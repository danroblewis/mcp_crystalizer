# Research: replacing AI-agent MCP tool chaining with programmatic systems

Research run on 2026-09-09 by a multi-agent workflow: seven search angles in parallel, a deep read of the top sources per angle, a synthesis, a completeness critique, and targeted gap-fill research.

## Start here

- [report.md](report.md): the synthesized report. Bottom line, pattern catalogue, what the agent was actually doing, recommended architecture, where an LLM is still needed.
- [critique-and-gaps.md](critique-and-gaps.md): what the critic said was missing, and the follow-up research that filled the top gaps.
- [sources.md](sources.md): every URL, grouped by angle.

## Per-angle detail

| Angle | Findings | Deep reads |
|---|---|---|
| [Agent distillation: turning agent traces into deterministic programs](angles/agent-distillation.md) | 21 | 5 |
| [MCP ecosystem: driving MCP servers without an LLM](angles/mcp-ecosystem.md) | 20 | 6 |
| [Entity and ID extraction from unstructured text without ML](angles/entity-extraction-no-ml.md) | 18 | 5 |
| [Tool graph linking: declaring how one result feeds the next tool](angles/tool-graph-linking.md) | 19 | 5 |
| [Local fuzzy and semantic search with no extra server](angles/local-fuzzy-search.md) | 16 | 5 |
| [On-call investigation automation: what is deterministic today](angles/oncall-investigation-automation.md) | 18 | 5 |
| [Codebase navigation without an index server](angles/codebase-navigation-no-server.md) | 19 | 5 |
