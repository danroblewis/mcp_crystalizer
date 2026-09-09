# Tool graph linking: declaring how one result feeds the next tool

_Research angle `tool-graph-linking`. Generated from the research workflow run on 2026-09-09._

## Angle verdict

Yes - this angle yields a real, reusable pattern, and all five sources converge on the same shape from different directions. Call it the "correlation link table over lazy tool adapters": a checked-in table of rows (source tool, source field/pointer, extractor, target tool, parameter template, applicability condition) that turns MCP tool results into a navigable graph. Grafana Correlations is the productized, config-only version of one hop (regex named groups or logfmt over a long field, interpolated into a target query template, link shown only when all variables resolve); RESTler shows how to bootstrap the rows automatically from schema name-matching plus a tiny annotation file for the rest, and how to compile them into writer/reader variables at run time; OpenAPI Links gives the standard vocabulary and runtime-expression grammar for the structured-ID cases; Arazzo gives the serialization for a whole recorded trace as a multi-step workflow with success criteria and branching, plus an existing MIT runtime (arazzo-runner). Trustfall is the execution engine that makes the graph queryable in-process (WASM in the browser satisfies the no-server constraint) with @filter/@optional/@fold/@recurse standing in for the agent's fuzziness. Two gaps recur in every source and are where the one-time LLM effort should go: none of the specs has regex/substring extraction into outputs, and none has array fan-out (`[*]`), so the MCP link schema must add an `extract` transform and a `forEach` binding.

## Deep reads

### Arazzo Specification v1.1.0 (OpenAPI Initiative) + Jentic arazzo-runner reference runtime

- **Link:** <https://spec.openapis.org/arazzo/latest.html>
- **Evidence quality:** spec

**Key techniques**

- Step object: `stepId`, one of `operationId` / `operationPath` / `workflowId` (sub-workflow call), `parameters` (array of {name, in, value}), `successCriteria` (all must pass), `onSuccess`/`onFailure` action lists (goto step / retry / end), `outputs` (map name -> runtime expression or Selector Object).
- Runtime expression grammar is a fixed set of `$`-sources: `$inputs.<name>`, `$steps.<stepId>.outputs.<name>`, `$response.body#<json-pointer>`, `$response.header.<n>`, `$request.path|query|header.<n>`, `$workflows.<id>.outputs.<n>`, `$components.parameters.<n>`; any of these can be embedded in a string as `{expr}` (scalars stringified, objects serialized as JSON) - this is how you build a free-text search query from earlier outputs.
- Criterion object has four types: `simple` (boolean expression with <,==,&&,|| etc), `regex`, `jsonpath` (RFC 9535), `xpath`; the last three take a `context` runtime expression, and the condition string may itself embed `{expr}` before evaluation.
- Outputs use a Selector Object `{context, selector, type: jsonpath|xpath|jsonpointer}` - JSON Pointer or JSONPath only; regex extraction is NOT available in outputs (only as a pass/fail criterion). No loop construct: the spec has no fan-out over arrays; you get array elements only by index or JSONPath filter.
- `sourceDescriptions` declare the APIs (openapi/asyncapi/arazzo) a workflow spans; `components` hold reusable parameters/inputs/actions. Real executors exist: Jentic `arazzo-runner` (PyPI, MIT) is composed of an OpenAPIOperationExecutor -> StepExecutor -> WorkflowExecutor that 'iterates a workflow's steps, owns the run state, and interprets control-flow actions'; Speakeasy's Go library and pb33f libopenapi only read/walk/validate, they do not execute.

**How to apply it.** Use Arazzo as the on-disk shape of a 'compiled agent trace': the one-time LLM pass turns a recorded Claude Code trace (jira.get_issue -> slack.search -> grep -> confluence.search -> chronosphere.query + logz.search) into a workflow YAML where each step names an MCP tool instead of an operationId, `parameters` bind from `$steps.prev.outputs.x`, and search-string params are templated strings like `"{$steps.jira.outputs.service} error {$steps.jira.outputs.env}"` for logz/slack/confluence search. The interpreter is ~200 lines (evaluate `$` expressions, call MCP tool, run criteria, pick next step). Two extensions are mandatory for this problem: an `extract:` output type with a regex + named groups (for pulling IDs out of log lines / slack text), and a `forEach` / fan-out step so a jira ticket's N linked PRs or a slack search's N hits each drive downstream steps.

**Limitations.** Outputs are JSON-pointer/JSONPath only (no regex capture), no iteration over arrays, no scoring/ranking between candidate next steps, and it is HTTP/OpenAPI-oriented (parameters have an `in: path|query|header`), so the tool-call adaptation is yours to write; existing runners assume OpenAPI docs, not MCP tool schemas.

