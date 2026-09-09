# MCP ecosystem: driving MCP servers without an LLM

_Research angle `mcp-ecosystem`. Generated from the research workflow run on 2026-09-09._

## Angle verdict

Yes, this angle yields a real, reusable pattern, though it is a substrate rather than the fuzzy core. Name it "frozen code-mode over session-pinned MCP": record agent traces with a single PostToolUse hook (mcp__.* matcher, JSONL of tool_input/tool_response keyed by prompt_id and tool_use_id, joined to transcript_path for the model's reasoning), generate a typed function per tool from tools/list (Anthropic's servers/<server>/<tool>.ts layout), have the LLM write the chain once as a persisted skill that filters and extracts in-process, then execute it with no model against long-lived authenticated sessions (mcpc @sessions or its --proxy) with isError/exit-class branching copied from the Inspector CLI. The ecosystem's honest limitation, visible in every source, is that results arrive as JSON-in-text (content[0].text | fromjson), structuredContent and resource_link are rare in the wild, and nothing at the protocol level chooses the next tool or phrases a search string; the only protocol hooks for fuzziness are completion/complete and resource templates, which exist only for prompts/resources and mostly require an in-house facade server. So the reusable pattern covers recording, typing, chaining, execution, auth, and a schema-drift guard (tools-get --schema); the entity-extraction and next-tool-selection logic still has to be authored as ordinary code from the traces, and the existing MCP web UI (Inspector) is a schema-form shell with zero cross-tool linking that the developer's frontend would extend rather than replace.

## Deep reads

### MCP spec 2025-06-18: Tools (outputSchema/structuredContent, resource_link, annotations, isError)

- **Link:** <https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
- **Evidence quality:** spec

**Key techniques**

- structuredContent is the only typed output channel and it is optional: 'Tools may also provide an output schema ... If an output schema is provided: Servers MUST provide structured results that conform to this schema.' Servers 'SHOULD also return the serialized JSON in a TextContent block' for backward compat, which is why in practice you parse content[0].text as JSON.
- resource_link content blocks are the only protocol-level 'this result points at another entity' edge: {type:'resource_link', uri, name, description, mimeType, annotations}. The spec explicitly warns these links 'are not guaranteed to appear in the results of a resources/list request', so they are per-result edges, not a catalog.
- Tool annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint) are declared on the tool definition; 'clients MUST consider tool annotations to be untrusted unless they come from trusted servers.' For corporate servers you control, readOnlyHint+idempotentHint is a usable machine gate for speculative/parallel/cached calls.
- Two error channels: JSON-RPC protocol errors (-32602 unknown tool / invalid args) versus tool execution errors returned as a normal result with isError:true and a text block. A no-LLM driver must branch on isError, not just on transport success.
- tools/list is paginated with an opaque nextCursor; inputSchema is JSON Schema, so a form or an argument-binder can be generated from it mechanically. Content blocks can carry annotations {audience, priority, lastModified} which are usable as a ranking signal when choosing which result text to extract from.

**How to apply it.** Defines the exact hooks a deterministic driver can rely on and the exact places it cannot. Concretely: (1) for every corporate server, run tools/list --full once and store inputSchema/outputSchema/annotations in a local catalog (sqlite/JSON file); the presence of outputSchema tells you whether a tool's edges can be read from structuredContent or must be regex/JSON-parsed out of content[].text. (2) Treat resource_link blocks, when jira/confluence/gdrive servers emit them, as first-class graph edges (uri -> resources/read). (3) Use readOnlyHint to decide which tools the planner may fan out speculatively (jira get_issue, slack search, confluence search, chronosphere query, logz search, codebase grep are all read-only; pagerduty ack/resolve, jira transition, gcloud/azure mutations are not). (4) Branch on isError so a failed logz query does not feed an empty string into the next slack search.

**Limitations.** Pure spec text with toy examples; it does not tell you what real vendor servers emit. Nothing in the spec provides cross-tool linking, entity types, or ID formats; all of that is client-side work. Annotations are hints and optional; most third-party servers omit them.

**Quotes**

> Servers MUST provide structured results that conform to this schema. Clients SHOULD validate structured results against this schema.

> Resource links returned by tools are not guaranteed to appear in the results of a resources/list request.

> For trust & safety and security, clients MUST consider tool annotations to be untrusted unless they come from trusted servers.

### apify/mcpc: universal MCP CLI with persistent named sessions, --json output, key:=value/stdin args, grep-based tool discovery

- **Link:** <https://github.com/apify/mcpc>
- **Evidence quality:** production-proven

**Key techniques**

