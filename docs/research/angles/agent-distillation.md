# Agent distillation: turning agent traces into deterministic programs

_Research angle `agent-distillation`. Generated from the research workflow run on 2026-09-09._

## Angle verdict

Yes, this angle yields a real, reusable pattern; I would call it "trace-grounded crystallization with audited extractors". The five sources converge on one loop: (1) run the agent with full trace capture, including raw MCP responses and the exact value it carried from one tool result into the next call (tool-maker paper: the executed trace, not the schema, is what makes generated code work); (2) have an LLM emit an executable artefact per step (regex/JSONPath extractor, parameterised query template, or a 20-50 line function with a fixed signature returning a compact structured record) rather than an answer; (3) gate that artefact with mechanical audits before caching: uniqueness of match, value-format validation, and a held-out cross-trace check so an extractor that only works on the one Slack message it was written from is rejected (ScrapingBee), plus a repair loop with candidate voting when no labels exist (tool-maker Appendix D); (4) route each new request to the cheapest tier that has earned it, with evidence thresholds and a circuit breaker that demotes a step back to the LLM when it stops validating (Progressive Crystallization, Stagehand self-heal). The important caveat comes from Compiled AI's DocILE table: LLM-free regex reached parity only on ID-shaped fields and collapsed to ~20% on semantic ones, so the user should plan for a small, compile-time-bounded, schema-fixed LLM call for steps like phrasing a Confluence/Slack search from a ticket description, and reserve zero-LLM execution for the ID-plumbing steps (Jira key, SHA, pod, service, time window) that make up most of the chain. None of the sources requires an external index; extractor caches, fingerprints and trace stores are JSON/SQLite files, which fits the hard constraint.

## Deep reads

### Progressive Crystallization: Turning Agent Exploration into Deterministic, Lower-Cost Workflows in Production (arXiv 2607.07052, Microsoft Azure Networking)

- **Link:** <https://arxiv.org/abs/2607.07052>
- **Evidence quality:** production-proven

**Key techniques**

- Three-tier execution taxonomy for the same discovered behaviour: Type 3 agent-orchestrated (~10k-50k tokens, ~50% reproducible), Type 2 hybrid (fixed step structure, LLM only for interpretation/classification at specific stages, ~1k-5k tokens), Type 1 deterministic (pre-coded logic + typed API calls, zero tokens, 100% reproducible).
- Trace-extraction ('capture') algorithm: parse the agent trace into an ordered list of tool calls, detect the branch conditions the agent acted on, infer input/output schema per step, build a DAG of tool dependencies, parameterise instance-specific values (device IDs, timestamps), mark human-approval points as explicit gates. Output is a reusable playbook template.
- Step-level promotion rule: a step is replaced by a deterministic rule when the LLM 'consistently produces the same classification' across runs; a step where reasoning varies but the outcome is stable becomes a scoped single-purpose prompt; steps whose output is drawn from a finite set become rules last. Acceptance tests are auto-generated from successful traces and gate every promotion.
- Evidence thresholds (Table II, configurable): 3->2 needs >=10 successful runs, zero safety violations, >=90% of runs producing the same action sequence, all auto-tests pass, no human override in the recent window; 2->1 needs >=50 hybrid runs, >=99% LLM classification consistency, deterministic rule covering all observed input variation, regression suite passing without the LLM, human review of the logic.
- Circuit-breaker demotion: a promoted playbook is demoted one tier on execution failure, safety violation, or acceptance-test regression, and re-promoted after a run of clean executions. Production example: a firmware update changed a command's output format, the deterministic parser failed, the playbook fell back to hybrid so the LLM handled the new format.
- Routing: each incoming request is routed to the lowest-cost type available for its pattern; novel incidents always enter as Type 3, so the Type 1:2:3 ratio is a platform maturity metric.

**How to apply it.** This is the lifecycle skeleton for the user's plan. Concretely: (1) record every Claude Code tool-call trace over jira/slack/confluence/chronosphere/logz/pagerduty/git as an ordered list with per-step input/output, (2) run a capture step that turns each trace into a DAG template with slots (ticket key, service name, time window, commit SHA, pod/host ID) in place of instance values, (3) keep a per-template counter and only promote a template to 'runs with no LLM' once N recorded runs produced the same tool sequence and the replayed template reproduces the agent's recorded outputs (the auto-generated acceptance tests), (4) wrap the LLM-free runner in a circuit breaker so a Jira/Slack/logz format change demotes the template back to 'ask the agent' instead of silently returning garbage. The PagerDuty alert type + service + error signature is the natural trigger key for routing an incoming page to a template.

**Limitations.** Only a 4-page short paper. The extraction algorithm, trace format, signature matching, trace clustering and acceptance-test generation are each described in a single sentence; no pseudo-code, no example playbook, no detail on how parameters are pulled from unstructured tool output. Results are single-org, uncontrolled platform-level observations. Treat as architecture vocabulary and threshold defaults, not a recipe.