**Quotes**

> outputs: Map of `string` to runtime expression or Selector Object

> Regex: Requires `type: regex` and `context` (runtime expression); `condition` is a regex pattern; passes if pattern matches context value

> No native loop construct. The specification does not define iteration over arrays in step definitions.

### Grafana Correlations: configuration, variables and transformations

- **Link:** <https://grafana.com/docs/grafana/latest/administration/correlations/correlation-configuration/>
- **Evidence quality:** production-proven

**Key techniques**

- Data model of one correlation: source data source, `Results field` ('Defines where the link is shown in a visualization'), target type `query` (run a query in another datasource, shown in split view) or `external` (open a URL), `Target query` template, optional `transformations` list.
- Every column of the clicked source row is automatically a variable (`${fieldname}`, or verbosely `${__data.fields.fieldname}`); the target query/URL is a template interpolated with those variables. A link is only rendered when every variable it needs has a value in that row ('Correlation creates a data link only if all variables have values in the selected data row').
- Transformation `type: regex` with `field`, `expression` (named capture groups become variables), and optional `mapValue` ('the first matching is mapped to a new variable' under that name). Example from the docs: `expression: service=(\w+)\.\w+`, `mapValue: application`, then target uses `${application}`.
- Transformation `type: logfmt` on a field: parses `level=error message=error service=app1.loginService` into variables `level`, `message`, `service` with no regex.
- Provisioned as YAML per datasource (`correlations: - targetUID, label, description, config: {type, field, target, transformations}`), i.e. the whole graph of cross-datasource links is a checked-in file, and the UI surfaces it as clickable links on values inside log lines and table cells.

**How to apply it.** This is the exact 5-tuple to copy for the MCP explorer: (source tool, source field, extractor[regex named groups | logfmt | json pointer], target tool, param template). Examples for the listed tools: logz.io `message` -> regex `(?<trace_id>[0-9a-f]{32})` -> chronosphere/logz search template; slack message `text` -> regex `(?<jira_key>[A-Z]+-\d+)` -> jira.get_issue; jira `summary` -> regex `(?<service>[a-z-]+-service)` -> codebase grep `service`, confluence search `"{service} runbook"`, pagerduty service lookup; gcloud/azure resource names -> chronosphere label queries. The 'all variables must have values' rule is a cheap deterministic version of the agent's 'is this tool applicable now' decision, and the Grafana UI model (values in a result become links whose targets are enumerated from the correlation table) is precisely the browser frontend the developer wants: no LLM at click time, the LLM's one-time job is to write the correlation rows from the recorded traces.

**Limitations.** Single-hop only (a correlation is one click from one row to one query), first-match regex, no chaining/planning, no scoring among multiple candidate links, and the source is a tabular data frame rather than nested JSON; you must add a JSONPath/pointer 'field' selector and a way to chain hops.

**Quotes**

> a correlation defines how data in one data source is used to query data in another data source or to generate an external URL

> Correlation creates a data link only if all variables have values in the selected data row

> - type: regex\n  field: msg\n  expression: service=(\w+)\.\w+\n  mapValue: application

### Trustfall - in-process query engine; BasicAdapter trait and HackerNews adapter

- **Link:** <https://docs.rs/trustfall_core/latest/trustfall_core/interpreter/basic_adapter/trait.BasicAdapter.html>
- **Evidence quality:** production-proven

**Key techniques**

- An adapter is four functions over a schema: `resolve_starting_vertices(edge_name, parameters) -> VertexIterator` (root edges like `Top(max: Int)`, `User(name: String!)` - parameters are how you inject a search query), `resolve_property(contexts, type_name, property_name)`, `resolve_neighbors(contexts, type_name, edge_name, parameters) -> (context, VertexIterator)` and `resolve_coercion(contexts, type_name, coerce_to_type)`; all must yield results 'in the same order as the input contexts iterator'.
- Neighbors are resolved lazily per vertex: the HackerNews example's `Story.comment` edge does `story.kids.into_iter().filter_map(|id| get_client().get_item(id))` inside a boxed iterator, so an HTTP call happens only when the query actually pulls that comment. Edges from a vertex can take parameters too (`resolve_neighbors` receives `EdgeParameters`), which is the hook for 'search B using text from A'.
- Schema is GraphQL SDL with vertex types, interfaces (`Item`, `Story implements Item`), typed edges (`byUser: User!`, `reply: [Comment!]`, `parent: Item!`) and parameterized root edges; a single query spans adapters, e.g. HN story -> `link` -> `GitHubRepository` -> `workflows` -> YAML file jobs, with 'the transition ... seamless - it isn't visible from the query'.
- Query directives give you the agent's flexibility declaratively: `@filter(op: ">=", value: ["$min_score"])` (also regex ops), `@optional` (tolerate missing hop), `@fold` + `@transform(op: "count")` (aggregate), `@recurse(depth: N)` (follow parent/child chains), `@tag` (bind a value from one branch to filter another branch).
- Runs in-process: Rust core, Python bindings, WASM build powering the browser playground (play.predr.ag); production use in cargo-semver-checks (2000x speedup post).