- Persistent named sessions: `mcpc connect <url> @name` spawns a bridge process; later commands `mcpc @name tools-call tool k:=v` reuse it. Session metadata lives in ~/.mcpc/sessions.json with file locking; tokens in the OS keychain (or ~/.mcpc/credentials.json mode 0600); auto-reconnect with a 10s cooldown. This removes the per-call initialize/OAuth cost that makes naive scripting of remote MCP servers slow.
- Machine-readable output contract: `--json` 'always emits only a single JSON object (or array)' to stdout on success, stderr on error, and 'the returned objects are always consistent with the MCP specification' (i.e. you get the raw tools/call result: {content:[...], structuredContent?, isError?}).
- Three argument forms: `key:=value` (JSON-parsed with string fallback; `id:='"123"'` forces string), inline JSON as first arg, or a JSON object on stdin. Stdin JSON is what makes pipe-composition trivial.
- The canonical no-LLM chain, verbatim from the README: `mcpc --json @apify tools-call search-actors keywords:="scraper" | jq '.content[0].text | fromjson | .items[0].id' | xargs -I {} mcpc @apify tools-call get-actor actorId:="{}"`. Note the `.content[0].text | fromjson` step: even Apify's own server delivers JSON-in-text, not structuredContent.
- `mcpc grep <regex>` searches tool names/descriptions across all active sessions ('dynamic tool discovery'); `tools-get <tool> --schema expected.json` validates a live schema against a stored snapshot in compatible/strict modes, which is a drift detector for frozen extractors.
- Proxy mode (`connect <url> @relay --proxy 8080`) re-exposes an authenticated session as a local unauthenticated (or bearer-gated) MCP endpoint, so a web frontend or sandbox can call corporate servers without holding the OAuth tokens. Resource subscriptions can mirror a server resource to a local file.

**How to apply it.** This is the runtime substrate for the 'run extractors with no LLM' phase. Each of the developer's frozen steps becomes: `mcpc --json @jira tools-call get_issue key:="$KEY" | extract_jira_issue.py | mcpc @slack tools-call search_messages` where the extractor is a jq filter or a small Python script that pulls IDs/substrings from content[].text. Named sessions map one-to-one to jira/slack/confluence/chronosphere/logz/pagerduty/gdrive/gcloud/azure/arc. `tools-get --schema` snapshots guard the generated extractors against vendor schema drift. The `--proxy` mode is a ready-made backend for the web frontend: the browser talks to localhost proxies, tokens stay in the keychain. The docs/examples/company-lookup.sh script is a template for the defensive shell around a call (session-alive check, tool-exists check, `.content[0].text // empty` fallback, raw dump on parse failure), though it only calls one tool.

**Limitations.** A CLI, not a library: process spawn per call, and jq filters are a weak place to keep complex extraction logic (regex over log lines, fuzzy substring choice). No cross-server entity model, no caching of results, no notion of 'next tool'. Elicitation/completion not yet implemented. The company-lookup.sh example is single-tool, so the README's one-liner is the only real chaining demo.

**Quotes**

> mcpc --json @apify tools-call search-actors keywords:="scraper" | jq '.content[0].text | fromjson | .items[0].id' | xargs -I {} mcpc @apify tools-call get-actor actorId:="{}"

> Once agents understand the server's capabilities, they can write shell scripts that compose multiple mcpc commands with --json output... [this] can be more accurate and use fewer tokens than tool calling for complex workflows.

> the returned objects are always consistent with the MCP specification

### Anthropic Engineering: Code execution with MCP (tools as a filesystem of typed code; filter in the sandbox; persist skills)

- **Link:** <https://www.anthropic.com/engineering/code-execution-with-mcp>
- **Evidence quality:** opinion

**Key techniques**

- Tool-as-typed-function: each MCP tool becomes a file `./servers/<server>/<tool>.ts` exporting `async function getDocument(input: GetDocumentInput): Promise<GetDocumentResponse> { return callMCPTool<GetDocumentResponse>('google_drive__get_document', input); }`. The type comes from inputSchema/outputSchema; the model (or a human) only needs to read the files it uses.
- Chaining in code, not in context: `const transcript = (await gdrive.getDocument({documentId:'abc123'})).content; await salesforce.updateRecord({... data:{Notes: transcript}})`. The intermediate document never enters the model; reported 150,000 -> 2,000 tokens (98.7%).
- Filter/aggregate/join in the execution environment: `allRows.filter(row => row['Status']==='pending')` so 'the agent sees five rows instead of 10,000'; the same pattern is stated for 'aggregations, joins across multiple data sources, or extracting specific fields'.
- Native control flow: a `while(!found)` poll over slack.getChannelHistory with a 5s sleep, replacing agent-loop round trips.
- Privacy tokenization at the client: PII flows tool-to-tool while the model sees `[EMAIL_1]`, `[PHONE_1]` placeholders.
- Persisted skills: save working code as `./skills/save-sheet-as-csv.ts` plus a SKILL.md, so future runs call a higher-level function instead of re-deriving the chain. Progressive disclosure via filesystem listing or a `search_tools` tool with name / name+description / full-schema detail levels.

**How to apply it.** This is the developer's plan stated by the model vendor, with one gap: Anthropic still has the model author each run. The developer's step 2 ('LLM writes deterministic extractors/query generators from traces') is exactly 'save it as a skill', taken to its end state where the skill is invoked with zero model calls. Concrete mapping: generate `./servers/{jira,slack,confluence,chronosphere,logz,pagerduty,gdrive,gcloud,azure,arc,codebase}/<tool>.ts` (or .py) from tools/list; have Claude Code, once, write `./skills/investigate-ticket.ts` that does jira.getIssue -> extract service/commit/error-string -> slack.search -> codebase.grep -> confluence.search -> chronosphere.query + logz.search, with all filtering in-process; then run that script from the web frontend with no LLM. The 'filter 10,000 rows down to 5 in the sandbox' idea is the token-budget win and also why the hard constraint (no index servers) is satisfiable: the in-process script is the index.

