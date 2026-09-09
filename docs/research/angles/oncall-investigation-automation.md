# On-call investigation automation: what is deterministic today

_Research angle `oncall-investigation-automation`. Generated from the research workflow run on 2026-09-09._

## Angle verdict

Yes - this angle yields a real, reusable pattern, and it is more concrete than the sweep suggested. Call it the "rule-graph correlation engine with crystallized playbooks": model each MCP server as a domain with classes and native-language query strings (korrel8r), connect classes with optimistic start->template->goal-query rules that silently skip when a field is missing and fan out when a field repeats (korrel8r), feed free-text results through an ordered list of guarded regex-with-named-groups extractors whose captures become entity attributes (Keep), then let goal search / N-hop neighbourhood search over the static class graph replace the agent's "which tool next" (korrel8r). Once a start object resolves to (service labels, time window), run a fixed catalog of parallel deterministic checks with per-check interestingness thresholds - error-pattern rate vs pre-window baseline, recent deploys, error-rate series (Sift) - executed as a blueprint graph with typed parse nodes, numeric conditionals, fork/correlate joins, and preconditions on expensive calls (Blueprint First). The rules and extractors are written once by an LLM from recorded traces, promoted only when they reproduce the agent's choices on all recorded inputs (>=99% agreement) and replay the traces as a regression suite, with a circuit breaker back to a single LLM call for novel cases (Progressive Crystallization). Everything in the pattern is in-process: a template engine, a regex list, a static graph, and threshold arithmetic - no index server required. The one gap none of the sources close is ranking among multiple candidate substrings in a long line; that has to be handled by ordered patterns plus post-filters (or a local Drain-style template miner), not by anything these sources provide.

## Deep reads

### Korrel8r - rule-based correlation engine over observability signals (Red Hat / OpenShift)

- **Link:** <https://github.com/korrel8r/korrel8r>
- **Evidence quality:** production-proven

**Key techniques**