**How to apply it.** Model each MCP server as an adapter and each MCP tool as either a root edge (`JiraIssue(key: String!)`, `SlackSearch(query: String!)`, `LogzSearch(query: String!, from: String)`, `CodeGrep(pattern: String!)`) or a neighbor edge on a vertex (`JiraIssue.slackThreads`, `SlackMessage.mentionedIssues`, `LogLine.traceSpans`, `Service.runbooks`, `PagerDutyIncident.jiraIssue`). The resolver for `JiraIssue.slackThreads` is deterministic code that formats a slack search string from the issue's key/summary and calls the MCP tool; regex extraction of IDs from `text` lives in the resolver that produces `mentionedIssues`. The one-time LLM job is 'write the GraphQL schema + resolver bodies from the traces'; afterwards, an on-call investigation is one saved Trustfall query executed with no LLM, and the WASM build means the frontend can run it browser-side with no extra server. Satisfies the hard constraint outright.

**Limitations.** Requires writing real adapter code per MCP server (not just config), Rust/Python/WASM only (no first-class JS adapters outside WASM), no built-in string-extraction directive (extraction is resolver code), and edge resolvers issue one tool call per vertex unless you batch by hand - watch MCP call volume on fan-out.

**Quotes**

> Trustfall is a query engine for querying any kind of data source, from APIs and databases to any kind of files on disk