**Limitations.** A blog post with no code release and no evaluation beyond one token count; the 'skills' mechanism is asserted, not measured for reuse rates. It says nothing about how to choose the next tool or phrase a search string without the model, which is the fuzzy part the developer wants to freeze. Requires a sandbox for model-written code; if the code is frozen and reviewed, that cost drops.

**Quotes**

> This reduces the token usage from 150,000 tokens to 2,000 tokens—a time and cost saving of 98.7%.

> The agent sees five rows instead of 10,000. Similar patterns work for aggregations, joins across multiple data sources, or extracting specific fields—all without bloating the context window.

> Running agent-generated code requires a secure execution environment with appropriate sandboxing, resource limits, and monitoring.

### Claude Code hooks reference: PostToolUse contract, mcp__<server>__<tool> matchers, transcript_path (plus local verification of transcript JSONL shape)

- **Link:** <https://code.claude.com/docs/en/hooks>
- **Evidence quality:** spec

**Key techniques**

- PostToolUse stdin JSON carries tool_name, tool_input, tool_response, tool_use_id, session_id, prompt_id, transcript_path, cwd, agent_id/agent_type (when inside a subagent). One hook with matcher `mcp__.*` (regex, unanchored) sees every MCP call from every server; `mcp__jira__.*` scopes to one server; `mcp__.*__write.*` selects by tool-name pattern.
- Handler types: command (stdin JSON, exec or shell form), http (POST body), mcp_tool (call another MCP tool with `${tool_input.field}` templating), prompt/agent. The docs include a verbatim logging hook that jq-appends {timestamp, tool, input} to ~/.claude/tool-calls.jsonl.
- PreToolUse can rewrite arguments via hookSpecificOutput.updatedInput, or block with exit 2; PostToolUse cannot block but can inject additionalContext. That makes PreToolUse the place to A/B a generated query-rewriter against the model's own argument choice before removing the model.
- Local verification (this machine, ~/.claude/projects/**/*.jsonl): an MCP call appears as an assistant record whose message.content holds {type:'tool_use', id, name:'mcp__kicad__open_project', input:{...}}, followed by a user record with {type:'tool_result', tool_use_id, content:[{type:'text', text:'{...json...}'}]} and a top-level `toolUseResult` field duplicating the MCP content array. The assistant text/reasoning preceding each tool_use is in the same file, so 'why this substring' context is recoverable from transcript_path alone, even without a hook.

**How to apply it.** Step 1 of the developer's plan with zero infrastructure: add one PostToolUse hook `{matcher:'mcp__.*', hooks:[{type:'command', command:'record-trace.sh'}]}` that appends {session_id, prompt_id, tool_use_id, tool_name, tool_input, tool_response} to a JSONL file. Because prompt_id and tool_use_id are present, the trace can be re-assembled into ordered chains per investigation (jira -> slack -> grep -> confluence -> chronosphere -> logz). Then join against transcript_path to attach the model's intermediate text, which is the supervision signal for the LLM that writes extractors ('it took the service name from the ticket summary, not the description'). PreToolUse+updatedInput lets you shadow-test a generated query-generator on live sessions: log what the model chose vs. what the extractor would choose, and only cut the model out once agreement is high.

**Limitations.** Traces are only as diverse as the sessions you run; the hook records inputs/outputs, not the alternatives the model considered. tool_response for MCP tools is the content array (JSON-in-text), so the same client-side parsing problem applies to the recorded traces. Hook docs are for Claude Code specifically; other agents need their own recorder.

**Quotes**

> "mcp__memory__.*"              # All tools from memory server

> PostToolUse ... Exit code 2 behavior: Shows stderr to Claude; the tool already ran

> "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {...}}

### MCP Inspector CLI and web client (--cli --method tools/call, --format json, exit-code classes; web UI = React SPA + local Node proxy with schema-generated forms)

- **Link:** <https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector/cli>
- **Evidence quality:** production-proven

**Key techniques**

- One request per process: `mcp-inspector --cli <server> --method tools/call --tool-name mytool --tool-arg key=value --tool-arg count=1 --tool-arg 'options={"format":"json"}'`; `--tool-arg` JSON-coerces values (so '012' becomes 12), `--tool-args-json '{...}'` passes the object verbatim. Server selection by positional stdio command, `--server-url --transport http`, or `--config ./mcp.json --server name` (which also carries headers, timeouts, OAuth, roots).
- `--format json` emits 'a single JSON object on stdout with no banners'; results are under `.result` (e.g. `| jq '.result.tools[].name'`).
- Stable exit-code classes for branching without prose scraping: 0 ok, 1 usage, 3 auth required, 4 unreachable, 5 tool error (isError:true or tool not found), plus a single JSON error line on stderr `{error:{code:'auth_required', message, status, url}}`. 'A tools/call that returns isError: true still prints its payload, but exits 5, so an && chain doesn't proceed on a failed call.'
- Non-interactive auth: `--stored-auth-only` reuses tokens the web inspector obtained and fails fast instead of waiting on a loopback OAuth callback. Other methods: resources/read --uri, resources/templates/list, prompts/get, servers/list from a catalog without connecting.
- Web client architecture: a single-page React app backed by 'a small Node server that owns the actual MCP connections', guarded by a per-launch token (MCP_INSPECTOR_API_TOKEN to pin it; DANGEROUSLY_OMIT_AUTH=true to disable). Tools tab: 'its input schema rendered as a form ... the result renders below with structured content, embedded resources, and images handled natively.' Protocol tab keeps a JSON-RPC transcript that 'can be cleared or exported'. Deep links `?serverUrl=&transport=&autoConnect=<token>` and `openApp=<tool>&appArgs=<base64url(JSON)>&autoOpen=<token>` let a script land a user on a pre-filled tool call.