**Quotes**

> The extraction algorithm parses the trace into an ordered list of tool calls, detects the branch conditions the agent acted on, infers input and output schemas per step, builds a directed acyclic graph of tool dependencies, parameterizes instance-specific values such as device identifiers and timestamps, and marks human-approval points as explicit gates.

> Each promoted playbook is monitored, and a circuit breaker demotes it to a higher execution type on execution failure, safety violation, or acceptance-test regression.

> A more capable model does not automatically earn more autonomy; a track record does.

### Zero-Shot E-Commerce Scraping: Call the LLM Last (ScrapingBee engineering blog; uses parsel + Scrapling + Ollama)

- **Link:** <https://www.scrapingbee.com/blog/ecommerce-scraping-cascade-scrapy-local-llm/>
- **Evidence quality:** production-proven

**Key techniques**

- Cost cascade, cheapest first: Tier 1 read structured data the source already exposes (JSON-LD, __NEXT_DATA__ hydration blobs); Tier 2 replay the internal JSON/GraphQL endpoint; Tier 3 non-LLM fingerprint healing when a selector breaks; Tier 4 local LLM writes a selector map once per template. The LLM is reached only when everything cheaper misses.
- LLM writes an extractor, not an answer: input is cleaned HTML (1.2M raw tokens -> 2.8k tokens via markdown conversion), output is a JSON {field: css_selector} map produced with a JSON-schema-constrained format at temperature 0, with the hard rule 'each selector must match exactly one element'. The map is then executed by parsel at ~2 ms/page with zero tokens.
- Three audits gate caching, with problems fed back to the model in a repair loop (max 3 rounds): structural audit (audit_map: every selector matches exactly 1 element and is valid CSS), value audit (semantic_audit: name non-empty, 0 < price < 10000, rating word present), cross-page audit (cross_page_audit: run the map on 2 held-out pages and compare path_signature = ancestor chain of (tag, sorted classes); a selector that matches 0 or a different DOM region on another page 'likely encodes a value specific to the sample page' and is rejected).
- Cache as a list of candidate maps, not one map: load_cached_map iterates every stored map in selectors.json, runs it on the current page, and returns the first whose output passes validate(); new maps are appended, never overwrite. An asyncio.Lock per template prevents concurrent regeneration during a redesign.
- Fingerprint healing before LLM regeneration: Scrapling stores a fingerprint of the matched element (tag, text, attributes, siblings, path, parent) in a SQLite file; when the selector matches nothing it scores all candidates in the new DOM with difflib.SequenceMatcher and returns the best above 0.4 similarity. Healed 12/12 redesigned pages in 78 ms, zero tokens. Fingerprint key is per template (template://books), not per URL.
- Reported: 65 products across 2 stores with exactly 1 model call on the cold run and 0 on the second; explicitly lists what validation misses (currency symbol ambiguity, decimal-comma parsing) and says those need secondary alarms.

**How to apply it.** Transfers almost line-for-line from CSS selectors over HTML to regex / JSONPath / substring extractors over MCP results. For each recorded agent step of the form 'took value X out of tool result R and put it into parameter P of the next call', have the LLM (once) emit an extractor spec (regex with a named group, a JSONPath, or a 'take the token matching pattern near anchor word' rule) rather than the value. Then gate it with the same three audits on the recorded traces: structural (the extractor matches exactly one span in R), value (the span matches the target parameter's format: JIRA-\d+, ISO timestamp, sha1, k8s pod name, service name from a known list), and cross-example (the extractor also matches on 2+ other held-out traces for the same template and yields the value the agent actually used there, rejecting extractors that overfit to one Slack message or log line). Cache a list of extractor candidates per (template, step) and use the first that validates, which handles Slack/Confluence/logz formatting variants. The Tier 1 idea also applies: prefer structured fields the MCP already returns (Jira issue JSON, PagerDuty incident JSON, git log) before regexing free text. The fingerprint-heal idea maps to 'if the regex stops matching, find the closest substring to the previously-extracted value by difflib before calling the LLM'.

**Limitations.** Single-author blog on a toy domain (product pages); no public repo, only inline code. Fingerprint healing lives inside the Scrapling library and is DOM-specific; the substring analogue would need to be written. The value audit is hand-written per field, so the user still has to author (or LLM-generate) format validators per MCP parameter type.

**Quotes**

> Reach for an LLM when a page is HTML-only, has no reachable API, and drifts too structurally for a non-LLM healer.

> Cross-page: selector that resolves elsewhere likely encodes a value specific to the sample page.

> On a template with embedded JSON or a stable selector, a scraper built this way reaches the model rarely, if at all.

### Compiled AI: Deterministic Code Generation for LLM-Based Workflow Automation (arXiv 2604.05150)

- **Link:** <https://arxiv.org/abs/2604.05150>
- **Evidence quality:** research-prototype

**Key techniques**

- Compile phase: an Orchestrator takes a YAML workflow spec and selects from a Template Library (synchronous handlers, streaming processors, batch processors with checkpointing, input validators with fallback logic) and a Module Library (DB access, HTTP clients, notification delivery); the LLM only fills in narrow 20-50 line business-logic functions inside those pre-validated templates.
- Four-stage validation pipeline before deployment, with regeneration on failure: security static analysis (Bandit, Semgrep for injection/path traversal), syntax (AST parse, mypy, lint), execution (sandboxed runs against fixtures), accuracy (comparison against golden datasets). 96.1% precision on prompt-injection detection over 135 cases; 87.5% static safety catch rate with zero false positives.
- Runtime: generated code runs as static Temporal activities with zero LLM calls in the control plane. Where a step is genuinely semantic, a 'bounded tool call' invokes the LLM with prompt template, output schema and trigger condition all fixed at compile time (the 'Code Factory' variant).
- Break-even formula n* = GenTokens_compiled / (RuntimeTokens_per_tx_direct - RuntimeTokens_per_tx_compiled): with 9,600 generation tokens vs 552 tokens per direct call, break-even at ~17 executions; 57x fewer tokens at 1,000 executions; 40x cheaper at 1M/month.
- DocILE document-field-extraction result (Table 3): pure deterministic regex 20.3% KILE / 59.7% LIR (4,915x faster than LLM but fails on semantic fields: customer names 10.1%, currency amounts 9.2%); Code Factory (deterministic orchestration + bounded LLM extraction) 80.0% / 80.4%; Direct LLM 80.0% / 74.5%. Compilation failure rate 4% (16/400), all syntactically valid and detectable.

**How to apply it.** Two takeaways for the MCP frontend. First, the cost model: one-time LLM generation of an extractor/query generator pays for itself after roughly 17 runs of that step, which is the argument for the 'LLM writes the code, then leaves' plan. Second, a warning that matters more than the headline: the fully LLM-free regex variant scored 20% on semantic fields (names, amounts), and parity with the runtime LLM was reached only by leaving a schema-fixed, compile-time-bounded LLM call in the loop for those fields. So the user's design should classify each agent step as 'mechanical' (ID-shaped values: Jira keys, SHAs, pod names, timestamps, PagerDuty incident IDs -> regex/JSONPath, no LLM) versus 'semantic' (phrasing a Slack or Confluence search query from a ticket description, choosing which error message in a log line is the interesting one) and budget a small, fixed-prompt, schema-constrained LLM call for the latter rather than pretending regex will cover it. The template-library discipline (LLM fills 20-50 line functions inside a hand-written harness that owns MCP auth, pagination, error handling) is the right shape for generated extractors over the jira/slack/confluence/logz clients, and Bandit/Semgrep + fixture replay is the validation gate.

**Limitations.** Not a production system; two benchmarks (BFCL function-calling and DocILE), Temporal-specific, and the strong numbers are on structured function-calling, not on free-text extraction. Requires an accurate YAML spec up front, which the user would have to derive from traces. The pure-deterministic path is explicitly shown to be weak on semantic fields.

**Quotes**

> Pure deterministic extraction (regex) runs 4,915 times faster than direct LLM but achieves only 20.3% KILE, an 80 percentage point gap driven by failures on semantic fields: customer names (10.1% KILE) and currency amounts (9.2% KILE), where contextual understanding is required.

> Code Factory compiles orchestration deterministically and invokes the LLM as a bounded tool call for each extraction step, with prompt template, schema, and trigger conditions all fixed at compile time.

> Break-even against direct LLM occurs at n* ~= 17 transactions. At 1,000 transactions, compiled AI uses 57 times fewer tokens than direct LLM and 84 times fewer than AutoGen.

### Tool-Making and Self-Evolving LLM Agents in Low-Latency Systems (arXiv 2607.08010; fulfilment-centre alarm triage over MCP backends)

- **Link:** <https://arxiv.org/html/2607.08010>
- **Evidence quality:** production-proven

**Key techniques**

- Per-node compilation of a CodeAct loop into one deterministic tool: each SOP decision-tree node (which the baseline agent answered by writing fresh MCP-querying code, up to 100+ LLM turns) becomes a versioned Python function with a uniform signature (warehouse, timestamp, context) -> {verdict: true/false/no-data, observed value, threshold, textual explanation}. Context carries site parameters and verdicts from parent nodes; thresholds are resolved from a runtime parameter store so one tool serves many sites.
- Grounding via a data-collector trace: the existing CodeAct sub-agent is run on 3 labelled cases per node against the production MCP and its trace (query code, MCP responses, observed schema with field names/dtypes/value ranges, graded verdict) is fed to the tool-maker LLM together with the SOP text and the node's parent/child nodes. Ablation: full trace 94.5% pass@1; schema metadata alone +1.8pp, graded verdict alone +1.7pp, so the executed trace is what closes the gap between prose and the real interface.
- Test-repair loop: candidate is run on the full labelled set; a reflector LLM receives failing (input, expected, predicted) triples and writes a short diagnosis; tool-maker rewrites conditioned on it; budget 3 rounds, early stop on full pass, deploy the highest pass-rate candidate. Appendix E documents the loop 'unlearning' correct code by chasing fewer failures, so hold-out checks matter.
- Label-free variant (Appendix D): K=3 sub-agent investigations per node, an LLM judge filters traces inconsistent with the SOP, N=10 candidates generated, pseudo-labels by majority vote across candidates and judge-passing traces, R=5 repair rounds with a code-review judge; reaches 91.6% vs 94.5% labelled ceiling, and 99.1% once the SOP text was clarified. Voting across N candidates is the largest contributor (N=1 loses 8.6pp).
- Runtime policy: main agent calls the node tool directly (direct-call architecture, p50 latency -62% on top of -42% from tools vs sub-agent); exceptions fall back to the CodeAct sub-agent for that node only. Every invocation logs tool version, inputs, outputs; a batch monitoring agent reviews logs for drift and a flagged tool is regenerated and promoted as a new version after passing the held-out set and manual review. Deploying or reverting a tool is a config change.
- Deterministic tools surfaced three upstream data inconsistencies that generated code had silently adapted to (a field type differing between offline and prod MCP, cumulative vs active event counts, an endpoint switching from 75 to 0.75). Library of 42/44 nodes generated for a one-time 800K tokens; per-alarm output tokens -58%, error rate -36% to -53%.

**How to apply it.** Closest production analogue to the user's setup because the backends are MCP servers and the agent is Claude-Code-like CodeAct. Recipe: treat each recurring investigation step (e.g. 'given a Jira key, find the Slack thread', 'given a service + window, pull error-rate from chronosphere', 'given a pod name, fetch logz lines with the stack trace') as a node; run the existing agent on ~3 recorded examples with full trace capture (tool inputs, raw MCP responses, observed field names/types, the value the agent extracted next); feed SOP-ish description + traces to an LLM to write one Python function per node with a fixed signature returning a compact structured record; validate against all recorded traces with a reflector repair loop; version the functions in a local repo/sqlite and log every call. The label-free voting scheme (generate N candidate extractors, pseudo-label by majority agreement) is the answer to 'we don't have hand-labelled expected outputs, only agent traces'. The 'compact structured verdict' contract is why the LLM-free frontend can chain nodes: each node returns typed IDs, not prose, so the next node's parameters are filled by field access rather than parsing.

**Limitations.** Nodes are numeric-threshold checks over metric tables (dataframes), not free-text search; the paper never addresses phrasing search strings or extracting IDs from Slack/log prose, so the WebFetch summary claiming query-expansion/NLP techniques was fabricated and should be ignored. Still needs an agent at runtime to traverse the decision tree (tools replace the inner loop, not the orchestrator). Four nodes never passed because the SOP was underspecified, which the authors say is the actual ceiling.

**Quotes**

> The resulting trace, passed to the tool-maker, contains the query code, the MCP responses and observed schema (field names, datatypes, value ranges), and the graded verdict, supplying the execution details the SOP omits.

> Each tool returns the verdict, the observed value, the threshold, and a textual explanation. "No data" is reserved for missing metrics, and exceptions trigger fallback to the CodeAct sub-agent of the baseline.

> Because a tool returns the same value on the same input, its logs reveal environment issues that run-to-run variation in generated code hides.

### Stagehand (Browserbase): caching observe()/act() results and replaying agent runs as scripts

- **Link:** <https://docs.stagehand.dev/v2/best-practices/caching>
- **Evidence quality:** production-proven

**Key techniques**

- What is cached: the ObserveResult the LLM produced for a natural-language step, a JSON object {description, method, selector (xpath), arguments}. Executing a cached ObserveResult via page.act(result) performs 'NO LLM INFERENCE'.
- Cache pattern is deliberately left to the user (getCache/setCache over a JSON file, actWithCache(page, key, prompt, selfHeal)): key defaults to the prompt string, with an explicit note to use a more unique key (page content / DOM / accessibility tree) when prompts repeat across contexts.
- Self-heal: when replaying the cached action throws, actWithCache re-runs page.act(prompt) which re-engages the LLM to find a new selector; with selfHeal=false it throws. Docs do not auto-evict the stale entry; that is user code.
- Agent run -> script: the replay() utility on the build-agent page converts the agent's AgentAction list into a replay.ts file of concrete act({method, selector, arguments}) calls, with the rule that 'Replay will default to Playwright first to avoid unnecessary LLM calls'; on failure 'Stagehand AI will take over and self-heal'.

**How to apply it.** The simplest end-to-end template for the user's frontend: the unit of caching is one agent decision expressed as a concrete, executable action (here xpath+method+args; for MCP it would be tool name + fully-resolved parameters + the extractor used to fill them). The key design question Stagehand surfaces is the cache key: 'prompt text' is too coarse, so for the MCP case the key should be (template id, step index, source-tool result shape) rather than the ticket text. Self-heal maps to 'if the cached extractor yields nothing or the downstream MCP call errors, invoke the LLM for this one step, then overwrite the cache entry'. Emitting a replay.ts from an agent trace is the literal 'record trace, emit deterministic script with slots' step.

**Limitations.** Thin: two short doc pages, no discussion of validating a cached action beyond 'it did not throw', no held-out checks, key design is left to the user, and the domain is browser DOM rather than API results with embedded IDs. Useful as a shipped proof of the record-replay-self-heal loop, not as a methodology.

**Quotes**

> Replay will default to Playwright first to avoid unnecessary LLM calls!

> If the Playwright action fails, Stagehand AI will take over and self-heal.

> We want to leave caching logic up to you, but give you all the tools

## All sweep findings

_21 findings, sorted by relevance score._

### Progressive Crystallization: Turning Agent Exploration into Deterministic, Lower-Cost Workflows in Production (arXiv 2607.07052)

- **Link:** <https://arxiv.org/abs/2607.07052>
- **Kind:** paper
- **Relevance:** `██████████` 9.5/10

**What it is.** Production AIOps paper (cloud-networking incident system, tens of thousands of incidents/month). Defines a lifecycle: fully agent-orchestrated -> hybrid -> fully deterministic. Repeatedly-validated agent traces over metrics/log/ticket/service tools are distilled into parameterised deterministic workflows with signature-based triggers (alert type, service, error pattern), evidence-based promotion (minimum incident counts, success-rate comparison vs the agent) and automatic demotion/fallback to the agent when a workflow regresses. Deterministic share went 0% -> 45% in 8 months; per-incident agent cost fell >70% while volume doubled.

**Why it matters here.** Almost exactly the user's plan applied to on-call work: record agent traces, crystallise the common paths into LLM-free workflows, keep the agent as fallback for the long tail. Provides the promotion/demotion and trigger-matching vocabulary the user will need. Full PDF was fetched; mechanics of parameter extraction are described only at a high level, so treat it as an architecture reference, not a code recipe.

### Zero-Shot E-Commerce Scraping: Call the LLM Last (ScrapingBee)

- **Link:** <https://www.scrapingbee.com/blog/ecommerce-scraping-cascade-scrapy-local-llm/>
- **Kind:** blog
- **Relevance:** `████████░░` 8.5/10

**What it is.** Engineering write-up of a four-tier cascade where a local LLM reads ONE sample page and writes a JSON selector map; deterministic code (parsel) then runs the map on every other page at ~2 ms/page and zero tokens. The map is only cached after three audits: structural (each selector matches exactly one element), value (type/range/pattern checks), and cross-page (held-out pages, to reject selectors that overfit to sample-page values). On drift, a non-LLM fingerprint relocation is tried first; only if that fails does the LLM regenerate a map, which again must pass the audits. Reported: 65 products crawled with 1 model call.

**Why it matters here.** The clearest production instance of 'LLM writes the extractor, then is removed'. The audit gate design (uniqueness, value validation, held-out cross-example check) transfers directly to LLM-written regex/ID extractors over Slack/log/wiki text, and the repair cascade is a template for what to do when a Jira/Slack format changes.

### Compiled AI: Deterministic Code Generation for LLM-Based Workflow Automation (arXiv 2604.05150)

- **Link:** <https://arxiv.org/abs/2604.05150>
- **Kind:** paper
- **Relevance:** `████████░░` 8/10

**What it is.** Proposes a 'compile phase' where an LLM generates narrow business-logic functions embedded in validated templates; thereafter the workflow runs with zero runtime model calls. Evaluated on BFCL function-calling (96% completion, zero runtime tokens, break-even at ~17 executions, 57x token reduction at 1,000 executions) and DocILE document field extraction (matches the LLM on key fields at 80%). Includes static safety analysis of generated code.

**Why it matters here.** Provides the cost framing (break-even after ~17 runs) the user can use to justify one-time LLM use, and shows LLM-compiled extractors matching LLM-at-runtime accuracy on field extraction. Template-constrained generation is a good discipline for extractor/query-generator code.

### Tool-Making and Self-Evolving LLM Agents in Low-Latency Systems (arXiv 2607.08010)

- **Link:** <https://arxiv.org/html/2607.08010>
- **Kind:** paper
- **Relevance:** `████████░░` 8/10

**What it is.** Production alarm-triage system (fulfilment centres). A pre-deployment 'tool-maker' pipeline mines execution traces and backend schemas, generates candidate tools from repeated SOP steps, validates them against labelled cases, versions and stores them. At runtime agents call the compiled tools first and only generate code when needed; tools return compact structured verdicts. Median latency -42% from tool calls, a further -62% from a direct-call architecture, error rate down to 53%.

**Why it matters here.** Same shape as the user's problem (alarm/incident triage over backend systems): LLM-authored, trace-derived tools that encapsulate a multi-step lookup and return structured output, with generation demoted to a fallback. Shows 'direct-call' (no LLM orchestration) as the end state.

### Stagehand: cache act()/observe() and replay as a script

- **Link:** <https://docs.stagehand.dev/v2/best-practices/build-agent>
- **Kind:** product
- **Relevance:** `████████░░` 7.5/10

**What it is.** Browserbase's Stagehand records the concrete Playwright actions an LLM chose for each natural-language step, caches them, and can emit a TypeScript script that replays the run. Replayed scripts 'default to Playwright first to avoid unnecessary LLM calls'; if a cached action fails the model takes over, self-heals, and updates the cache.

**Why it matters here.** A shipped, widely used implementation of 'LLM decision cached as code with self-healing fallback'. The user's MCP frontend can do the same: record the agent's concrete tool calls per step, emit a replayable script with slots, and re-engage an LLM only when a step's validation fails.

### Anthropic: Code execution with MCP / Programmatic Tool Calling

- **Link:** <https://www.anthropic.com/engineering/code-execution-with-mcp>
- **Kind:** blog
- **Relevance:** `████████░░` 7.5/10

**What it is.** MCP servers are exposed as code modules; the model writes code that calls tools, filters/joins/aggregates results in the sandbox and only returns the small final result to context (150K -> 2K tokens example; 37% average reduction on research tasks in the advanced-tool-use post). Explicitly recommends persisting working code as reusable functions/SKILL.md files so the agent builds 'a toolbox of higher-level capabilities'. Data can flow between tools without ever entering the model's context.

**Why it matters here.** Establishes the intermediate step the user wants: have the agent express each investigation as code over MCP tools (IDs extracted in code, not by the model), then save that code. Once saved, the code can be run without the model at all. Companion post: https://www.anthropic.com/engineering/advanced-tool-use

### SkillWeaver: Web Agents can Self-Improve by Discovering and Honing Skills (arXiv 2504.07079, OSU-NLP)

- **Link:** <https://arxiv.org/abs/2504.07079>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Agent explores an environment, practises proposed skills, and converts practised trajectories into reusable Python functions (APIs); a 'honing' loop tests and debugs the synthesised APIs against environment feedback. APIs synthesised by a strong agent transfer to weaker agents (+54.3% on WebArena). Code: https://github.com/OSU-NLP-Group/SkillWeaver

**Why it matters here.** Concrete trajectory -> Python function -> test/debug pipeline; the 'strong agent writes APIs, weak/no agent consumes them' pattern matches distilling Claude Code traces into MCP-composing functions. Also a source of prompts for turning a trace into a parameterised function.

### Inducing Programmatic Skills for Agentic Tasks (ASI, arXiv 2504.06821)

- **Link:** <https://arxiv.org/abs/2504.06821>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Web agents induce, verify and reuse program-based skills online from their own successful traces; skills are executable programs (compositions of primitive actions) rather than text. Beats static agents by 23.5% and text-skill (AWM-style) baselines by 11.3%, with 10.7-15.3% fewer steps; shows skill transfer across websites with adaptation.

**Why it matters here.** Direct evidence that program-form skills mined from traces outperform natural-language workflow memory, supporting the user's choice to distil into code rather than into prompts.

### Agent Workflow Memory (arXiv 2409.07429, CMU)

- **Link:** <https://arxiv.org/html/2409.07429v1>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Induces reusable workflows from successful trajectories: each workflow is a NL description plus steps of (state, reasoning, executable action) where example-specific values are replaced by slots like {product-name}. Two inducers: rule-based (dedupe by action sequence, drop invalid steps) and LM-based (extract common sub-routines across experiences). Offline and online modes. WebArena 35.5% vs 23.5% baseline.

**Why it matters here.** The slot-abstraction step (replace concrete IDs/strings in a trace with typed variables) is exactly the first transformation the user's distiller must do; the rule-based inducer is fully LLM-free. AWM still runs an LLM at inference, so it is a component, not the whole answer.

### LILAC: Log Parsing using LLMs with Adaptive Parsing Cache (FSE 2024, arXiv 2310.01796)

- **Link:** <https://arxiv.org/abs/2310.01796>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** LLM generates log templates (constants + <*> variable placeholders) for unseen log messages; templates are stored in an adaptive parsing cache (tree-structured) and every subsequent log is matched against the cache without the LLM. Reduces LLM queries by several orders of magnitude while improving template accuracy F1 by 69.5% over prior parsers. Code: https://github.com/logpai/LILAC

**Why it matters here.** For the logz.io/chronosphere side of the graph: ID and variable extraction from log lines can be bootstrapped by an LLM into templates that are then matched deterministically and locally (in-process, no index server). The cache-with-refinement design handles template drift.

### Cloudflare Code Mode (blog + @cloudflare/codemode)

- **Link:** <https://blog.cloudflare.com/code-mode/>
- **Kind:** blog
- **Relevance:** `██████░░░░` 6.5/10

**What it is.** Instead of exposing N MCP tools, expose one execute(code) tool over typed bindings; the model writes JavaScript that chains calls, handles pagination and filters responses inside a V8 isolate, returning only what it needs. Reports 32-81% token savings and, for the 2,500-endpoint Cloudflare API, 1.17M -> ~1K tokens. Follow-up: https://blog.cloudflare.com/code-mode-mcp/ ; HN discussion https://news.ycombinator.com/item?id=45399204

**Why it matters here.** Same 'agent as code author over MCP' pattern as Anthropic's; useful as the runtime for the user's frontend since generated JS/TS can be re-run in a sandbox without a model. No built-in persistence/reuse of generated code, so the user must add that layer.

### Get Experience from Practice: LLM Agents with Record & Replay (AgentRR, arXiv 2505.17716)

- **Link:** <https://arxiv.org/html/2505.17716v1>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6.5/10

**What it is.** Records environment state and operations for each step; a summary phase produces multi-level experiences. Low-level experiences are concrete action sequences with identified variables, descriptions and constraints, enabling rapid replay when the environment matches; high-level experiences are abstract and need a (local) model to instantiate. Check functions guard replay safety.

**Why it matters here.** Gives a taxonomy the user can adopt: low-level (exact replay with variable substitution, no LLM) vs high-level (needs a model). Their observation that low-level replay performs best supports investing in precise variable/ID identification within traces.

### FlashExtract: a framework for data extraction by examples (PLDI 2014, Microsoft PROSE)

- **Link:** <https://www.microsoft.com/en-us/research/publication/flashextract-framework-data-extraction-examples/>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6.5/10

**What it is.** Programming-by-example synthesis of extraction programs for text files, web pages and spreadsheets from a few highlighted examples, using a DSL of map/filter/merge/pair operators and inductive synthesis. Part of the PROSE SDK; sibling FlashFill handles string transformations.

**Why it matters here.** Adjacent but instructive: a purely deterministic, in-process way to get an ID/substring extractor from examples the agent trace already supplies (the value the agent picked out of a tool result is a labelled example). Can serve as the fallback synthesiser when an LLM-written regex fails audits, or as a verifier.

### Blueprint First, Model Second: Source Code Agent (arXiv 2508.02721)

- **Link:** <https://arxiv.org/abs/2508.02721>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6/10

**What it is.** Control flow is fixed as an expert-authored code Execution Blueprint run by a deterministic engine; the LLM is invoked only at bounded nodes and never chooses the path. TravelPlanner pass rate 35.56% vs 18.00% SOTA, constraint violations -96%; authors report transfer to production incident-diagnosis deployments.

**Why it matters here.** Describes the 'hybrid' end state where the LLM is confined to fuzzy sub-steps (e.g. phrasing a Slack search) while sequencing and ID plumbing are deterministic code; explicitly cites incident diagnosis. Blueprints are hand-written here, but they are the target format for a trace distiller.

### Data Extraction via Semantic Regular Expression Synthesis (Smore, arXiv 2305.10401)

- **Link:** <https://arxiv.org/abs/2305.10401>
- **Kind:** paper
- **Relevance:** `██████░░░░` 5.5/10

**What it is.** Semantic regexes combine syntactic regex operators with semantic predicates; synthesised from small positive/negative example sets via neural sketch generation plus type-directed compositional synthesis. Outperforms neural and PBE baselines on extraction from text.

**Why it matters here.** Adjacent: shows that regex-plus-semantics extractors can be learned from a handful of examples (the kind of examples an agent trace yields when it picks an ID out of a Slack message). Worth knowing if pure regex is too brittle for the wiki/Slack cases.

### AgentDistill: Training-Free Agent Distillation with Generalizable MCP Boxes (arXiv 2506.14728)

- **Link:** <https://arxiv.org/abs/2506.14728>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Teacher agent autonomously generates 'MCP boxes' (structured, reusable task-solving modules) while solving problems; student agents on small models reuse the boxes directly instead of imitating trajectories, matching GPT-4o-based OctoTools on biomedical and math benchmarks.

**Why it matters here.** Terminology overlap only (their 'MCP' is 'model-context-protocol module', not the tool protocol), but the principle of distilling into reusable modules rather than fine-tuning matches the user's plan; the student here is still an LLM, so it stops short of LLM-free.

### Voyager: An Open-Ended Embodied Agent with Large Language Models (arXiv 2305.16291)

- **Link:** <https://arxiv.org/abs/2305.16291>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Canonical executable skill library: GPT-4 writes JavaScript skills validated by in-environment execution and self-verification, stored and retrieved by embedding of their NL description, composed for new tasks; transfers to new worlds.

**Why it matters here.** Origin of the 'skills as verified code, indexed by description' idea; retrieval by description could pick which distilled investigation to run for a new ticket. Skills are still invoked by an LLM planner.

### Is Programming by Example solved by LLMs? (arXiv 2406.08316)

- **Link:** <https://arxiv.org/abs/2406.08316>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Finds pretrained LLMs are weak at few-example PBE (string/list transformations); fine-tuning helps in-distribution but out-of-distribution generalisation degrades sharply.

**Why it matters here.** Cautionary: an LLM-written extractor from 2-3 trace examples may not generalise; reinforces the need for held-out validation (as in the ScrapingBee audits) and possibly symbolic PBE as a fallback.

### Inducing Reasoning Primitives from Agent Traces (arXiv 2606.02994)

- **Link:** <https://arxiv.org/html/2606.02994v1>
- **Kind:** paper
- **Relevance:** `████░░░░░░` 4.5/10

**What it is.** Mines ReAct traces: filters successful rollouts, extracts per-step thoughts, clusters them via LLM into canonical reasoning moves, synthesises the top-K into typed 'pseudo-tool' primitives whose behaviour is an LLM-interpreted docstring. Large gains over source agents at ~24% lower cost than workflow-induction methods.

**Why it matters here.** Adjacent: the trace-mining pipeline (filter -> extract -> cluster -> synthesise with support threshold) is reusable for discovering the recurring 'moves' in Claude Code traces, but the primitives remain LLM-executed, so it does not remove the model.

### Trace2Skill: Distill Trajectory-Local Lessons into Transferable Agent Skills (arXiv 2603.25158, Qwen)

- **Link:** <https://arxiv.org/abs/2603.25158>
- **Kind:** paper
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Parallel analyst sub-agents inspect success and failure trajectories and propose patches that are hierarchically consolidated into a conflict-free SKILL.md directory; skills transfer across model sizes/families. Code: https://github.com/Qwen-Applications/Trace2Skill

**Why it matters here.** Adjacent: distils traces into text skills for an LLM, not code. Useful only for the consolidation step (merging lessons from many traces without conflicts).

### CompileAgent (yuer-dsl/compileagent)

- **Link:** <https://github.com/yuer-dsl/compileagent>
- **Kind:** oss-project
- **Relevance:** `██░░░░░░░░` 2.5/10

**What it is.** Proof-of-concept that 'compiles' an agent plan DSL into an IR and runs it on a deterministic, replayable, audited executor. 2 stars, 4 commits, v0.1-0.2.

**Why it matters here.** Name matches the idea but it is a toy DSL runner, not trace-derived; noted so the synthesiser does not spend time on it.

## Dead ends

_Queries and directions that produced nothing useful. Listed so nobody repeats them._

- site:news.ycombinator.com queries for 'replace LLM with deterministic code after bootstrapping' returned only generic LLM-determinism threads (temperature/seed debates); the 'Deterministic Programming with LLMs' thread (id=47158834) is about test-validating generated code, not agent distillation.
- Anthropic Agent Skills searches: no official mechanism for auto-generating SKILL.md from Claude Code session traces was found; only the manual skill format and marketplaces.
- Semantic-cache searches (GPTCache, Redis, TrueFoundry) describe embedding-similarity response caching, not caching decisions as executable code; TVCACHE (arXiv 2602.10986) is a tool-result cache for RL training only.
- n8n/Temporal/Airflow 'generate workflow definition from agent execution trace' returned only orchestration comparisons; no tooling that emits a workflow DAG from traces.
- Agent replay/debugging tools (agent-replay, opentraces, agent-inspect, dev.to JSONL replay) replay recorded tool RESULTS for debugging, not re-executable programs against live tools.
- Cloudflare Code Mode HN thread (id=45399204) and the 'you don't need code mode' Speakeasy thread had no discussion of persisting generated code or running it LLM-free; the latter page could not be fetched.
- 'Agent Skills Matter / SigLeak' (arXiv 2607.25560) reconstructs hidden skills from trajectories but from a security-leakage angle with an LLM student; not applicable.
- Alloy (arXiv 2510.10049) is programming-by-demonstration for web agents but the generalised workflows are still executed by an LLM agent; abstract gives no code-level representation.
- LLM-Guided Compositional Program Synthesis (arXiv 2503.15540) abstract only; no benchmark details retrievable to confirm FlashFill relevance.
- Search for LLM-generated regex from examples (RASLAN 2024 paper, clinical regex generation) surfaced only secondary summaries; not fetched/verified so excluded from findings.