> fn resolve_neighbors<V>(&self, contexts: ContextIterator<'vertex, V>, type_name: &str, edge_name: &str, parameters: &EdgeParameters) -> ContextOutcomeIterator<'vertex, V, VertexIterator<'vertex, Self::Vertex>>

> The easiest way to plug in a new data source is by implementing the BasicAdapter trait.

### RESTler: producer-consumer dependency inference, annotations file, and reader/writer variables

- **Link:** <https://github.com/microsoft/restler-fuzzer/blob/main/docs/user-guide/Annotations.md>
- **Evidence quality:** production-proven

**Key techniques**

- Dependency inference from the spec alone: 'inferring that a resource included in the response of a request A is necessary as input argument of another request B, and therefore that A should be executed before B' - done by matching response property names to path/query/header/body parameter names (compiler switches `ResolveBodyDependencies`, `ResolveQueryDependencies`, `ResolveHeaderDependencies`, `AllowGetProducers`).
- Compiled grammar makes the link explicit as writer/reader variables: the producer request carries `'post_send': {'parser': parse_posts, 'dependencies': [post_id.writer()]}` and the consumer uses `dependencies.set_var(post_id)` in place of the path segment; at run time EXECUTE is 'extracting and memoizing dynamic objects (if any), and providing those in subsequent requests in the sequence if needed, as determined by the dependency analysis'.
- Annotation file overrides inference with a tiny JSON record: `producer_endpoint`, `producer_method`, `producer_resource_name` (a body property, header, or a full path `/accounts/[0]/zones/[0]/name` to disambiguate), `consumer_param`, optional `consumer_endpoint`/`consumer_method`, `except: {consumer_endpoint, consumer_method}` to exclude specific consumers, and a producer/consumer pair with no resource name expresses a pure ordering constraint (`/resource/{id}/start` before `/resource/{id}/stop`).
- Annotations can be global (`x-restler-global-annotations`) or attached locally to the consuming operation (`x-restler-annotations` inside the path item); 'input producers' cover values that come from another request's parameter (`custom_payload_uuid_suffix`) rather than a response.
- Fallback when no producer is found: the parameter becomes a `restler_fuzzable_string` / dictionary `custom_payload` - i.e. a typed placeholder rather than a failure.

**How to apply it.** Gives the bootstrap step before any LLM: parse every MCP server's tool `inputSchema` (and `outputSchema` where present, or a sampled result from the recorded traces) and auto-propose links where an output field name matches an input parameter name (`issue_key`, `channel_id`, `service_name`, `incident_id`, `trace_id`, `repo`, `project_id` across jira/slack/pagerduty/chronosphere/gcloud). The recorded agent traces then serve exactly the role of RESTler's 'dynamic feedback' to confirm which candidate links the agent actually used. Where names do not match (slack `text` -> jira `issueIdOrKey`), the one-time LLM emits an annotation row shaped like RESTler's, extended with an extractor. The writer/reader variable compilation (`post_id.writer()` / `set_var(post_id)`) is the smallest possible runtime model for 'memoize IDs from earlier results and inject later'.

**Limitations.** Inference is structural-name matching only; there is no free-text extraction, no ranking among multiple producers (it uses them in production order), and no notion of query templates for search-type consumers. The docs pages are thin on the heuristics themselves; the ICSE'19 paper (fetched PDF) is the primary source for the mechanism.

**Quotes**

> producer_resource_name: the name of the value produced. This can be the name of a property in the response body, the name of a response header, or the name of a path or query parameter in the request

> extracting and memoizing dynamic objects (if any), and providing those in subsequent requests in the sequence if needed, as determined by the dependency analysis

> RESTler cannot automatically infer such dependencies, which can happen for a variety of reasons (most often, because of very generic or inconsistent naming in the specification...)

### OpenAPI 3.1 Link Object and Runtime Expressions (spec + issue #1452 on arrays)

- **Link:** <https://spec.openapis.org/oas/v3.1.0#link-object>
- **Evidence quality:** spec

**Key techniques**

- A Link lives on a Response object and has `operationRef` | `operationId`, `parameters: Map[string, Any | expression]`, `requestBody`, `description`, `server`; it declares 'a known relationship and traversal mechanism between responses and other operations'.
- Runtime expressions: `$url`, `$method`, `$statusCode`, `$request.path.<n>`, `$request.query.<n>`, `$request.header.<n>`, `$request.body#<json-pointer>`, `$response.header.<n>`, `$response.body#<json-pointer>` (e.g. `$response.body#/successUrls/2`); expressions embed in strings as `{expr}` e.g. `http://example.com?id={$request.body#/id}`.
- Consumers are explicitly not required to follow links, and a link may be unfollowable at runtime - links are hints for a traversal engine, not a plan.
- Array limitation (issue #1452, closed unresolved): `$response.body#/fooIds` binds the whole array to one parameter; there is no `[*]` fan-out because JSON Pointer 'is meant to point to a single object rather than a pattern of objects'; proposed fix was JSONPath (`$.fooIds[*]`), never adopted for compatibility reasons.

**How to apply it.** Use the Link Object as the vocabulary for MCP tool link metadata (MCP has none): attach a `links` map to each tool's result description, each entry naming a target tool and `parameters` bound via `$response.body#/...` or embedded-string templates. Every hop that the agent does with structured IDs (jira -> pagerduty by incident id, pagerduty -> slack by channel id, gcloud project -> chronosphere label) is expressible as-is. It also tells you exactly what to add: (a) an extractor form for unstructured strings, (b) `[*]`-style fan-out for arrays of hits, which OpenAPI itself never solved.

**Limitations.** Structured fields only, one scalar per binding, no array iteration, no extraction, no conditions - it is the weakest of the five but the most widely understood vocabulary.

**Quotes**

> The presence of a link does not guarantee the caller's ability to successfully invoke it, rather it provides a known relationship and traversal mechanism between responses and other operations.

> http://example.com?id={$request.body#/id}

> The equivalent specification would be '$response.body#$.fooIds[*]'. That said, there's no obvious way to make the switch without breaking compatibility

## All sweep findings

_19 findings, sorted by relevance score._

### Arazzo Specification (OpenAPI Initiative) - declarative multi-step API workflows

- **Link:** <https://spec.openapis.org/arazzo/latest.html>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** Arazzo is the OpenAPI Initiative's standard for describing a sequence of API calls as a workflow: each Step names an operationId, binds `parameters` from runtime expressions, declares `outputs` (e.g. `id: $response.body#/id`), and `successCriteria` (simple boolean, regex, JSONPath or XPath conditions). Later steps reference `$steps.<stepId>.outputs.<name>` and `$inputs.<name>`; expressions can be embedded inside strings (`"https://{$inputs.host}/api/{$steps.create.outputs.id}"`). Libraries exist (libopenapi/pb33f, Speakeasy) that parse and run these documents with no LLM.

**Why it matters here.** This is the closest existing serialization format for 'recorded agent trace -> deterministic chain'. An LLM could emit an Arazzo-style document once (steps over MCP tools instead of OpenAPI operations), and a tiny interpreter runs it forever. Gaps to note: outputs are JSON-pointer only (no regex extraction into outputs), and regex is only allowed in successCriteria, so you would extend it with an `extract:` transform step for substring pulls out of log lines / slack text.

### Grafana Correlations with regex/logfmt transformations

- **Link:** <https://grafana.com/docs/grafana/latest/administration/correlations/use-variables-and-transformations/>
- **Kind:** product
- **Relevance:** `█████████░` 9/10

**What it is.** A Grafana correlation is a provisioned YAML object saying: for results of datasource A, on field X, offer a link that runs a query template on datasource B. Transformations (`type: regex`, `field: msg`, `expression: service=(\w+)\.\w+`, `mapValue: application`; or `type: logfmt`) parse a long unstructured field into named variables, which are then interpolated into the target query (`alias: ${application}`). Named capture groups become variables automatically; the docs explicitly say transformations exist because log lines carry more than one piece of information per field.

**Why it matters here.** This is the exact pattern the developer wants, already productized: a declarative (source tool, source field, regex-with-named-groups, target tool, query template) 5-tuple. It also handles the 'B only has a free-text search param' case because the target is a query template string. Copy the schema (correlations + transformations) for the MCP graph; the UI model (click a value -> list of applicable correlations) is what an 'MCP explorer' frontend needs.

### Trustfall - in-process query engine over any combination of APIs, files, DBs

- **Link:** <https://github.com/obi1kenobi/trustfall>
- **Kind:** oss-project
- **Relevance:** `█████████░` 9/10

**What it is.** Trustfall (Rust, with Python and WASM bindings) lets you write one GraphQL-like query that spans multiple data sources; each source is an adapter implementing `BasicAdapter` (resolve starting vertices, resolve properties, resolve neighbors along an edge, coerce types). A schema declares vertex types and typed edges between them, and the engine lazily evaluates the query, calling adapters only as needed. The demo joins the HackerNews API, GitHub API and local YAML files in one query; it powers cargo-semver-checks. Discussed on HN ('Trustfall: How to Query (Almost) Everything').

**Why it matters here.** Satisfies the hard constraint (no server; can even run in the browser via WASM) while giving a real 'graph database with an odd query language' over MCP servers. Each MCP tool becomes an adapter edge (`JiraIssue.slackThreads`, `SlackMessage.mentionedIssues`, etc.); the 'which tool next' choice becomes an edge in the schema, and free-text-search edges are just resolver code that formats a query string from the source vertex. The one-time LLM job becomes 'write the schema + edge resolvers'.

### RESTler producer-consumer dependency inference and annotation file

- **Link:** <https://github.com/microsoft/restler-fuzzer/blob/main/docs/user-guide/Annotations.md>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** RESTler compiles an OpenAPI spec into a grammar by inferring producer-consumer dependencies: 'request B should be executed after request A because B takes as input a resource-id x produced by A'. Inference is by matching response schema field names against path/body parameter names; where it is ambiguous, a JSON annotation file overrides it with `producer_endpoint`, `producer_method`, `producer_resource_name` (which can be a JSON pointer like `/accounts/[0]/zones/[0]/name`), `consumer_param`, `consumer_endpoint`, and `except` lists. Without response schemas it falls back to 'fuzzstring' values.

**Why it matters here.** Directly reusable design: (1) auto-derive candidate links from MCP tool inputSchema/outputSchema field names, (2) let a one-time LLM pass (or a human) fix them up in a small annotation file with JSON-pointer producer paths. The annotation file shape is a good minimal 'link table' for the MCP graph.

### OpenAPI Links object and runtime expressions

- **Link:** <https://learn.openapis.org/specification/links.html>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** OpenAPI 3.x `links` live on a Response object and declare that a value from this response (via runtime expressions like `$response.body#/uuid`, `$request.path.id`, `$response.header.X`) can be passed as a named parameter to another operation identified by `operationId`/`operationRef`. Consumers are not obliged to follow links; the spec's own issue tracker (#1452) shows arrays of items are not addressable by a single link, and there is no string-extraction facility.

**Why it matters here.** The canonical, tooling-supported way to say 'field in response A is the input of operation B' in a machine-readable spec. Use it as the vocabulary for MCP tool link metadata (MCP has no equivalent today). Its limitations (structured fields only, one link per scalar) are exactly the places the developer needs to add regex extractors and fan-out over arrays.

### Maltego transforms and machines - entity-typed graph exploration over heterogeneous APIs

- **Link:** <https://docs.maltego.com/en/support/solutions/articles/15000053545-building-integrations-for-maltego>
- **Kind:** product
- **Relevance:** `████████░░` 8/10

**What it is.** Maltego is an investigation UI where every node is a typed Entity (Domain, IPv4Address, Person, ...) and a Transform is a small function declared as 'takes one entity of type X, returns zero or more entities' (types annotated in the TRX/Python SDK). The client offers only transforms whose input type matches the selected entity, output entities are auto-linked to the input (link direction/labels customizable), and 'Machines' are scripted macros that chain transforms automatically. Transforms wrap arbitrary APIs (OSINT services, Recorded Future, etc.).

**Why it matters here.** The most mature interaction model for a no-LLM 'MCP explorer': select a node (Jira ticket), see applicable next-step tools filtered by entity type, click to expand the graph; Machines are the equivalent of a recorded agent trace turned into a deterministic chain. Borrow the type-driven transform registry and the 'output entities are new nodes with typed links' convention.

### A Multi-Agent Approach for REST API Testing with Semantic Graphs and LLM-Driven Inputs (AutoRestTest, arXiv 2411.07098)

- **Link:** <https://arxiv.org/html/2411.07098>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Builds a Semantic Property Dependency Graph offline from an OpenAPI spec by computing cosine similarity (GloVe word embeddings) between each operation's response field names and other operations' input parameter names; an edge is created above 0.7, otherwise the five most similar operations are connected so every node has exploration paths. At runtime a dependency agent uses the graph to pick which earlier operation supplies a required parameter, and Q-learning chooses among value sources (dependency-propagated value, LLM-generated, random). Builds on RestTestGen's Operation Dependency Graph, which used case-insensitive/stemmed name matching.

**Why it matters here.** Shows how to infer 'output field -> input param' edges across many tools cheaply and offline with word embeddings (no server, tiny model, or even just stemming/case folding) - a candidate-link generator for the MCP tool graph that the recorded traces can then confirm or prune. Also validates the idea of learning which link/value source works from feedback rather than asking an LLM each time.

### Firefly: Verified Tool-Call Data Generation from Real MCP APIs (arXiv 2605.17558)

- **Link:** <https://arxiv.org/html/2605.17558>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Firefly collects ~1,000 tools from real MCP servers and builds a pairwise directed tool graph where an edge A->B means A's output can be used as B's input. Because 'schema-level type matching alone is too coarse (two string fields may be semantically unrelated)', an LLM judges each candidate pair once from descriptions and schemas, labelling edges high/medium/low confidence (~83k edges, ~64k medium+). The graph then guides sub-DAG sampling; a retrieval-augmented simulator replays recorded tool outputs offline so no live MCP calls are needed during training.

**Why it matters here.** Closest published precedent for the developer's plan: one-time LLM pass to build a chainability graph over MCP tools, after which the graph is used without an LLM. The confidence-labelled edge list plus 'replay recorded tool outputs' cache are both directly applicable (the replay cache also cuts token/API cost during development).

### Grafana data links, Loki derived fields and Tempo trace-to-logs custom queries

- **Link:** <https://grafana.com/docs/grafana/latest/datasources/tempo/configure-tempo-data-source/configure-trace-to-logs/>
- **Kind:** product
- **Relevance:** `███████░░░` 7/10

**What it is.** Three related mechanisms: (1) field-level data links with variables such as `${__value.raw}`, `${__data.fields["name"]}`, `${__field.labels.X}` interpolated into URLs or internal Explore queries; (2) Loki 'derived fields' - a regex with one capture group applied to each raw log line (e.g. `"trace_id":"([a-f0-9]+)"`) whose match becomes an internal link whose query is `${__value.raw}` against the Tempo datasource; (3) trace-to-logs: a tag map from span attributes to log labels plus a custom LogQL template like ``{${__tags}} | pod=`${__span.tags["k8s.pod.name"]}` |= `${__trace.traceId}` `` with time-shift windows around the span.

**Why it matters here.** Concrete encodings for the two hardest sub-cases: extracting an ID from an unstructured line (derived fields regex) and building a query for a tool that only accepts a query string (trace-to-logs template with tag mapping and time window). The chronosphere/logz.io legs of the on-call chain map almost one-to-one onto these configs.

### TheHive Cortex analyzers and cortexutils.Extractor - typed regex extraction of observables from unstructured reports

- **Link:** <https://github.com/TheHive-Project/Cortex>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Cortex runs 'analyzers' that take an observable of a declared dataType (ip, domain, hash, url, mail, ...) and return a JSON report; `cortexutils.Extractor` recursively walks any nested report and applies a library of typed regexes (IPv4/IPv6, URL, domain, FQDN, MD5/SHA1/SHA256, email, registry key, user-agent, URI path) to every string, returning deduplicated `{dataType, data}` artifacts. TheHive imports those artifacts as new observables, which can in turn be fed to more analyzers or 'responders'.

**Why it matters here.** A proven, LLM-free pattern for 'IDs of interest are buried in long strings': a per-type extractor library + recursive JSON walk, where each extracted typed value becomes a node that unlocks the analyzers (tools) declared for that type. Swap the IOC regexes for corporate types (JIRA-123, PagerDuty incident IDs, Slack permalinks, k8s pod names, trace IDs, service names) and you have the extractor layer the plan calls for.

### Steampipe KeyColumns / required quals and cross-table value passing (plus in-process SQLite extensions)

- **Link:** <https://steampipe.io/docs/develop/writing-plugins>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Steampipe exposes SaaS/cloud APIs (Slack, Jira, GitHub, PagerDuty, GCP, Azure, ...) as SQL tables. Each table declares `KeyColumns` - which columns must (or may, with allowed operators) appear in WHERE/JOIN for the API call to be made; the engine picks Get vs List hydrate functions accordingly, and `transform.FromQual` passes a joined value straight into the dependent table's API call. Originally a Postgres FDW (local Postgres process), but Steampipe also ships the same plugins as in-process SQLite extensions ('Zero-ETL for SQLite').

**Why it matters here.** KeyColumns is a compact way to encode 'this tool needs this input, optionally with these operators' so a planner can decide which API calls a join implies - essentially a declarative tool-parameter contract. The SQLite-extension packaging shows a no-server path; and the plugin catalogue already covers most of the developer's MCP servers, useful as a reference for which fields are practical join keys.

### MuleSoft patent US10776189 - API query engine resolving declarative graph queries by orchestrating API calls

- **Link:** <https://patents.google.com/patent/US10776189B2/en>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** Describes an engine that takes a declarative graph-query and resolves it by automatically orchestrating REST calls: it extracts a unified conceptual model (entity types + relationships) from RAML/OpenAPI specs, infers undocumented relationships via 'folding' heuristics over hierarchical URL paths, allows developer annotations in the spec to override defaults, compiles the query into a plan, and executes it as a pipeline of streaming iterators where one call's output binds the next call's parameters.

**Why it matters here.** Prior art for the exact 'treat APIs as a graph DB with a query language' idea, with two reusable ideas: path-hierarchy heuristics to infer parent/child links, and spec annotations as the override mechanism. Also a caution that the hard part is relationship inference, not query execution.

### Backstage Software Catalog well-known relations

- **Link:** <https://backstage.io/docs/features/software-catalog/well-known-relations/>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Backstage models an organization as entities (Component, API, Resource, System, Group, User...) with directed typed relations that come in inverse pairs (ownedBy/ownerOf, dependsOn/dependencyOf, providesApi/apiProvidedBy, partOf/hasPart, memberOf/hasMember, parentOf/childOf). Relations are emitted by processors from spec fields (spec.owner -> ownedBy, spec.dependsOn -> dependsOn); targets are entity refs of the form `kind:namespace/name`; custom relation types are allowed.

**Why it matters here.** A ready-made vocabulary and entity-ref format for the typed nodes/edges an MCP graph needs (service -> team -> on-call -> Slack channel -> repo). incident.io's Catalog (types whose attributes reference other catalog types, auto-created from PagerDuty/GitHub/Slack integrations) is the same idea and could even be a source of the static edges, leaving only the dynamic (ticket/log/message) edges to the extractors.

### Splunk workflow actions and Kibana URL drilldowns - per-field 'next action' templates

- **Link:** <https://help.splunk.com/en/splunk-enterprise/manage-knowledge-objects/knowledge-management-manual/10.4/workflow-actions/about-workflow-actions-in-splunk-web>
- **Kind:** product
- **Relevance:** `██████░░░░` 6/10

**What it is.** Splunk workflow actions attach to fields or events: a GET action drops one or more field values into a URL template, a search action launches a secondary search using field values, and a POST action can create an entry in an external issue tracker. Kibana URL drilldowns do the same with Handlebars templates (`{{event.value}}`, `{{context.panel.query}}`, `{{kibanaUrl}}`) and three variable classes (global, panel context, event).

**Why it matters here.** Simple, declarative 'value -> action' registries keyed by field name, i.e. the menu of applicable next tools for any extracted ID. Good reference for the frontend: an extracted field (issue key, host, trace id) carries its list of templated actions across Jira/Slack/Confluence/logz.

### PowerShell pipeline parameter binding ByPropertyName (and Nushell structured pipelines)

- **Link:** <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pipelines?view=powershell-7.5>
- **Kind:** spec
- **Relevance:** `█████░░░░░` 5/10

**What it is.** PowerShell binds pipeline objects to cmdlet parameters first ByValue (type match) then ByPropertyName: an incoming object's property named `Name` binds to the `-Name` parameter of the next cmdlet, so `Import-Csv | Get-Service` works with no glue code when column names match parameter names. Nushell offers the same table-shaped pipeline but without declared binding rules.

**Why it matters here.** The cheapest possible link heuristic: 'output field name == input parameter name (case-insensitive) => link', which is also the baseline RESTler/RestTestGen use. Worth implementing first for MCP tools (issue_key, channel_id, service_name), then layering regex extractors and annotations on top.

### Prometheus/OpenMetrics exemplars and Datadog unified service tagging - embedded cross-tool join keys

- **Link:** <https://chronosphere.io/learn/exemplars-for-distributed-traces-a-better-approach-to-linking-metrics/>
- **Kind:** blog
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Exemplars attach a trace_id/span_id to metric samples so a UI can jump from a metric datapoint to a trace; Chronosphere documents this end to end. Datadog's unified service tagging standardizes env/service/version tags across metrics, traces and logs so 'pivot from alert to trace to log line' works by shared key rather than by any declared link.

**Why it matters here.** Adjacent: identifies which keys are the natural joins between chronosphere, logz.io and pagerduty (service, env, pod, trace_id, time window). The extractor/link layer should treat these as first-class typed values, and a time-window carrier (like Grafana's span time shift) is needed for the metrics/logs legs.

### kubectl-tree and kube-lineage - graph navigation via ownerReferences plus per-type custom link logic

- **Link:** <https://tohjustin.github.io/posts/2021-11-01-kube-lineage/>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** kubectl-tree walks `.metadata.ownerReferences` to print the object hierarchy; kube-lineage extends it with hand-written per-resource logic to discover relationships that are not expressed as ownerReferences (e.g. Service -> Pods via selector, Ingress -> Service by name).

**Why it matters here.** Instructive split: a generic link field for the easy cases plus small per-type linker functions for everything else. Expect the MCP graph to need the same - a generic 'ID-in-text' extractor and a handful of hand-coded linkers per server.

### OctoSQL - in-process SQL joins across files, DBs and plugin data sources

- **Link:** <https://github.com/cube2222/octosql>
- **Kind:** oss-project
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Go CLI/dataflow engine that joins JSON/CSV/Parquet/stdin and plugin-backed databases in one SQL statement, in-process, with streaming; plugins are Go modules. Discussed on HN (item 32093002).

**Why it matters here.** Alternative to Trustfall if SQL is preferred over GraphQL-like queries and the team wants an embeddable engine; less suited to per-value graph navigation but fine for batch joins over cached MCP results.

### Hasura action relationships / remote joins

- **Link:** <https://hasura.io/docs/2.0/actions/action-relationships/>
- **Kind:** product
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Hasura lets a REST-backed Action's output type declare object/array relationships to database tables (or remote GraphQL schemas) by mapping a field of the output type to a column, so one GraphQL query traverses from the REST response into other sources.

**Why it matters here.** Another declarative 'field of response type -> key of other source' join encoding, but it requires the Hasura server, so it is a design reference only.

## Dead ends

_Queries and directions that produced nothing useful. Listed so nobody repeats them._

- site:news.ycombinator.com queries for 'APIs as a graph' / 'joins across SaaS tools' returned no on-topic threads; the only useful HN hits came via Steampipe/Trustfall/OctoSQL submissions.
- 'API knowledge graph' on arXiv mostly returns ontology-to-REST generators (OBA) and KG-construction papers, not API-chaining graphs; the useful papers are in the REST-API-testing literature (RESTler, RestTestGen, AutoRestTest/2411.07098, KAT).
- Unified-MCP-Tool-Graph (github) stores tools in Neo4j with overlaps_with/extends/preferred_for_task edges and keeps an LLM in the loop for retrieval; it does not encode output->input parameter dependencies, so it is not a fit.
- Honeycomb and Lightstep: their 'correlations' are statistical (which attributes explain latency) and their cross-tool links are ad-hoc URL templates; no declarative link config worth copying.
- Datadog correlation is convention-based (unified service tags, injected trace_id) rather than a configurable link model; useful only as a list of join keys.
- Wikidata/SPARQL federation and FedX/Comunica only federate SPARQL endpoints; REST wrappers were not found in results, and it would violate the no-server constraint anyway.
- EnvFactory (arXiv 2605.18703) abstract has no detail on its embedding-based parameter matching; claim of bge-m3 parameter nodes came only from search snippets and was not verifiable.
- n8n docs URLs for expressions/data-mapping (docs.n8n.io/data/..., docs.n8n.io/code/expressions/) 404'd; Zapier 'data pill' mapping had no primary-source hit. Both are just template expressions ({{ $json.field }}) and add little beyond Grafana/Arazzo.
- patents.justia.com returned 403; Google Patents copy of US10776189 worked instead.
- OpenSRE 'knowledge graphs for incident response' and Sagy blog posts are generic vendor/agent-centric content with no reusable link-encoding details.
- RESTler ICSE 2019 PDF and arXiv 2411.07098 PDF could not be text-extracted by WebFetch; the HTML version of 2411.07098 and RESTler's GitHub docs were used instead.