**How to apply it.** Two uses. (1) As a scriptable caller it is the stateless alternative to mcpc: no session daemon, but a full initialize+OAuth per call, so it suits CI checks and one-off probes (schema snapshot of jira/confluence/logz servers, `tools/list` drift checks) more than a hot investigation loop. Its exit-code classes are worth copying into the developer's own runner so a frozen chain stops cleanly on auth (3) or tool error (5). (2) The web client is the closest existing 'web frontend over MCP servers': schema-driven argument forms, native rendering of content blocks, multi-server catalog, token-gated local proxy, exportable protocol transcript (another trace source), and deep links that pre-fill a tool call from a URL. It has zero cross-tool linking, so the developer's value-add is precisely an entity-extraction and 'next call' layer on top of that shell, e.g. clicking a Jira key rendered in a Slack result opens a deep link to jira get_issue with appArgs pre-filled.

**Limitations.** The overview page was thin; the CLI/web sub-pages carried the detail. CLI is one connection per invocation (slow against remote OAuth servers), and stream/session methods are rejected. The web UI is a debugging tool: no result persistence, no entity model, no multi-step workflows; deep-link auto-open exists only for MCP Apps tools. Both are useful as a starting shell, not as the linking engine.

**Quotes**

> --format json emits a single JSON object on stdout with no banners, so the whole output pipes cleanly

> A tools/call that returns isError: true still prints its payload, but exits 5, so an && chain doesn't proceed on a failed call.

> Select a tool to see its description, its input schema rendered as a form, and its annotations. Fill the form and call it; the result renders below with structured content, embedded resources, and images handled natively.

### MCP spec 2025-06-18: Completion (completion/complete) and Resource Templates (RFC 6570 URI templates) — added source: protocol-level 'fuzzy argument value' and 'ID -> resource' hooks

- **Link:** <https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/completion>
- **Evidence quality:** spec

**Key techniques**

- completion/complete takes {ref:{type:'ref/prompt'|'ref/resource', name|uri}, argument:{name, value}, context:{arguments:{...already resolved...}}} and returns {completion:{values:[...max 100], total?, hasMore}}; servers 'SHOULD ... Return suggestions sorted by relevance' and 'Implement fuzzy matching where appropriate'. It applies only to prompt arguments and resource-template URI variables, not to tools/call arguments.
- Resource templates (`resources/templates/list`) expose parameterized resources as RFC 6570 URI templates, e.g. `file:///{path}`; 'Arguments may be auto-completed through the completion API.' A server that exposes `jira://issue/{key}` or `confluence://page/{id}` turns 'I extracted an ID' into a single resources/read with no argument guessing.
- Resource and content-block annotations {audience, priority 0..1, lastModified} are a ranking signal for which result to follow when several are returned.
- Common URI schemes: https:// only when the client can fetch directly, file://, git://, plus custom RFC 3986 schemes. A stable per-server scheme is a natural key space for a local entity graph.

**How to apply it.** Where the developer controls or can wrap a server (a thin in-house MCP facade over jira/confluence/gdrive/arc is cheap), expose resource templates (`jira://issue/{key}`, `confluence://page/{id}`, `gdrive://file/{id}`, `git://commit/{sha}`) and a completions handler backed by an in-process index (sqlite FTS5 or a simple trigram table, which satisfies the no-index-server rule). Then the deterministic driver's 'pick the right substring' problem becomes: run candidate substrings through completion/complete for the target template and take the top ranked value. For servers you cannot wrap (chronosphere, logz, pagerduty, slack), the same completion logic lives client-side in the frontend, but the interface is the same shape, which keeps the generated extractors uniform.

**Limitations.** Completion is not defined for tool arguments, so for vendor servers whose only surface is tools this hook is unavailable without a facade. Few public servers implement completions; mcpc has not implemented it yet. Max 100 values per response.

**Quotes**

> Servers return an array of completion values ranked by relevance, with: Maximum 100 items per response

> Servers SHOULD: Return suggestions sorted by relevance; Implement fuzzy matching where appropriate

> Resource templates allow servers to expose parameterized resources using URI templates. Arguments may be auto-completed through the completion API.

## All sweep findings

_20 findings, sorted by relevance score._

### MCP spec 2025-06-18: Tools — outputSchema/structuredContent, resource_link, annotations

- **Link:** <https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** Primary spec text. Tools MAY declare `outputSchema`; if they do, servers MUST return schema-valid `structuredContent` (and SHOULD also serialize it into a text block for backward compat). Tools MAY return `resource_link` content blocks (uri, name, description, mimeType, annotations) that point to server resources not necessarily in resources/list. Tool annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint) are untrusted hints. tools/list is paginated with opaque nextCursor.