- Four-concept data model per backend ('domain'): Object (a Pod, a log line, a time series), Store (the backend), Class (`domain:class`, e.g. `k8s:Pod`, `log:application`), Query (`domain:class:selector` where selector is the store's NATIVE query language - a k8s field/label selector, a LogQL selector, a PromQL expr, an alertmanager label match). Every tool server becomes a domain; every tool-call shape becomes a query string.
- Rule = start class(es) + goal class(es) + a Go text/template that receives ONE start object as `.` and renders a goal query string. Verbatim from etc/korrel8r/rules/_samples/alert.yaml: `AlertToPod` renders `k8s:Pod.v1:{namespace: "{{or (index .Labels "kubernetes_namespace_name") (index .Labels "namespace") | required}}", name: "{{or (index .Labels "kubernetes_pod_name") (index .Labels "pod") | required}}"}`; `AlertToMetric` renders `metric:metric:{{required .Expression}}`; `PodToLogs` renders `log:{{logTypeForNamespace .metadata.namespace}}:{"namespace":"{{.metadata.namespace}}","name":"{{.metadata.name}}"}`; `PodToAlert` renders `alert:alert:{"namespace":"{{.metadata.namespace}}","pod":"{{.metadata.name}}"}`. Note the `or (index .Labels A) (index .Labels B) | required` idiom: try several field names, fail the rule if none present.
- Skip-on-missing semantics: 'If a template returns a blank string or raises an error, korrel8r skips the rule for that object.' This is the whole 'fuzziness' mechanism - you write many optimistic rules and only the ones whose inputs exist fire. Rules can also fan out: `PodToPVC` uses `{{range .spec.volumes}}{{with index . "persistentVolumeClaim"}}...{{end}}{{end}}` to emit one query line per volume; `DependentToOwner` derives the goal CLASS itself from data (`{{k8sClass .apiVersion .kind}}`), so one rule covers 'follow the ownerReference whatever it points at'.
- Aliases group classes so one rule applies to many start types (`selectors` = ReplicationController, DaemonSet, Deployment, ReplicaSet, StatefulSet, Job, PodDisruptionBudget). Rules omitting `classes` apply to the whole domain (`AllToEvent`, `DependentToOwner`).
- Traversal is the fixed loop 'apply rules to objects, execute the resulting queries, apply more rules, and repeat' over the static class graph (nodes = classes, lines = rules; pkg/graph/data.go builds it from the rule list; Node.Empty() when a query returned nothing; RemoveEmptyGoalPaths prunes nodes not connected to a non-empty goal). Two modes: Goal search = 'Find shortest paths from a start to specific goal classes' ('find logs for this pod'); Neighbourhood search = 'Explore everything reachable within N rule hops'. Output is 'a correlation graph - not the data itself, but a map of what's available' (per-node: the goal queries rendered, and hit counts per query).
- Status rules: a second template kind that maps an object to a status label used to bucket counts, e.g. `LogSeverity` normalizes `level`/`severity_text` values (error/err/ERROR/critical/fatal -> Error; warning/warn -> Warning). Cheap, deterministic classification of noisy free-text fields.
- MCP surface (docs/reference/mcp): `create_goals_graph(start, goals[])`, `create_neighbors_graph(start, depth)`, `get_objects(query, constraint)`, `list_domains`, `list_domain_classes(domain)`, `help(domain)`. So the graph engine itself is exposed as a tool to agents; the repo also ships a `.claude/commands/generate-rule` command - i.e. an LLM writes rules once, the engine runs them forever with no LLM.

**How to apply it.** This is the direct blueprint for the frontend. Define one 'domain' per MCP server (jira, slack, confluence, chronosphere, logz, pagerduty, gdrive, arc/git, codebase). A Class is a result type each server returns (jira:Issue, slack:Message, confluence:Page, logz:LogLine, chronosphere:Series, pagerduty:Incident, git:Commit, code:File). A Query is a concrete MCP tool call rendered as a string (`slack:Message:{"query":"PROJ-123 in:#oncall"}`, `logz:LogLine:{"query":"trace_id:abc AND service:payments"}`). Rules are templates the one-time LLM writes from recorded traces: `IssueToSlack` renders a slack search from the issue key + service label; `SlackMessageToLogz` extracts a trace/request id with a regex template function and renders a logz query; `IssueToCodeGrep` renders a grep over the codebase for the error string; `CommitToConfluence` searches the runbook space for the component name. Skip-on-blank gives you the agent's 'only follow a lead if the ID is actually present' behaviour; fan-out templates give 'one search per candidate substring'; goal search gives 'get me from this Jira ticket to logs and metrics' without an LLM choosing the next tool; neighbourhood search with depth=2 gives the 'what's related' first pass. Everything is in-process (Go text/template, or a JS/Python equivalent like Jinja/Nunjucks) with a static class graph; hit counts per rendered query are exactly what a browser UI should show as the 'correlation map'. It also fits the token-budget constraint: korrel8r explicitly positions its engine as something an agent calls because it 'searches and correlates large data sets faster than an AI agent can'.

**Limitations.** Kubernetes/OpenShift-centric: all shipped domains and rules are k8s/alert/log/metric/trace/netflow/incident, so every domain for Jira/Slack/Confluence/etc. must be written from scratch (the domain interface is small but it's Go). Templates only render queries from structured fields of a single start object; extraction of IDs from long free-text needs custom template functions (regex) that korrel8r doesn't ship. No ranking/scoring of results beyond hit counts and 'shortest path'; no notion of time-window propagation except the per-query `constraint`. The docs site pages for search algorithm internals are sparse - the traversal details above come from the introduction page and pkg/graph source.

**Quotes**

> A rule is applied to an object of the start class and generates a query for the goal class. ... apply rules to objects, execute the resulting queries, apply more rules, and repeat.

> If a template returns a blank string or raises an error, korrel8r skips the rule for that object.

> Goal search: Find shortest paths from a start to specific goal classes. ... Neighborhood search: Explore everything reachable within N rule hops from a start.

### Progressive Crystallization: Turning Agent Exploration into Deterministic, Lower-Cost Workflows in Production (arXiv 2607.07052)

- **Link:** <https://arxiv.org/html/2607.07052>
- **Evidence quality:** production-proven

**Key techniques**

- Three-type taxonomy with measured cost/determinism: Type 3 agent-orchestrated (10k-50k tokens, ~50% reproducibility, HITL gates), Type 2 hybrid (fixed step structure, LLM called only at specific stages 'for interpretation or classification only', 1k-5k tokens, ~90%), Type 1 deterministic ('pre-coded logic with typed API calls and conditionals, zero LLM tokens at runtime', 100%). 'Every action is typed and schema-validated, so the LLM decides understanding, not what to do.'
- Trace-to-playbook extraction, stated as one pipeline: 'parses the trace into an ordered list of tool calls, detects the branch conditions the agent acted on, infers input and output schemas per step, builds a directed acyclic graph of tool dependencies, parameterizes instance-specific values such as device identifiers and timestamps, and marks human-approval points as explicit gates.' Framed as process mining where 'the event logs are agent execution traces and the recovered models are executable playbooks.'
- Promotion gates with exact thresholds. Type 3 -> 2: >=10 successful runs, zero safety violations, >=90% of runs produce an identical action sequence, all auto-generated acceptance tests pass, no human override in the recent window. Type 2 -> 1: >=50 successful hybrid runs, LLM classification consistency >=99%, a deterministic rule covers all observed input variation, full regression suite passes without LLM, human review of the deterministic logic.
- Acceptance tests come from the traces themselves: 'Acceptance tests are generated automatically from the successful traces, and the candidate Type 2 playbook must pass them.' Replaying recorded tool-call traces as fixtures is the regression suite.
- Demotion via circuit breaker: 'Each promoted playbook is monitored, and a circuit breaker demotes it to a higher execution type on execution failure, safety violation, or acceptance-test regression.'
- Results over 8 months, tens of thousands of incidents/month: Type 1 share 0% -> 45%, per-incident agent cost down >70% while incident volume doubled, >90% of common incidents resolved autonomously, false-positive remediation <5%.

**How to apply it.** This is the user's plan, run at scale and measured, so it mostly validates and sharpens the process rather than adding a mechanism: (1) record Claude Code MCP traces as ordered tool-call lists; (2) diff traces for the same incident type to find which argument values vary (Jira key, service name, trace ID, time window) -> those become the parameters of a playbook, everything else becomes constants; (3) where the agent branched (e.g. 'if slack search hit 0 results, grep codebase for the error string instead'), record the observed condition as a deterministic conditional; (4) the 'LLM classification consistency >=99%' gate is the right test for replacing any remaining LLM step (e.g. 'which substring is the trace ID') with a regex: run the regex against all recorded inputs and require it to agree with the agent's choice on all of them; (5) keep the Type 2 fallback for genuinely novel tickets so the frontend degrades to one small LLM call rather than failing. The replayed traces double as the regression suite for the extractors.

**Limitations.** Thin on mechanism: the paper gives the pipeline as one sentence and no algorithm for branch-condition detection, schema inference or parameterization, no example playbook, and no demotion thresholds. Domain is cloud network operations with typed device APIs, not free-text sources like Slack/Confluence, so the 'parameterize device IDs' step is easier there than substring extraction from log lines. A supplementary source that does describe trajectory-to-workflow induction is Agent Workflow Memory (arXiv 2409.07429): it prompts an LLM to 'find the repetitive subset of actions across multiple tasks, and extract each of them out as a workflow', replacing instance values with placeholders (e.g. 'Buy dry cat food on Amazon' -> 'search for a product on Amazon' with {product-name}); but AWM keeps the LLM in the execution loop, so it only informs the one-time induction step, not runtime.

**Quotes**

> The extraction algorithm parses the trace into an ordered list of tool calls, detects the branch conditions the agent acted on, infers input and output schemas per step, builds a directed acyclic graph of tool dependencies, parameterizes instance-specific values such as device identifiers and timestamps, and marks human-approval points as explicit gates.

> every action is typed and schema-validated, so the LLM decides understanding, not what to do.

> Each promoted playbook is monitored, and a circuit breaker demotes it to a higher execution type on execution failure, safety violation, or acceptance-test regression.

### Blueprint First, Model Second: A Framework for Deterministic LLM Workflow (arXiv 2508.02721)

- **Link:** <https://arxiv.org/html/2508.02721>
- **Evidence quality:** production-proven

**Key techniques**

- Execution Blueprint = expert-written graph in real source code, run by a deterministic engine; node kinds observed in the paper: tool-call nodes (jstat, jmap, log grep), conditional/routing nodes on parsed numeric fields (`if jstat_result.old_gen_usage > 0.9: heap_dump = execute_tool("jmap", pid=process_id)`), fork_join for parallel collection, correlate_join to cross-reference two result sets, knowledge-base/RAG match against past cases, precondition/postcondition guards as decorators (`@precondition(lambda ctx: not ctx["lb_api"].is_in_pool(ctx["target_host"]))`), validation nodes that reject malformed LLM output (`if 'intent' not in parsed or parsed['intent'] not in VALID_INTENTS: raise ValidationError`).
- Tool output flow: execute tool -> parse output into typed fields -> deterministic comparison picks the branch -> parsed data becomes the next node's parameters or LLM context -> schema-validate before proceeding. 'The routing decision is governed by deterministic code (old_gen_usage > 0.9), ensuring consistent workflow paths.'
- JVM heap-exhaustion production runbook, step by step: precondition host drained from LB (jmap causes stop-the-world); fork-join runs jstat, jmap (4.1 GB dump) and a surrounding-log scan in parallel; correlate_join joins heap suspects with log findings and a postcondition checks suspects exist; LLM node 1 synthesizes root cause from parsed heap data (found 'an unbounded SessionEntry cache ... 1.8 GB across 1.94M instances', confidence 0.86); KB match surfaces a past case ('a TTL configuration that defaulted to zero after a schema migration'); LLM node 2 writes the remediation hint. 'The LLM is invoked at only two nodes ... and never decides the diagnostic path.'
- Outcome numbers: draft ticket in ~5 min vs ~30 min manual, ~60% of drafts accepted unmodified, unsafe-action incidents 6/quarter -> 0; a second deployment (Android crash triage) cut false positives 30% -> 8% with 85% correct-team routing. Benchmark side (TravelPlanner): 35.56% pass vs 18.00% for the best agent baseline, 96% fewer constraint violations, 10.2 vs 14.0 steps.

**How to apply it.** Gives the shape of the frontend's execution layer once korrel8r-style rules have produced candidate queries: express each investigation as a blueprint graph in plain code (TypeScript in the browser or Python behind it) with tool-call nodes hitting MCP servers, parse nodes that pull typed fields out of results, numeric/string conditionals for branching ('if logz error count in window > 5x baseline'), fork_join to hit chronosphere + logz + slack in parallel from one (service, window) tuple, and correlate_join to intersect e.g. trace IDs found in Slack with those found in logz. Preconditions map onto 'don't run the expensive full-text Confluence search unless the cheap Jira-label lookup failed'. It also answers the 'minimal, one-time LLM' question concretely: the only LLM nodes worth paying for are final synthesis/summary of the collected evidence, not path selection or ID extraction, and a cheap validation node should reject any LLM output that isn't in the expected schema.

**Limitations.** Blueprints are hand-written by experts, not derived from traces, so it does not help with the 'LLM writes the extractors' step. The tools in the case study (jstat/jmap) emit well-structured numeric output; the paper never deals with extracting IDs from free text. Node types are described through code snippets rather than a formal spec, and the framework itself is not released as OSS as far as the paper shows.

**Quotes**

> The LLM is invoked at only two nodes-root-cause synthesis and remediation-hint generation-and never decides the diagnostic path.

> The routing decision is governed by deterministic code (old_gen_usage > 0.9), ensuring consistent workflow paths. Within each path, the LLM exercises full autonomy.

> Knowing a rule and being structurally unable to violate it are different properties

### Keep - open-source alert management with regex extraction rules and templated YAML workflows

- **Link:** <https://docs.keephq.dev/overview/enrichment/extraction>
- **Evidence quality:** production-proven

**Key techniques**

- Extraction rule = (target alert attribute, Python regex with named groups, optional CEL condition). Example: `Error (?P<error_code>\d+): (?P<error_message>.+) - \[UserID: (?P<user_id>\d+)\]` applied to the message field yields error_code, error_message, user_id as new attributes. 'Extracted values are added to the alert, enhancing its data with additional attributes.'
- Ordering semantics: 'The extraction process is designed to stop after the first successful match. Once a rule successfully applies and enriches the alert, no further rules are processed.' CEL conditions scope which rule is even tried (e.g. only for source == 'grafana').
- Workflow templating (docs/workflows/syntax/context): every later step can read earlier results with `{{ steps.<id>.results }}` and nested paths (`{{ steps.get-pods.results.0.status.phase == 'Running' }}` in an `if:`), plus `{{ alert.* }}`, `{{ incident.* }}`, `{{ consts.* }}`. `foreach: "{{ steps.get-alerts.results }}"` iterates and exposes `{{ foreach.value.name }}` inside the provider `with:` block - i.e. one downstream call per upstream hit, with the hit's fields interpolated into the provider's query params.
- Provider model: each integration (Jira, Slack, Prometheus, Datadog, GitHub, Loki...) is a provider with typed `query`/`notify` methods; workflow YAML = triggers -> steps (queries) -> actions (side effects), so extracted attributes flow into provider queries purely by template substitution.

**How to apply it.** Keep supplies the missing piece korrel8r lacks: the regex-with-named-groups extractor applied to a chosen free-text field, with first-match-wins ordering and a guard condition. That is exactly the shape the one-time LLM should emit for 'pull the trace ID / request ID / customer ID / error code out of this Slack message / logz line / Confluence paragraph': a small ordered list of {when: <condition on source/tool>, on: <field>, regex: <named groups>} rules whose captures become new attributes on the entity. Keep's `{{ steps.x.results.0.field }}` + `foreach` templating is the reference syntax for the frontend's step-chaining layer (e.g. foreach slack hit -> logz query with `{{ foreach.value.trace_id }}`). Helps directly with slack, logz.io, confluence, jira, pagerduty (extraction) and with chronosphere/prometheus and git (templated queries).

**Limitations.** Keep is a self-hosted server with Postgres and a workflow engine; not embeddable in a browser page, so it is a design reference only. Extraction rules are flat regexes on one attribute at a time with no scoring or multi-candidate handling - if a log line contains three UUIDs you get whichever the pattern hits first, so 'pick the right substring' still has to be encoded by careful patterns or post-filters. The docs are terse about rule priority beyond first-match.

**Quotes**

> The extraction process is designed to stop after the first successful match. Once a rule successfully applies and enriches the alert, no further rules are processed.

> Error (?P<error_code>\d+): (?P<error_message>.+) - \[UserID: (?P<user_id>\d+)\]

> if: "{{ steps.get-pods.results.0.status.phase == 'Running' }}"

### Grafana Sift - fixed catalog of deterministic diagnostic checks keyed on labels and time range

- **Link:** <https://grafana.com/docs/grafana-cloud/machine-learning/sift/analyses/error-pattern-logs/>
- **Evidence quality:** production-proven

**Key techniques**

- Investigation scope = a label set + a time range, nothing else. Labels: 'Sift will still work best with cluster and namespace labels'; 'Sift uses the provided labels to identify the scope of investigation'. Time range is inherited from context: 'When a Sift investigation is triggered from within an incident, the Timerange is automatically set to the incident start time through the time investigation is triggered'; from Explore/dashboards it 'will extract labels from the query and use the current ... time range'.
- Error Pattern Logs check, concretely: default LogQL filter `!~ "debug|DEBUG|info|INFO" |~ "error|ERROR"` on an auto-discovered Loki datasource; lines are grouped into patterns ('grouping similar log lines together' and counting occurrences per pattern); each pattern's rate in the window is compared with its rate 'before the investigation time range'; a pattern is interesting only if count >= minimum (default 5, range 1-10) AND its rate increased; output is 'the log lines for each pattern found, along with the number of occurrences and the percentage increase', with up to N example lines (default 3).
- Fixed catalog of other checks, each a parameterized query on the same (labels, window): HTTP Error Series (elevated HTTP error rates for cluster/namespace), Kube Crashes (from k8s metrics, reason Error vs OOMKill), Recent Deployments (workloads changed in window), Noisy Neighbors (host load > CPU cores, then which pods on the host), Resource Contention (CPU throttling at limits, packet drops), Slow Requests (traces over a threshold, default 3 s), plus user-defined recurrent Log Query (LogQL) and Metric Query (PromQL) with templated output.
- Trigger/plumbing: 'You can trigger a new Sift investigation as part of an OnCall escalation chain, so you can have an automatic investigation of every single alert group. Sift will even post the results back to the resolution notes of the alert group'. Results appear as a 'Suggestions' list of clickable findings and as annotations on dashboards via the Sift panel.

**How to apply it.** Defines the deterministic 'second stage' for chronosphere/prometheus and logz.io once the graph stage has resolved a Jira/PagerDuty record to (service or namespace labels, incident window): run a fixed catalog of checks in parallel, each a templated query over those labels and window, each with its own interestingness rule. The Error Pattern Logs recipe is directly reimplementable in-process against logz.io: fetch error-level lines for the window and for an equal-length pre-window, mask digits/UUIDs/hex to get a pattern key (or use a Drain-style template miner, which is a pure library), count per pattern, keep patterns with count >= 5 and rate ratio > 1, show 3 examples. Recent Deployments maps to arc/git commits touching the service in the window; Slow Requests/HTTP Error Series map to PromQL over chronosphere. The 'window inherited from the incident' rule is the right default for time-bounding every search the frontend renders.

**Limitations.** Closed product; the docs describe behaviour, not the pattern-grouping algorithm or the rate-increase threshold, and there is no code to copy. Everything is keyed on Kubernetes cluster/namespace labels and Loki/Prometheus/Tempo; it does not navigate between systems (no Jira -> Slack -> logs hop), so it is only the leaf-check pattern, not the graph. Never states whether any ML is used, though every documented check is a deterministic query plus threshold.

**Quotes**

> Sift uses the provided labels to identify the scope of investigation and discover issues.

> When a Sift investigation is triggered from within an incident, the Timerange is automatically set to the incident start time through the time investigation is triggered.

> The analysis will show the log lines for each pattern found, along with the number of occurrences and the percentage increase

## All sweep findings

_18 findings, sorted by relevance score._

### Korrel8r - rule-based correlation engine over observability signals (Red Hat / OpenShift)

- **Link:** <https://github.com/korrel8r/korrel8r>
- **Kind:** oss-project
- **Relevance:** `█████████░` 9/10

**What it is.** Korrel8r models each backend (k8s API, Prometheus alerts/metrics, Loki logs, Tempo traces, netflow, incidents) as a 'domain' with 'classes', and the whole thing as a graph. Edges are YAML rules: a start class, a goal class, and a Go text/template that takes a start object and renders a query in the goal domain's own query language, e.g. AlertToPod renders `k8s:Pod.v1:{namespace: "{{index .Labels "namespace"}}", name: "{{index .Labels "pod"}}"}` and PodToLogs renders a Loki selector from pod metadata; a template that returns blank or errors simply skips the rule. On top of the rules it offers 'neighbourhood search' (everything reachable in N hops) and 'goal search' (find paths from an alert to logs), and exposes an MCP interface. No LLM anywhere; the sample YAML rules live in etc/korrel8r/rules/_samples/*.yaml (the default set is now compiled in as quicktemplate 'quickrules').

**Why it matters here.** This is the closest existing realization of 'treat the tool servers as a graph database with an odd query language'. The rule shape (start-class -> template -> goal-domain query string, skip-on-missing-field) is exactly what the user's LLM-written extractors need to emit for jira->slack->confluence->chronosphere->logz. Its goal/neighbourhood search is a ready-made algorithm for 'which tool next' without an LLM. Kubernetes-centric, so the domains would need reimplementing, but the design is directly copyable in-process.

### Progressive Crystallization: Turning Agent Exploration into Deterministic, Lower-Cost Workflows in Production (arXiv 2607.07052)

- **Link:** <https://arxiv.org/abs/2607.07052>
- **Kind:** paper
- **Relevance:** `█████████░` 9/10

**What it is.** Describes a production AIOps system (cloud networking, tens of thousands of incidents/month) with a three-type taxonomy: fully agent-orchestrated, hybrid, fully deterministic. Agent traces are parsed into an ordered list of tool calls, branch conditions are detected, per-step input/output schemas inferred, a DAG of tool dependencies built, and instance-specific values (device IDs, timestamps) parameterized - explicitly framed as process mining where 'the event logs are agent execution traces and the recovered models are executable playbooks'. Promotion criteria are concrete (hybrid->deterministic: >=50 successful runs, >=99% LLM classification consistency, deterministic rule covers all observed input variation, regression suite passes without LLM, human review); workflows demote back to LLM when they regress. Result: deterministic share went 0%->45% in 8 months, agent cost down >70% while incident volume doubled.

**Why it matters here.** This is the user's plan, already run in production and measured: record agent traces, extract deterministic playbooks with parameterized IDs, keep LLM as fallback for novel incidents. Gives concrete promotion thresholds and the trace-normalization steps (parameterize IDs/timestamps, infer schemas, DAG) to copy.

### Blueprint First, Model Second: A Framework for Deterministic LLM Workflow (arXiv 2508.02721)

- **Link:** <https://arxiv.org/abs/2508.02721>
- **Kind:** paper
- **Relevance:** `████████░░` 8/10

**What it is.** 'Source Code Agent' framework: an expert-written Execution Blueprint (a graph of deterministic nodes for control flow, tool calls, fork_join, correlate-join, knowledge-base match) is executed by an engine; the LLM is invoked only at bounded nodes and 'never decides the diagnostic path'. The production case is a JVM heap-exhaustion on-call runbook: precondition check host is drained, run jstat, `if jstat_result.old_gen_usage > 0.9` collect heap dump, parse jmap and grep logs in parallel, correlate, match against past cases, then exactly two LLM nodes (root-cause synthesis, remediation hint). Draft ticket in ~5 min vs ~30 min manual; ~60% of drafts accepted unmodified.

**Why it matters here.** Shows the target architecture for the web frontend: deterministic graph with parsed tool outputs feeding conditionals and the next tool's params, LLM optional and confined to summarization at the end. Also a useful argument for where a single cheap LLM call still earns its keep (final synthesis) vs where it doesn't (path selection).

### Keep - open-source alert management with regex extraction rules and templated YAML workflows

- **Link:** <https://github.com/keephq/keep>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Keep ingests alerts and enriches them with 'extraction rules': a Python regex with named groups applied to a chosen alert attribute (docs example: `Error (?P<error_code>\d+): (?P<error_message>.+) - \[UserID: (?P<user_id>\d+)\]`), optional CEL condition to scope the rule, first-match-wins, extracted fields become alert attributes. Workflows are YAML (triggers -> steps -> actions) over providers (Jira, Slack, Prometheus, Datadog, GitHub...), with `{{ alert.customer_id }}` / `{{ steps.<id>.results.<field> }}` templating to pipe one step's output into the next step's parameters, foreach over results, and conditions. Also has mapping/topology enrichment and correlation rules. (Docs: https://docs.keephq.dev/overview/enrichment/extraction and /workflows/syntax/context)

**Why it matters here.** The most complete OSS example of 'regex-extract IDs from free text, then template them into other tools' query params' chained across the exact integrations the user has. Caveat: Keep is a self-hosted server with a Postgres backend, so it is more a design reference (extraction rule + workflow context syntax) than something to embed in a browser-side frontend.

### Grafana Sift - fixed catalog of deterministic diagnostic checks keyed on labels and time range

- **Link:** <https://grafana.com/docs/grafana-cloud/machine-learning/sift/analyses/>
- **Kind:** product
- **Relevance:** `███████░░░` 7/10

**What it is.** Sift runs an 'investigation' scoped by labels (works best with cluster+namespace, arbitrary labels supported) and a time range, and executes a fixed set of checks with no LLM: Error Pattern Logs (query Loki for error-level lines, group similar lines into patterns, count, compare rate to the pre-investigation baseline, report patterns with increased rate plus 3 example lines, min count 5), HTTP Error Series, Kube Crashes (OOMKill vs error), Recent Deployments, Noisy Neighbors, Resource Contention, plus arbitrary saved LogQL/PromQL 'recurrent queries'. Investigations can be auto-triggered from an OnCall escalation chain for every alert group, and findings are overlaid as annotations on the metric timeline.

**Why it matters here.** A production template for the deterministic 'checks' the frontend can run against chronosphere and logz.io once it has (service, namespace, time window) from a Jira/PagerDuty record: pattern-group error logs vs baseline, recent deploys, error-rate series. Also validates that a curated check catalog, not a free-form agent, covers most of the first pass.

### Sentry Suspect Commits + Ownership Rules - deterministic stack-trace -> commit -> owner linking

- **Link:** <https://docs.sentry.io/product/issues/suspect-commits/>
- **Kind:** product
- **Relevance:** `███████░░░` 7/10

**What it is.** Suspect commits: collect in-app frames from the stack trace (first in-app frame top-down is primary), map file paths to repo paths via code mappings, git-blame the exact file+line, and treat the most recent commit as suspect if it is <1 year old; fallback to release-associated commits. Ownership rules (https://docs.sentry.io/product/issues/ownership-rules/) are glob rules of the form `path:src/api/* #backend-team`, `url:`, `module:`, `tags.browser:`, plus an imported CODEOWNERS; evaluated top-to-bottom, last match wins, producing suggested assignees.

**Why it matters here.** Fully deterministic chain from an unstructured artefact (a stack trace in a log line) to code, commit, PR and owning team - the same shape as 'log line -> codebase grep -> git blame -> team Slack channel'. The glob-rule + code-mapping design is trivially reproducible in-process with the user's arc/git and codebase tools.

### Meta: heuristic retriever + LLM ranker for root-cause code changes

- **Link:** <https://engineering.fb.com/2024/06/24/data-infrastructure/leveraging-ai-for-efficient-incident-response/>
- **Kind:** blog
- **Relevance:** `███████░░░` 7/10

**What it is.** Meta's investigation assistant narrows thousands of candidate diffs to a few hundred with deterministic heuristics only - code and directory ownership of impacted systems, the runtime code graph, and time - then a fine-tuned Llama ranks 20 at a time in an 'election' down to top 5. 42% of investigations had the root cause in the top five.

**Why it matters here.** Clean published example of what is deterministic (ownership, dependency graph, time window filtering of changes) versus what needed a model (ranking by semantic relevance). For the user, the retriever half can be done with git log + CODEOWNERS + time window today; the ranker is where a one-time or cheap LLM pass may still be wanted.

### RCACopilot: Automatic Root Cause Analysis via LLMs for Cloud Incidents (Microsoft, EuroSys'24, arXiv 2305.15778)

- **Link:** <https://arxiv.org/abs/2305.15778>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Incoming incidents are matched by alert type to a predefined 'incident handler' that deterministically runs a per-type diagnostic data-collection workflow (queries across logs, metrics, traces, tickets), and only then an LLM summarizes (120-140 words) and predicts a root-cause category. The paper states the handler-based diagnostic collection component had been in production at Microsoft for over four years before any LLM was added.

**Why it matters here.** Direct evidence that the 'which data to fetch for this alert type' step is routinely encoded by hand as deterministic handlers keyed on alert type, with LLM used only for the final narrative. Supports building per-ticket-type handlers rather than a generic agent.

### anthropics/oncall-kit - mine incident history into playbooks with provenance, validate on held-out incidents

- **Link:** <https://github.com/anthropics/oncall-kit>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Starter kit that mines 30-90 days of pager history, alert-channel Slack threads and postmortems, clusters them into failure classes, and drafts one playbook per class (skills/triage/references/<failure-class>.md) with provenance tags such as '(seen 2x, unverified)'; routing is proposed from CODEOWNERS; STACK.md is a capability->tool map (Grafana, PagerDuty...) so playbooks stay vendor-neutral. Setup is gated: Phase 3 replays 5-10 held-out incidents and grades blind, requiring >=70% pass and zero harmful answers before going live. At runtime Claude still classifies and investigates, read-only.

**Why it matters here.** Same 'one-time LLM mining of history, then gated validation against held-out incidents' loop the user wants, with a concrete repo layout and provenance convention. The runtime still uses an LLM, so it is the 'before' state of the user's plan, but the mining/validation phases are reusable as-is.

### Tool-Making and Self-Evolving LLM Agents in Low-Latency Systems (arXiv 2607.08010)

- **Link:** <https://arxiv.org/abs/2607.08010>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** A tool-maker collects execution traces from live operations, observes backend schemas and values, synthesizes candidate deterministic tools for steps that repeat, and repairs them against labeled cases; at runtime the agent calls compiled tools and only generates code for novel situations. Evaluated on a fulfillment-center alarm-triage agent working through a 44-node SOP over heterogeneous metric backends: p50 latency -42%, end-to-end error rate down up to 53% by 'suppressing run-to-run variance in repeated steps'. Also discusses translating troubleshooting guides into tools.

**Why it matters here.** Second independent paper (with Progressive Crystallization) on compiling repeated agent steps into deterministic tools from traces plus backend schemas - the user's 'LLM writes extractors/query generators' step - with numbers showing determinism also improves accuracy, not just cost.

### PagerDuty AIOps: Related Incidents, Recent Changes / Change Correlation

- **Link:** <https://support.pagerduty.com/main/docs/related-incidents>
- **Kind:** product
- **Relevance:** `██████░░░░` 6/10

**What it is.** Related Incidents marks two incidents related deterministically when they trigger within five minutes and one service directly depends on the other (or they share a parent business service), supplemented by an online ML model over creation-time proximity, alert-metadata similarity and thumbs-up/down feedback; shows up to 20. Change correlation (AIOps quickstart / Recent Changes docs) surfaces change events on the same service in the past 24 hours, on dependent services, or via ML. PagerDuty's SRE Agent doc lists what it gathers automatically: event/alert payload, historical and related incidents, change events, logs from Grafana/Datadog/CloudWatch, and pre-configured runbooks from Confluence/GitHub, with the LLM only doing prioritization and narrative.

**Why it matters here.** Provides simple, proven correlation primitives to implement without a model: time-window plus service-dependency joins, and 'changes in last 24h on this or dependent services'. The SRE Agent's gather list doubles as a checklist of deterministic fetches to run before any reasoning.

### Cleric - 'The hidden complexity of building an AI SRE' (what they made deterministic)

- **Link:** <https://cleric.ai/blog/the-hidden-complexity-of-building-an-ai-sre>
- **Kind:** blog
- **Relevance:** `██████░░░░` 6/10

**What it is.** Cleric describes building an implicit service map ('not just the official dependencies'), running 50+ queries per alert, exploring hypotheses in parallel, and a confidence score that is 'a compound score from dozens of factors' which 'heavily favors deterministic signals, like the topological locality of evidence, the amount of independent sources of evidence'. The ZenML writeup of their talk adds that the knowledge graph has layers updated 'using deterministic methods (like walking a Kubernetes cluster with kubectl)', episodic memory is extracted from Slack threads as 'contextual containers' from problem to solution, and per-investigation token caps (e.g. 10 cents to $1) are enforced. Resolve AI similarly states it queries its 50k-node graph through a custom DSL ('Gragg') rather than free text.

**Why it matters here.** Vendor map of deterministic vs LLM: topology building, evidence-locality scoring, budget caps and Slack-thread episode extraction are deterministic; hypothesis selection is LLM. Their 'Slack thread = problem-to-solution container' is a useful framing for the user's trace mining.

### Traversal Causal Indexer - deterministic log-pattern compression before any LLM

- **Link:** <https://www.traversal.com/blog/blog-causal-indexer-agentic-incident-root-cause-analysis-without-tokenmaxxing>
- **Kind:** blog
- **Relevance:** `██████░░░░` 6/10

**What it is.** Traversal argues against 'tokenmaxxing' (dumping raw telemetry into context). Its Causal Indexer continuously reduces routine log lines matching known patterns to 'a pattern, a count, and a baseline rate', keeps deviations at full fidelity ('what changed, by how much, and which upstream service moved first'), and materializes entity dependency edges; claims up to 1000x reduction (50,000 log lines/hour -> ~50 anomalous entries plus dependency metadata). Agents then reason only over the compressed index.

**Why it matters here.** Instructive for the token-budget constraint: pattern-mine and baseline logs deterministically (in-process Drain-style templating is enough) so that whatever LLM use remains sees only anomalies. Also a reminder that the entity/edge extraction from logs is done by code, not by the model.

### Backstage well-known annotations - entity holds foreign IDs, plugins fan out

- **Link:** <https://backstage.io/docs/features/software-catalog/well-known-annotations/>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** Backstage catalog entities carry annotations that are foreign keys into other systems: `github.com/project-slug`, `sentry.io/project-slug`, `jenkins.io/job-full-name`, `sonarqube.org/project-key`, plus plugin-defined ones such as `pagerduty.com/service-id`, `jira/project-key`, `grafana/dashboard-selector`. Each plugin on the entity page uses its annotation to query its system, producing the 'single pane of glass' service page. incident.io's Catalog (https://incident.io/blog/announcing-catalog) is the same idea with typed attributes referencing other types and workflow expressions that navigate feature -> owning team -> PagerDuty service; Cortex's events page overlays deploys, commits, K8s events, Sentry events on the incident timeline by service ID.

**Why it matters here.** Establishes the pattern the user's frontend needs: a small local entity table (service -> jira project, slack channel, pagerduty service, chronosphere labels, logz index, repo path) so most cross-tool hops are key lookups, and only the residue needs fuzzy extraction. A catalog-info.yaml-style file in sqlite satisfies the no-extra-server constraint.

### Robusta - YAML playbooks: Prometheus alert triggers, deterministic enrichers, change tracking

- **Link:** <https://github.com/robusta-dev/robusta>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Robusta receives Prometheus/Alertmanager webhooks and runs YAML playbooks (trigger -> actions) that deterministically enrich the alert with pod logs, events, graphs and recent Kubernetes changes ('correlate alerts with changes to your infrastructure or applications' via automatic change tracking), route to Slack/Jira/PagerDuty, and optionally auto-remediate. The AI investigation layer (HolmesGPT) is a separate, optional component layered on the deterministic enrichment.

**Why it matters here.** A working OSS split between deterministic enrichment (fetch the logs/changes for the alert's labels, post to Slack) and optional LLM investigation. HolmesGPT's 'tool output transformers' and server-side filtering are also worth noting as deterministic pre-processing that shrinks tool results before any model sees them. Kubernetes-only.

### Netflix Winston - event-driven diagnostic runbooks on StackStorm (2015)

- **Link:** <https://netflixtechblog.com/introducing-winston-event-driven-diagnostic-and-remediation-platform-46ce39aa81cc>
- **Kind:** blog
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Winston hosts Python runbooks triggered by alerts; it 'runs pre-defined diagnostics and prepares a report for the on-call engineer' so that on login they already have discovery status and recent exceptions. Built on StackStorm (sensors -> rules with Jinja criteria -> ActionChain/Orquesta workflows that pass each action's output into the next, with ChatOps posting to Slack); Netflix cited FBAR (Facebook), Nurse (LinkedIn) and Naoru (Dropbox) as prior art. (Medium blocked direct fetch; confirmed via StackStorm case study https://stackstorm.com/case-study-netflix/ and search snippets.)

**Why it matters here.** Historical precedent that Tier-1 diagnostic gathering for on-call was automated without any model a decade ago; StackStorm's rule/action-chain data passing is a mature reference for the 'output of tool A templated into tool B' engine.

### ARGUS: MCP-Grounded Root Cause Analysis for Kubernetes Incidents (arXiv 2608.23084)

- **Link:** <https://arxiv.org/abs/2608.23084>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** RCA system that reaches Kubernetes state, Prometheus, Loki and NATS exclusively through MCP servers, with a tool-selection policy, an evidence graph and structured extraction on the deterministic side and diagnosis/remediation on the LLM side. Reports an 'MCP success ratio' of 0.91 and root cause found in all ten injected-fault scenarios; practitioners trusted diagnoses but not recommended fixes.

**Why it matters here.** Confirms the framing of MCP servers as the query surface for investigation and introduces an 'evidence graph' assembled from tool results - adjacent but useful vocabulary; still LLM-in-the-loop.

### unSkript Awesome-CloudOps-Automation - Jupyter runbooks of chained Python actions

- **Link:** <https://github.com/unskript/Awesome-CloudOps-Automation>
- **Kind:** oss-project
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Open-source runbook engine where a runbook is a Jupyter notebook of atomic 'Actions' (Python functions with typed inputs/outputs) against connectors for Prometheus, Grafana, Jira, GitHub, AWS, Kubernetes, etc.; notebooks can run fully automated or step-by-step and pass outputs between cells.

**Why it matters here.** Lower-tech but pragmatic packaging of investigation playbooks as code with a large action library; useful if the user wants an interactive, human-steppable format for the deterministic playbooks rather than a pure web UI.

## Dead ends

_Queries and directions that produced nothing useful. Listed so nobody repeats them._

- site:news.ycombinator.com queries (three phrasings: on-call automation without LLM, AI SRE deterministic runbook, Show HN incident investigation) returned only generic Jira/Slack rant threads - nothing on deterministic investigation tooling
- Korrel8r doc pages korrel8r.github.io/korrel8r/user-guide/ and the raw rules/k8s.yaml 404 - default rules were moved into the compiled binary; the only readable examples are etc/korrel8r/rules/_samples/*.yaml and doc/content/docs/writing-rules.md
- Sentry blog post on suspect commits via git blame is marketing-only; the algorithm is only in docs.sentry.io/product/issues/suspect-commits/
- Resolve AI knowledge-graph blog and Traversal 'Causal Search Engine' blog give no ingestion or pipeline detail beyond claims (graph size, 10,000 parallel hypotheses); Datadog Bits docs overview page has no deterministic-check list
- Netflix Winston techblog (medium) returned 403; StackStorm case study lacks technical detail
- PagerDuty support doc 'change-correlation' URL 404s; the substance is in the AIOps quickstart and 'Recent Changes' pages
- FireHydrant/Blameless/Rootly 'runbooks' are process automation (create channel, page, open Jira ticket) not investigation-data chaining; Rootly AI pages are marketing only
- OpenSRE (Tracer-Cloud) is an LLM-driven RL environment; its deploy correlation is via LLM reasoning, no deterministic correlators found
- Sourcegraph/Backstage 'JIRA-123 smart commit' regex-linking query found only the deprecated sourcegraph-jira extension
- SPL (arXiv 2607.07727) is a deterministic/probabilistic composition language but oriented at symbolic math, not ops; noted but not scored