**Why it matters here.** Defines exactly which structured hooks a no-LLM driver can rely on: `structuredContent` is the only protocol-level typed output and it is optional; `resource_link` is the only protocol-level 'this result points to another entity' edge. In practice (see the vendor server findings below) almost none of the servers the user cares about declare outputSchema or emit resource_link, so the graph-edge extraction has to happen client-side by parsing text blocks. `readOnlyHint` is a usable machine signal for 'safe to call speculatively/in parallel' when the deterministic planner explores.

### apify/mcpc — universal MCP CLI with persistent sessions, --json 'code mode', stdin piping

- **Link:** <https://github.com/apify/mcpc>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** Built on the official TS SDK; supports every client-facing MCP feature. Named persistent sessions (`mcpc connect <url> @name`, then `mcpc @name tools-call tool key:=value`), `--json` guarantees a single JSON object for scripting, args accepted as key:=value / inline JSON / stdin. README shows chaining via jq+xargs: `mcpc --json @apify tools-call search | jq '.content[0].text | fromjson | .items[0].id' | xargs -I {} mcpc @apify tools-call get-actor`. Also exposes structuredContent, `tools-list --full` for complete schemas, OAuth 2.1, and a local authenticated proxy mode.

**Why it matters here.** Most complete existing 'drive MCP without an LLM' primitive: a session-keeping shell client with machine-readable output. The user's deterministic extractors could literally be jq/Python filters between mcpc calls; the jq+xargs example is the minimal form of 'extract ID from result, feed into next tool'. Also shows the reality that most results need `.content[0].text | fromjson` — i.e. text-block parsing, not structuredContent.

### Anthropic: Code execution with MCP (tools as a filesystem of code APIs, persisted skills)

- **Link:** <https://www.anthropic.com/engineering/code-execution-with-mcp>
- **Kind:** blog
- **Relevance:** `████████░░` 8/10

**What it is.** Instead of exposing tool definitions to the model, present each MCP tool as a typed code function on a filesystem; the agent writes code that chains tools, filters/transforms intermediate data in the sandbox, and only returns what it logs. Reports 150k→2k token reduction on a GDrive→Salesforce task. Explicitly recommends saving working code as reusable functions/skills so future runs call higher-level capabilities.

**Why it matters here.** This is essentially the user's plan stated by Anthropic: have the LLM write code once against a typed MCP API, persist it, and rerun it. The gap the user must fill is the 'zero-LLM at run time' part — Anthropic still has the model author each run's script. The user's trace-recording→extractor-generation step is the natural way to freeze those scripts into a library, and the 'intermediate data never enters the model' property is exactly the token-budget win.

### Claude Code hooks reference — PostToolUse receives tool_name, tool_input, tool_response; matchers like mcp__<server>__.*

- **Link:** <https://code.claude.com/docs/en/hooks>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** PostToolUse hooks get JSON on stdin including tool_name, tool_input and tool_response (plus session_id, transcript_path). MCP tools are named `mcp__<server>__<tool>` and can be matched with regex such as `mcp__.*` or `mcp__jira__.*`. Handler types include command, http and mcp_tool.

**Why it matters here.** Zero-infrastructure way to do step 1 of the user's plan (record agent tool-call traces): a single PostToolUse hook with matcher `mcp__.*` appending {tool_name, tool_input, tool_response} as JSONL. The transcript_path also gives the model's reasoning between calls, which is useful context for the LLM that later writes extractors ('why did it pick that substring').

### MCP Inspector CLI mode (--cli --method tools/call --tool-arg)

- **Link:** <https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** Official inspector has a `--cli` flag that runs tools/list, tools/call (with --tool-name / --tool-arg key=value), resources/read etc. without the browser, printing JSON for scripting and CI; 2.0 adds a `--tui` mode. The browser UI is a React app that connects via a local proxy and lets a human fill tool arguments from the inputSchema form.

**Why it matters here.** Reference implementation of both a human-driven MCP web frontend (form generated from inputSchema, results shown as content blocks) and a scriptable no-LLM caller. The inspector's UI source is the closest starting point for the user's 'web frontend over corporate MCP servers' — it already handles stdio/HTTP/OAuth and renders results, but has zero cross-tool linking; the user's value-add would be the extraction/linking layer on top.

### Cloudflare: Code Mode — MCP tools compiled to a TypeScript API executed in isolates

- **Link:** <https://blog.cloudflare.com/code-mode/>
- **Kind:** blog
- **Relevance:** `███████░░░` 7/10

**What it is.** Agents SDK fetches an MCP server's tool schemas and generates a TypeScript API with doc comments; the LLM writes JS against it, executed in a disposable V8 isolate whose only capabilities are 'bindings' to the MCP servers (credentials injected by a supervisor). Key claim: LLMs are better at writing code against APIs than at emitting tool calls, and chained calls avoid round-tripping outputs through the model.

**Why it matters here.** Provides the concrete recipe for turning tools/list inputSchemas into a typed client library (the thing the user's LLM-written extractors would be written against). The binding/supervisor design is also a good model for the user's web frontend: the browser or a small local process holds the generated code, the MCP connections hold the credentials.

### mkerix/toolscript — Claude Code plugin/CLI: TypeScript scripts calling MCP tools through a local gateway, Deno sandbox

- **Link:** <https://github.com/mkerix/toolscript>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Generates TypeScript types from MCP tool schemas, runs LLM-written scripts in a Deno subprocess whose only network access is a local HTTP gateway that aggregates multiple MCP servers (with OAuth handling). Scripts chain tool calls deterministically with data passing directly between calls. Lightweight alternative to Cloudflare's isolates; runs locally with no extra services.

**Why it matters here.** Closest open-source local implementation of 'code mode' that fits the no-extra-servers constraint (a local gateway process + Deno). The user could record the scripts Claude Code writes via toolscript, then keep them as the deterministic library and re-run them without the model. Type generation from schemas is directly reusable for their frontend.

### devhelmhq/mcp-recorder and Jarvis2021/agent-vcr — VCR-style record/replay proxies for MCP

- **Link:** <https://github.com/devhelmhq/mcp-recorder>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Transparent stdio/HTTP proxies that record every JSON-RPC request/response (method, params, result, latency) into a JSON cassette, then replay as a mock server with matching strategies (method+params, subset, sequential, exact) and diff/verify against a live server. agent-vcr (Capital One, https://github.com/Jarvis2021/agent-vcr) adds merge, index/search across cassettes, stats, and cross-language Python/TS cassettes.

**Why it matters here.** Alternative to hooks for capturing traces at the protocol level (captures tools/list schemas and raw content blocks exactly as the server sent them, independent of the client). Cassettes double as offline fixtures for developing and regression-testing the deterministic extractors without hitting Jira/Slack, and 'subset' matching is a cheap way to unit-test that a generated query generator reproduces the agent's calls.

### Atlassian Rovo MCP server — supported tools (search_jira=JQL, search_confluence=CQL, search_atlassian=natural language, discover/deferred tools)

- **Link:** <https://support.atlassian.com/atlassian-rovo-mcp-server/docs/supported-tools/>
- **Kind:** product
- **Relevance:** `███████░░░` 7/10

**What it is.** Official remote server. Tools are coarse verbs per product: read_jira/write_jira/search_jira/delete_jira/manage_jira, read_confluence/write_confluence/search_confluence, read_jsm, read_bitbucket, read_teamwork_graph, search_atlassian, search_code, plus getAccessibleAtlassianResources, discover, executeRead/executeWrite/executeDestructive. search_jira takes JQL, search_confluence takes CQL, search_atlassian takes free text (semantic via Rovo). Many tools are 'deferred' and must be located with `discover`. Docs do not document result shapes, outputSchema or cursors.

**Why it matters here.** The query generators for Jira/Confluence should emit JQL/CQL (well-specified, deterministic) rather than free text, so an LLM-written 'ticket key → JQL' or 'service name → CQL text ~' generator is easy. The important caveat is that result content is undocumented text, so the user must derive result parsers from recorded traces; and the deferred-tool/`discover` indirection means a tools/list snapshot is not sufficient — traces must capture which deferred tool was resolved.

### f/mcptools — CLI with --format json, shell mode, guard mode, proxy for shell scripts as tools

- **Link:** <https://github.com/f/mcptools>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Go CLI: `mcp call <tool> --params '{...}' <server cmd>`, `--format json|pretty|table`, persistent `mcp shell`, aliases for servers, `mcp guard --allow 'tools:read_*' --deny 'tools:write_*'` to filter a server, `mcp proxy tool ... ./script.sh` to publish shell scripts as MCP tools, and `mcp mock` to fake a server. Companion blog posts show jq pipelines over tools/call output.

**Why it matters here.** Two adjacent patterns useful here: (1) guard mode as a cheap way to expose only read-only tools to the deterministic runner; (2) proxy mode lets the LLM-written extractor scripts themselves be re-published as MCP tools, so the 'compiled' investigation steps live in the same namespace as the raw tools and can be composed further (or called from the agent later).

### fiberplane/mcp-gateway — local proxy that logs all MCP traffic to SQLite with a web dashboard

- **Link:** <https://github.com/fiberplane/mcp-gateway>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Local gateway exposing `/s/{server}/mcp` per registered server; every request/response/error is captured to `~/.mcp-gateway/logs.db` (SQLite) and browsable/searchable in a web UI, with a REST `/api/logs` endpoint filterable by server/session and JSON export. Also acts as an MCP server itself. Related: mcp-audit (signed audit log), mcpsnoop (live terminal tap).

**Why it matters here.** Fits the constraint (single local process + sqlite, no search server) and gives the user a ready trace store keyed by server/session that the extractor-writing LLM can query. Because it is itself an aggregating MCP endpoint, the web frontend could talk to one URL for all corporate servers.

### grafana/mcp-grafana — 100+ tools; query_prometheus(datasource_uid, expr, start, end, step), query_loki_logs, search_dashboards, generate_deeplink

- **Link:** <https://github.com/grafana/mcp-grafana>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Official Go server. Prometheus tools: query_prometheus (instant/range with datasource_uid, expr, start/end/step), list_prometheus_metric_names/label_names/label_values, metadata; Loki: query_loki_logs (LogQL, limits, guardrail modes), label/pattern tools; dashboards: search_dashboards by query/folder/tag/starred, get_dashboard_property (JSONPath-style extraction), generate_deeplink for dashboard/panel/explore URLs. No outputSchema declared; results are text/JSON in content blocks; README emphasises limiting tool exposure to save context.

**Why it matters here.** Good proxy for the user's Chronosphere/Prometheus step: parameters are fully structured (PromQL + uids + times) so a deterministic generator is straightforward (service name → label matcher → expr). get_dashboard_property and generate_deeplink are examples of servers doing extraction/linking server-side, which the user's frontend can emulate client-side. The lack of outputSchema means you still parse the text block.

### korotovsky/slack-mcp-server — conversations_search_messages with structured filters, cursor pagination, CSV outputs

- **Link:** <https://github.com/korotovsky/slack-mcp-server>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Popular Slack server. conversations_history(channel_id, cursor, limit '1d'|'1w'|N), conversations_replies(channel_id, thread_ts, ...), conversations_search_messages(search_query optional, filter_in_channel, filter_in_im_or_mpim, filter_users_from/with, filter_date_before/after/on/during, filter_threads_only, cursor, limit≤100), users_search, channels_list. Directory/list tools return CSV; history returns rows with the pagination cursor in the last row. Search is unavailable with bot tokens.

**Why it matters here.** Illustrates what the graph edges look like in practice: Slack entities are (channel_id, ts/thread_ts, user_id) and they appear inside CSV/text rows, not as resource links. A deterministic Slack query generator has a mostly structured surface (filters) plus one fuzzy string (search_query) — the fuzziness the user wants to replicate is confined to choosing that substring from a Jira ticket or log line. CSV output is easy to parse without an LLM.

### Logz.io official MCP server — search_logs (Elasticsearch DSL), search_logs_simple, scroll_logs, query_prometheus_metrics(_range)

- **Link:** <https://docs.logz.io/docs/open360/logzio-mcp/>
- **Kind:** product
- **Relevance:** `██████░░░░` 6/10

**What it is.** Official hosted server. Logs: search_logs(query as Elasticsearch DSL, size, from, sort, day_offset), scroll_logs(scroll_id), search_logs_simple(search_term), search_logs_by_timestamp(start_time,end_time,search_term), get_log_structures, get_all_log_types; metrics: query_prometheus_metrics / _range (PromQL, ISO-8601 times), get_available_metrics, get_metric_labels; plus dashboards/alerts/insights tools. Requires ISO-8601/RFC3339 timestamps; OAuth or token auth.

**Why it matters here.** For the user's logz step, the deterministic generator can emit full Elasticsearch DSL (exact term/phrase filters on request_id/trace_id fields, time ranges) instead of free text — more precise than what the agent usually types. scroll_logs gives cursor-style paging that a programmatic driver can exhaust, which an LLM agent never does. get_log_structures is a cheap way to learn field names for ID extraction without a search index.

### Skill-DisCo: Distilling and Compiling Agent Traces into Reusable Procedural Skills (arXiv 2606.26669)

- **Link:** <https://arxiv.org/abs/2606.26669>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6/10

**What it is.** Treats successful agent traces as paths in an unknown transition graph; normalizes each raw trace into an executable intermediate program, segments it into subgoal-level operations, extracts parameterized finite-state-machine (PFSM) subgraphs shared across traces, and compiles them into callable, executable, verifiable procedural skills. Evaluated on ALFWorld/WebArena: higher success and fewer agent turns. Related: AgentDistill (arXiv 2506.14728) has the teacher LLM generate parameterized Python 'MCP' tool modules, then abstracts/clusters/consolidates them into an 'MCP-Box' the student reuses without training (student still picks tools with an LLM); DynamicMCPBench (arXiv 2607.20531) distills live-MCP traces into path-agnostic TaskSpecs with tool-equivalence sets and value checkpoints.

**Why it matters here.** Academic form of the user's 'record traces → compile deterministic procedures' plan. Useful ideas: normalize traces into programs first, parameterize the 3-ish varying inputs, cluster similar traces before consolidating, and encode tool equivalence sets (jira.search vs jira.read) rather than exact paths. Caveat: all three still keep an LLM in the loop for dispatch; none tackles substring extraction from unstructured text, which the user must solve separately.

### n8n MCP Client node (and Activepieces issue #13788) — call MCP tools as deterministic workflow steps

- **Link:** <https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-langchain.mcpclient>
- **Kind:** product
- **Relevance:** `█████░░░░░` 5/10

**What it is.** n8n distinguishes the 'MCP Client Tool' sub-node (for agents) from the 'MCP Client' core node, which calls a chosen tool of an external MCP server as a normal workflow step with arguments mapped from previous steps' output, no LLM. Activepieces has an open proposal for the same piece: dropdown populated from tools/list, inputs generated from inputSchema, raw result available to later steps.

**Why it matters here.** Shows the industry pattern for 'MCP without LLM': tool selection and argument mapping done by a human at design time, expressions/JS for extracting fields between steps. This is a strong UX template for the user's frontend (pick tool → form from inputSchema → map fields from prior result), though these products don't help with fuzzy substring extraction from unstructured text.

### PagerDuty hosted MCP server (mcp.pagerduty.com) — incident list/get/context/list_alerts/list_notes; Google Drive official MCP (drivemcp.googleapis.com) — search_files/read_file_content/get_file_metadata

- **Link:** <https://support.pagerduty.com/main/docs/pagerduty-mcp-server>
- **Kind:** product
- **Relevance:** `█████░░░░░` 5/10

**What it is.** PagerDuty: ~55-60 tools split into read (list, get, list_alerts, get_alert, list_notes, context related|past|outlier, list_change_events) and write (create, update, add_note, add_responders); API key or OAuth; no tool filtering on hosted server; self-hosted repo archived. Google Drive official server (https://developers.google.com/workspace/drive/api/guides/configure-mcp-server): 8 tools — search_files, read_file_content, get_file_metadata, get_file_permissions, list_recent_files, download_file_content, copy_file, create_file — OAuth 2.0 over HTTP. Neither documents outputSchema or result shapes.

**Why it matters here.** Confirms the pattern across vendors: read/write split by tool name (usable as a safety filter), IDs returned inside text/JSON blobs, free-text search params (search_files) whose phrasing is the fuzzy bit. PagerDuty's `context` tool (related/past/outlier incidents) is a server-side 'graph neighbour' operation worth exploiting directly rather than re-implementing.

### Meta MCP: chaining tools via PROMPT_ARGUMENT (cefboud) — multiplexed tools/call batch with LLM-filled args

- **Link:** <https://cefboud.com/posts/XMCP-multiplexing-mcp/>
- **Kind:** blog
- **Relevance:** `█████░░░░░` 5/10

**What it is.** A 'MultiplexTools' tool accepts a list of JSON-RPC tool requests and runs them sequentially; any argument prefixed PROMPT_ARGUMENT is filled by a small LLM call using previous results as context (e.g. list Kafka topics → read messages → create duplicate named after the original). Batch execution avoids client round-trips; LLM used only for the argument-derivation hops.

**Why it matters here.** Instructive middle ground: the chain structure is fixed and deterministic, and LLM use is confined to tiny 'derive this argument from that output' calls. The user's design could adopt the same shape and then replace each PROMPT_ARGUMENT with an LLM-authored regex/jq extractor, falling back to a cheap model only when the extractor fails to match.

### open-webui/mcpo — MCP-to-OpenAPI proxy (each tool becomes a REST endpoint)

- **Link:** <https://github.com/open-webui/mcpo>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Wraps any stdio/HTTP MCP server and exposes every tool as a documented OpenAPI/REST endpoint (POST /<server>/<tool> with JSON body = tool arguments), with auto-generated Swagger docs and API-key auth. Similar bridges: SecretiveShell/MCP-Bridge (REST for all MCP primitives). Cloudflare's earlier Code Mode post claims a whole 2,500-endpoint API fits in ~1,000 tokens when presented as code.

**Why it matters here.** Simplest way to let a browser-side web frontend call corporate MCP servers with plain fetch() and no MCP client library or CORS/session handling: one local mcpo process. Trade-off: it flattens results to JSON and drops MCP-specific niceties (resource links, sampling), which the user is not relying on anyway.

### SEP-1865 MCP Apps / mcp-ui — tools returning interactive ui:// HTML resources

- **Link:** <https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp>
- **Kind:** spec
- **Relevance:** `███░░░░░░░` 3/10

**What it is.** Stable since 2026-01-26: a server publishes an HTML view as a ui:// resource linked to a tool, so the tool result renders as an interactive UI in the host, with bidirectional JSON-RPC between the iframe and host (including the UI calling further tools). Supported by Claude desktop/web, VS Code, Goose, Postman; grew out of mcp-ui.

**Why it matters here.** Adjacent rather than central: it is the protocol's answer to 'human-facing UI over MCP', but it puts the UI in the server, whereas the user wants a client-side frontend over servers they don't control. Still useful as a source of host-side plumbing (how a UI iframe invokes tools/call through the host) if the user's frontend wants to embed vendor-provided widgets later.

## Dead ends

_Queries and directions that produced nothing useful. Listed so nobody repeats them._

- Searching for any MCP server or convention that emits cross-server entity references (e.g. jira://KEY, slack://channel/ts) as resource_link blocks — found only per-server custom URI schemes for resources (note://, stock://) and the IETF 'mcp:' discovery URI draft; no linking convention exists, so cross-tool ID linking must be done client-side.
- Looking for vendor MCP servers (Atlassian, Slack, Grafana, PagerDuty, Google Drive, Logz.io) that declare outputSchema/structuredContent — none of the docs/READMEs fetched mention it; results are text/JSON/CSV inside content blocks.
- 'MCP servers as a graph database' queries return only Neo4j/knowledge-graph-memory MCP servers (graph DBs exposed via MCP), not treating a set of MCP servers as a graph — no prior art found for the user's framing.
- Searching Hacker News for 'MCP without LLM' yields general threads asserting servers are deterministic function calls, but no project doing agent-trace→deterministic-program compilation; the only concrete pointer was mkerix/toolscript.
- The PagerDuty GitHub Pages docs URL (pagerduty.github.io/pagerduty-mcp-server/docs) returns 404; self-hosted repo is archived — use the support.pagerduty.com page instead.
- MCP sampling and elicitation were not pursued in depth: both are server-initiated requests that presuppose an LLM/human on the client side, so they do not help a no-LLM driver (a deterministic client would have to refuse or stub them).
- Searching for a browser-side MCP client (Streamable HTTP directly from browser JS) turned up only the official TS SDK and Node examples; no evidence on CORS handling by vendor remote servers, so a local proxy (mcpo, fiberplane gateway, toolscript gateway, mcpc --proxy) is the safe assumption.
