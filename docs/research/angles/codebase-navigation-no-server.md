# Codebase navigation without an index server

_Research angle `codebase-navigation-no-server`. Generated from the research workflow run on 2026-09-09._

## Angle verdict

Yes - this angle yields a real, reusable pattern, and it is the one that most directly replaces the agent's codebase hops. I would name it "source-as-schema navigation": precompute, at repo sync, a small set of static artifacts from the code itself - (1) a log-template table (Xu: logger-call format strings -> regex + variable names/types + file:line, extracted with ast-grep rules), (2) a name-level def/ref graph with typed edges (aider tags / LocAgent contains-imports-invokes-inherits), and optionally (3) a precise SCIP SQLite for type-resolved references - and then expose exactly three deterministic operations over them: fuzzy-search-entity (exact id -> name dictionary -> stemmed BM25 -> token_set_ratio), k-hop-traverse with type/edge filters, and retrieve, plus a ranked-by-personalized-PageRank projection seeded with whatever identifiers arrived from jira/slack/logz. Every piece is an in-process library (tree-sitter/ast-grep, networkx or a JSON adjacency map, bm25s/MiniSearch, rapidfuzz, sqlite/sql.js), so the no-server constraint holds. The fuzziness the agent provides today is replaced by two explicit mechanisms the traces can calibrate: the template-match-then-regex-verify step that turns unstructured log/slack text into typed entity bindings, and the identifier-worthiness heuristics (Xu's many-occurrences/many-distinct-values/many-templates; aider's long-snake/camel-case-scores-high) that decide which substring to pivot on. LocAgent's ablations are the useful warning: keyword/BM25 entry search is the load-bearing part, multi-hop traversal is secondary, so invest in the extractor and search cascade first. The main gap is type resolution outside Python/Java: tree-sitter gives names only, so Xu's toString/subclass expansion has to be approximated per language or backed by SCIP where a buildable checkout exists.

## Deep reads

### Xu, Huang, Fox, Patterson, Jordan - Detecting Large-Scale System Problems by Mining Console Logs (SOSP 2009), Sec. 3, 4.2, 7 and Appendix A

- **Link:** <https://www.sigops.org/s/conferences/sosp/2009/papers/xu-sosp09.pdf>
- **Evidence quality:** production-proven

**Key techniques**

- Partial template extraction: one AST pass (Eclipse JDT) finds every method invocation on the logger class (log4j-style loggers auto-detected from the library the project links), turns the string-format/concatenation argument into a regex ('starting: (.*)'), and records for each call the interpolated variable names, their declared types, and file:line of the call.
- Type resolution via a toString Table + Class Hierarchy Table: for each non-primitive interpolated variable, look up that class's toString() template and substitute it in; if absent, walk up superclasses; ALSO enumerate all known subclasses and emit one template per subclass override; recurse until only primitives remain (depth-limited; descendant fan-out capped at 100 to avoid Object/JDK explosions). Anything unresolvable becomes '(.*)'.
- Runtime matching: compile all templates into an in-memory Lucene reverse index; for each log line strip numbers/special symbols to form the query, take the relevance-ranked candidates, and 'pick the highest-ranked result that allows a regular expression match to succeed'. Index 'usually fits in memory', matching is embarrassingly parallel.
- Automatic identifier discovery (Algorithm 1): a template variable is an identifier if it is (a) reported many times, (b) has many distinct values, (c) appears in multiple message types. Group log lines by identifier value -> per-entity 'message count vector' (bag-of-message-types), which is what you then reason over.
- Parse failure rates: HDFS 0.121% of 24M lines, Darkstar 0.002%; failure = no template regex matches. Language-specific idioms (Arrays.deepToString) handled as special cases; undecorated messages (only a bare variable) are ignored.

**How to apply it.** Directly implements 'given a logz.io line, which source line produced it and what are the entity values in it' with zero LLM and zero servers: build the template table once from the repo (ast-grep can do the partial-template extraction step per logger idiom; an LLM can write the per-language toString/format resolver once), ship it as JSON/sqlite, and at query time replace Lucene with an in-process BM25/trigram index over template constant tokens (e.g. MiniSearch/lunr in the browser, or tantivy/bm25s in Python) followed by regex verification. The matched template's variable bindings give you typed IDs (block id, txn id, request id) to feed into jira/slack/chronosphere/pagerduty search params; the identifier-discovery heuristic (many occurrences, many distinct values, appears in several templates) is exactly the fuzzy 'which substring in this blob is worth pivoting on' decision the agent currently makes, made deterministic. Also covers the reverse direction: a template's file:line plus the def/ref graph tells you which module/team/Slack channel owns the failure.

**Limitations.** Requires source for the logging code (third-party/library log lines need their own templates or fall through to '(.*)'); static prediction fails on loops/dynamic format strings; original implementation is Java-only and toString resolution needs a real type hierarchy (tree-sitter alone gives names, not types, so structured-logging idioms like zap/logrus/slog key=value fields are easier to template than Java string concat); templates must be rebuilt per release and matched against the right version; 2009 paper, no maintained code artifact.

**Quotes**

> "it is much easier for a machine to use the source code as the 'schema' for console logs."

> "we construct an index query from each log message by removing all numbers and special symbols. From the list of relevance-ranked candidate results returned by the reverse-index search, we pick the highest-ranked result that allows a regular expression match to succeed against the log message."

> "Find all message variables reported in the log with the following properties: a. Reported many times; b. Has many distinct values; c. Appears in multiple message types."

### ast-grep - tree-sitter structural search CLI, YAML rules, --json output, napi/pyo3/wasm bindings

- **Link:** <https://github.com/ast-grep/ast-grep>
- **Evidence quality:** production-proven

**Key techniques**

- Patterns are real code with metavariables: '$VAR' matches one AST node, '$$$' matches a sequence; a pattern like `logger.$METHOD($FMT, $$$ARGS)` dumps every logging call with the format string bound to $FMT.
- YAML rule objects compose atomic rules (pattern, kind, regex, nthChild), relational rules (inside, has, follows, precedes, each with stopBy: neighbor|end|rule) and composite rules (all, any, not, matches) plus per-metavariable constraints; 'A node must satisfies all fields in the rule object to be considered as a match.'
- `--json=stream|pretty|compact` emits one object per match with text, range (byteOffset, zero-based line/column start+end), file, lines, and metaVariables split into single / multi / transformed - i.e. a ready-made extractor output format.
- Library API (`@ast-grep/napi`, `ast-grep-py`, `@ast-grep/wasm`, Rust `ast_grep_core`): parse(lang, src) -> SgRoot.root() -> find/findAll(pattern|rule) -> getMatch('FMT').text()/range(); async findInFiles({paths, matcher}) walks a tree multi-threaded; napi ships Html/JS/TS/TSX/CSS built in and loads others via registerDynamicLanguage from @ast-grep/langs. '@ast-grep/wasm' enables the JavaScript API in browsers.
- Multi-core Rust CLI positioned against semgrep (not embeddable, slower CLI) and comby (not syntax-aware).

**How to apply it.** The engine for both the Xu-style logger-call extractor and generic 'who calls X / where is X constructed' over the codebase read/glob/grep/ast-grep MCP tools. Recorded agent traces that today use the ast-grep MCP tool become YAML rule files (one per logger idiom per language, one per 'call-site of symbol' question); run them at repo sync with --json=stream into a sqlite/JSON table (file, line, $FMT, $$$ARGS) that the frontend queries. The wasm build means the frontend itself can run a rule over a fetched file client-side without any server. Constraints + regex on metavariables let an LLM-authored rule encode fuzziness (e.g. 'format string contains the word timeout') deterministically.

**Limitations.** Purely syntactic: metavariables bind nodes, not resolved types, so Xu's toString/subclass step must be done by a second pass (or approximated by name); patterns must be valid parseable snippets per language and can be brittle across formatting idioms; no cross-file resolution or call graph; the Python/JS bindings' fix/rewrite path is marked experimental; multi-file API needs the Rust/napi runtime rather than the browser wasm.

**Quotes**

> "Think of it as regular expression dot `.`, except it is not textual."

> "A node must satisfies all fields in the rule object to be considered as a match."

> "JavaScript API in browsers and other WebAssembly environments" (@ast-grep/wasm)

### Aider repo map: tree-sitter tags + personalized PageRank over a def/ref graph (blog + aider/repomap.py source; RepoMapper standalone port)

- **Link:** <https://github.com/Aider-AI/aider/blob/main/aider/repomap.py>
- **Evidence quality:** production-proven

**Key techniques**

- Tag extraction: per-language tags.scm queries capture `name.definition.*` and `name.reference.*` nodes -> Tag(rel_fname, fname, line, name, kind); if a language has defs but no ref captures, fall back to Pygments token stream (`token[0] in Token.Name`) to synthesize reference tags. Cached per file by mtime in diskcache/sqlite.
- Graph: nx.MultiDiGraph with edge referencer_file -> definer_file per identifier, weight = use_mul * sqrt(num_refs). Multipliers: ident in mentioned_idents *10; snake/kebab/camelCase and len>=8 *10; leading underscore *0.1; defined in >5 files *0.1; referencer in chat files *50; isolated defs get self-edge weight 0.1.
- Personalization vector: 100/len(files) mass on chat files, mentioned files, and files whose path components intersect mentioned_idents; then `nx.pagerank(G, weight='weight', personalization=...)`. Each file's rank is pushed onto its outgoing edges proportional to weight and summed per (definer_file, ident) to rank definitions.
- Budget fitting: binary search over prefix length of the ranked tag list, rendering with grep-ast TreeContext (shows definition line plus enclosing scope, ellipses elsewhere) and counting tokens until within ok_err of --map-tokens; lines truncated to 100 chars.
- RepoMapper (pdavis68) reimplements the same pipeline as a standalone CLI/MCP server over tree-sitter + networkx + grep-ast for ~30 languages, with --chat-files/--mentioned-files/--map-tokens flags.

**How to apply it.** The deterministic 'which files matter for THIS ticket/log line' ranker: seed mentioned_idents with identifiers pulled from the jira ticket, slack thread, or the variable bindings of a matched log template, seed mentioned_files with the file:line of the template, run personalized PageRank, and you get a ranked file/symbol list the frontend can render or use to choose the next codebase read call - no LLM. The def/ref MultiDiGraph is also a serviceable name-level 'who references X' index for the codebase read/grep tools, precomputable at repo sync and loadable in the browser (small JSON). The identifier heuristics (long snake/camel names score high, common short names low) are the same 'which substring is worth searching for' judgment the agent makes, made explicit and tunable from traces.

**Limitations.** Name-based only: no type resolution, overloads/shadowing collapse into one node, and cross-repo/service boundaries are invisible; ranking is tuned for LLM context selection, so multipliers are heuristics to re-tune from traces; tags.scm coverage varies by language and the blog itself is thin (mechanism only visible in repomap.py); PageRank is over files, so symbol-level rank is a projection.

**Quotes**

> "G.add_edge(referencer, definer, weight=use_mul * num_refs, ident=ident)"

> "if (is_snake or is_kebab or is_camel) and len(ident) >= 8: mul *= 10"

> "ranked = nx.pagerank(G, weight='weight', **pers_args)"

### LocAgent: Graph-Guided LLM Agents for Code Localization (ACL 2025) + gersteinlab/LocAgent code (build_graph.py, traverse_graph.py, repo_ops.py, retrievers)

- **Link:** <https://arxiv.org/html/2503.09089>
- **Evidence quality:** research-prototype

**Key techniques**

- Graph: networkx MultiDiGraph with NODE_TYPE_{DIRECTORY,FILE,CLASS,FUNCTION} and EDGE_TYPE_{CONTAINS,IMPORTS,INVOKES,INHERITS}; nodes carry type, code, start_line, end_line; entity ids are 'path/file.py:Class.method'. Built with Python `ast`; imports resolved to paths by `module_name.replace('.', '/') + '.py'`; invoke/inherit targets resolved by NAME only via a scope-walking lookup of reachable nodes with suffix matching.
- SearchEntity cascade (repo_ops.search_entity): (1) exact node id `searcher.has_node(term)`; (2) global_name_dict then global_name_dict_lowercase after stripping 'class '/'function '/'def ' prefixes and splitting 'Class.method'; (3) BM25 over entity ids (`Document(text=nid)`, llama-index BM25Retriever, English Stemmer) plus a second BM25 over code chunks (EpicSplitter, chunk 100-2000 tokens, top 5); (4) rapidfuzz `fuzz.token_set_ratio` over node ids with '_'/'-' split into words, top-k=3. Results returned at detail levels complete < code_snippet < preview < fold.
- TraverseGraph (explore_tree_structure): start_entities, direction upstream|downstream|both, traversal_depth (-1 -> 20), entity_type_filter, dependency_type_filter; BFS with a frontier queue over MultiDiGraph edges; output serialized as an indented tree ('├── contains ── Name', reversed edges labeled 'invokes-by') so topology is encoded by indentation.
- RetrieveEntity: id -> file path, line span, code; file_path:line inputs resolved to the enclosing module via get_module_name_by_line_num with a ±20 line window.
- Ablations (Qwen-2.5-7B fine-tuned): removing SearchEntity drops function-level acc 71.53 -> 53.28; removing BM25 index -> 60.22; single-hop traversal -> 66.79; disabling TraverseGraph -> 66.06. File-level Acc@5 ~76-78% on SWE-bench-Lite.

**How to apply it.** The closest existing blueprint for an 'agent-shaped but callable' codebase navigator: three deterministic tools (search by fuzzy keyword, k-hop typed traversal, retrieve) that an agent's recorded trace can be replayed against as a fixed call sequence with no LLM. The search cascade is precisely the substring-to-entity fuzziness you need when the input is a token pulled from a slack message or log line - exact id, then name dictionary, then stemmed BM25, then token_set_ratio fuzzy - all in-process (networkx pickle + llama-index BM25 persist dir + rapidfuzz; no server). The ablation numbers tell you which pieces matter: keyword search and BM25 are the load-bearing parts, multi-hop traversal is secondary. Map to the codebase read/glob/grep MCP tools plus ast-grep for non-Python graph building.

**Limitations.** Python-only graph builder (ast module; 'Framework currently limited to Python codebases'); invoke edges are name-resolved so precision is low on polymorphic code; requirements pull llama-index/faiss (heavy) even though the BM25 path needs only bm25s/rapidfuzz; traversal has no output cap; reported accuracies are with an LLM driving the tools, so a fully scripted replay will be lower and needs the trace-derived policy to choose hops/filters.

**Quotes**

> "frontiers, visited = [(nid, 0) for nid in roots], []"

> "fuzz.token_set_ratio" over identifiers preprocessed with "re.findall(r'\b\w+\b', s.replace('_', ' ').replace('-', ' '))"

> "module_path = os.path.join(repo_path, module_name.replace('.', '/') + '.py')"

### SCIP index format (scip.proto), scip CLI (print --json, expt-convert to SQLite) and per-language indexers

- **Link:** <https://github.com/sourcegraph/scip/blob/main/docs/CLI.md>
- **Evidence quality:** production-proven

**Key techniques**

- Index = Metadata + repeated Document{relative_path, language, occurrences[], symbols[], optional text}; Occurrence{range, symbol, symbol_roles bitmask (Definition, Import, WriteAccess, ReadAccess, Generated, Test, ForwardDefinition), enclosing_range}; SymbolInformation{symbol, documentation, relationships[], kind, display_name, signature_documentation, enclosing_symbol}; Relationship{is_reference, is_implementation, is_type_definition, is_definition}.
- Symbol strings are a stable grammar: `<scheme> ' ' <manager> ' ' <package-name> ' ' <version> (<descriptor>)+` with descriptor suffixes '/' namespace, '#' type, '.' term, '().' method - so a symbol id is a globally unique, human-parseable string usable as a graph key across repos and versions.
- CLI: `scip print --json` dumps the whole index; `scip expt-convert --output x.db` (experimental) writes SQLite with tables documents(id, language, relative_path, position_encoding, text), chunks(document_id, chunk_index, start_line, end_line, occurrences BLOB zstd-compressed, 200 occurrences/chunk), global_symbols(symbol, display_name, kind, documentation, signature, enclosing_symbol, relationships), mentions(chunk_id, symbol_id, role) junction, defn_enclosing_ranges(document_id, symbol_id, start/end line/char); indexes on chunks by doc+line range, mentions by symbol+role, definitions by symbol/doc, global_symbols by symbol text. Also lint/stats/snapshot/test.
- Indexers exist for TypeScript, Python, Java/Scala/Kotlin, Go, C/C++, Ruby, .NET, PHP, Dart, Rust (rust-analyzer); Go/Rust/TS bindings read the protobuf directly.

**How to apply it.** When name-level fuzziness is not enough (overloaded method names, interface implementations, 'who actually calls this handler'), run the SCIP indexer in CI and ship the SQLite file: `mentions` joined with `global_symbols` answers 'all references to symbol S with role R' and `defn_enclosing_ranges` answers 'definition of S' with one indexed query, loadable via sql.js in the browser or plain sqlite3 server-side - no Sourcegraph, no server. The symbol grammar gives you a canonical entity id to store alongside jira/slack/confluence findings, and `enclosing_range` lets a log-template file:line be mapped to its enclosing function symbol, which then joins to the def/ref graph. Occurrences being an opaque zstd blob means 'who references' via mentions is cheap but exact positions require decoding a chunk.

**Limitations.** Indexers generally need a buildable checkout with dependencies resolved (scip-java/scip-go/scip-clang especially), so index freshness is tied to CI; full JSON dump is very large for big repos and expt-convert is explicitly experimental with an evolving schema; occurrence blobs must be decoded (zstd + protobuf) to get exact positions, so the frontend needs a small decoder or a precomputed side table; no fuzzy search - you must already have a symbol or name, so pair it with BM25/rapidfuzz for the entry point.

**Quotes**

> "<symbol> ::= <scheme> ' ' <package> ' ' (<descriptor>)+ | 'local ' <local-id>"

> "[EXPERIMENTAL] Convert a SCIP index to a SQLite database"

> "Occurrences are stored opaquely as a blob to prevent the DB size from growing very quickly."

## All sweep findings

_19 findings, sorted by relevance score._

### Xu et al., "Detecting Large-Scale System Problems by Mining Console Logs" (SOSP 2009)

- **Link:** <https://www.sigops.org/s/conferences/sosp/2009/papers/xu-sosp09.pdf>
- **Kind:** paper
- **Relevance:** `█████████░` 9/10

**What it is.** The canonical 'source code is the schema for your logs' technique. A single pass over the AST finds logger calls, turns each format/concatenation expression into a regex template (e.g. `starting: xact (.*) is (.*)`), resolves toString()/type hierarchy so interpolated objects expand into their own sub-templates, and records for every template the variable names, types, and the file:line of the logger call. Runtime lines are matched against a reverse index of templates; parse failure rate was 0.121% on 24M HDFS lines and 0.002% on Darkstar.

**Why it matters here.** This is exactly 'where is this error string produced' done deterministically: build the template table once (an LLM can help write the per-language extractor), ship it as a JSON/sqlite file, and a log line from logz.io maps to (file, line, variable bindings) with no LLM and no server. The extracted variable values (IDs) are precisely the entities you then feed to jira/slack/chronosphere tools.

### ast-grep (sg) — tree-sitter structural search CLI and library

- **Link:** <https://github.com/ast-grep/ast-grep>
- **Kind:** oss-project
- **Relevance:** `█████████░` 9/10

**What it is.** Rust CLI that parses code with tree-sitter and matches patterns written as real code with metavariables (`log.Printf($FMT, $$$)`), multi-threaded across files, with `--json` output, YAML rules, and library bindings (napi for Node, pyo3 for Python). The project's own comparison page positions it against semgrep (which cannot be embedded as a library and is slower as a CLI), comby (not syntax-aware), and GritQL.

**Why it matters here.** The practical engine for the Xu-style extractor: one pattern per logger idiom per language dumps every logging call site with its format string, file and line as JSON. Also answers 'who calls X' structurally for any tree-sitter language without an index; output is a static JSON you can precompute at repo sync and load in the browser.

### Aider: Building a better repository map with tree-sitter (+ RepoMapper standalone port)

- **Link:** <https://aider.chat/2023/10/22/repomap.html>
- **Kind:** blog
- **Relevance:** `█████████░` 9/10

**What it is.** Aider parses every file with tree-sitter using modified `tags.scm` query files to collect symbol definitions and references, builds a directed graph (files as nodes, references as weighted edges), runs personalized PageRank biased toward the files/symbols of interest, and emits the top definitions (with signatures) that fit a `--map-tokens` budget. pdavis68/RepoMapper (https://github.com/pdavis68/RepoMapper) is a standalone CLI reimplementation of the same idea.

**Why it matters here.** A fully deterministic, in-process 'what matters in this codebase' ranker. The def/ref graph it builds is the same thing you need for 'who calls this' at the symbol-name level (fuzzy, no type resolution), and the personalization vector is the hook: seed it with identifiers extracted from a ticket or log line and you get a ranked file list with no LLM. Output is plain text/JSON, trivially loadable by a web frontend.

### LocAgent: Graph-Guided LLM Agents for Code Localization (full text)

- **Link:** <https://arxiv.org/html/2503.09089>
- **Kind:** paper
- **Relevance:** `████████░░` 8/10

**What it is.** Parses a repo into a heterogeneous graph (directory/file/class/function nodes; contain/invoke/import/inherit edges) using Python's ast, and exposes three tools: SearchEntity (keyword lookup over a 4-level index: fully-qualified-name index, name dictionary, BM25 inverted index, chunk-to-entity index), TraverseGraph (type-aware BFS with entity/relation filters and hop limit), RetrieveEntity (path, line, code). Everything is local read-only structures; no server or DB. Reports 92.7% file-level localization.

**Why it matters here.** This is the closest published blueprint for the 'agent-shaped but programmable' navigator: the three tools are a tiny, deterministic API surface, and BM25 + graph BFS run in-process. The recorded traces of Claude Code choosing next steps can be compiled into fixed TraverseGraph/SearchEntity call sequences. Python-only graph builder, so you would swap in tree-sitter tags for other languages.

### SCIP index format + `scip` CLI (print --json, expt-convert to SQLite) and per-language indexers

- **Link:** <https://github.com/sourcegraph/scip/blob/main/docs/CLI.md>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** SCIP is a protobuf index (`index.scip`) of definitions, references, and symbol docs produced by scip-typescript, scip-python, scip-java (Java/Scala/Kotlin), scip-go, scip-clang, scip-ruby, scip-dotnet, scip-php, scip-dart, and rust-analyzer. The `scip` CLI has `print --json` to dump the whole index, `expt-convert` to turn it into a SQLite database, plus lint/stats/snapshot; Go/Rust bindings and generated TS bindings let you consume the index outside Sourcegraph.

**Why it matters here.** Precise (type-resolved) 'who calls this / where is it defined' as a static artifact: run the indexer in CI, convert to SQLite or JSON, and ship it with the frontend (sql.js in the browser or a sqlite file server-side). No Sourcegraph instance needed. Costs: indexers need a buildable checkout, and the JSON dump is large for big repos, so the SQLite route is the realistic one.

### Universal Ctags JSON output (tags with scope/signature/roles) + GNU Global for reference lookups and static HTML

- **Link:** <https://docs.ctags.io/en/latest/man/ctags-json-output.5.html>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** `ctags --output-format=json` emits one JSON line per tag with name, path, pattern, line, kind, scope/scopeKind, signature, language, and (with `--extras=+r --fields=+r`) reference tags with roles; `--list-fields` shows what is available. GNU Global (https://www.gnu.org/software/global/manual/global.html) builds GTAGS/GRTAGS databases (C/C++/Java/PHP/Yacc/asm built in, anything else via the universal-ctags plug-in parser), answers `global -r SYMBOL` (references) with `--result=grep|ctags-x|cscope`, and `htags` generates a static cross-referenced HTML site that needs no HTTP server unless -D/-f/--dynamic are used.

**Why it matters here.** Cheapest possible symbol index: a tags JSON file for 40+ languages that a browser can load into a Map, giving 'where is X defined' and (name-based, fuzzy) 'where is X referenced'. Global's GRTAGS gives reference lookups as local Berkeley-DB-style files, and htags is proof that a fully static cross-reference browser is viable.

### python `parse` library ('parse() is the opposite of format()') and Drain3 (in-process log template miner)

- **Link:** <https://pypi.org/project/parse/>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** `parse` compiles a Python format()-style string (`"user {uid} failed {reason}"`) into a matcher with `parse/search/findall` and typed named fields, so a logger's format string becomes an extractor with no hand-written regex. Drain3 (https://github.com/logpai/Drain3) is the complementary source-free path: a streaming Drain-algorithm miner that clusters log lines into templates with `<*>` wildcards, with FilePersistence (no Kafka/Redis needed).

**Why it matters here.** Bridge between extracted logging statements and runtime lines: format strings pulled by ast-grep/semgrep feed `parse` directly for Python-style templates, printf/`{}` templates need a small translation to regex. Drain3 covers third-party or unreachable source (templates from logs alone) and can be persisted as a local file that the frontend reads. Both are in-process libraries.

### Codebase-Memory: Tree-Sitter-Based Knowledge Graphs for LLM Code Exploration via MCP (paper + DeusData/codebase-memory-mcp)

- **Link:** <https://arxiv.org/html/2603.27277v1>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Single statically linked C binary MCP server over stdio (optional coordination daemon; CLI mode skips it) that indexes 162 tree-sitter languages into one SQLite file (~/.cache/codebase-memory-mcp, shareable as `.codebase-memory/graph.db.zst`), with content-hash incremental sync, Louvain community detection, and tools `trace_path` (callers/callees BFS 1-5 hops), `query_graph` (read-only openCypher subset), `search_graph` (regex/label + BM25 + vector), `get_architecture`. Paper: 83% answer quality vs 92% for file-exploration agent at 10x fewer tokens, 2.1x fewer tool calls; Linux kernel 2.1M nodes in ~3 min. MIT.

**Why it matters here.** Directly on-theme for 'tight token budget': a deterministic graph you can query with Cypher instead of paying an agent to grep. It is a process, but an MCP stdio process like the ones already in use, not an index server, and the SQLite file is a portable artifact a frontend can read via sql.js. Call resolution is name/import-based with LSP-assisted typing for ~12 languages, so expect some fuzz.

### CODEOWNERS libraries/CLIs (hmarr/codeowners Go, orsinium-labs/owners Python) + Backstage catalog-info.yaml spec.owner

- **Link:** <https://github.com/hmarr/codeowners>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** hmarr/codeowners is a Go library (`ParseFile`, `Match(path)` returning the winning rule's Owners) and CLI (`-o owner`, `-u unowned`), with a Rust port codeowners-rs; orsinium-labs/owners (`pip install owners`) has `owners-of PATH`, `owned-by @team`, `tree`. Backstage's Software Catalog stores ownership per component in a `catalog-info.yaml` committed next to the code with `spec.owner: group:payments-team` and system/dependsOn relations; git-who aggregates blame per tree as a fallback for repos with no CODEOWNERS.

**Why it matters here.** 'Which service/team owns this?' reduces to two deterministic lookups: last-matching-rule glob match against CODEOWNERS for a file path, and a parse of catalog-info.yaml (or your internal service catalog export) keyed by service name. Both are static files that can be snapshotted per repo and loaded by the frontend, then joined to Slack channels/PagerDuty schedules by owner string.

### ripgrep `--json` (JSON Lines with byte offsets) and `--multiline`, plus grep-ast for AST context around matches

- **Link:** <https://manpages.debian.org/testing/ripgrep/rg.1.en.html>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** `rg --json` emits begin/match/context/end messages with the line text, absolute byte offset, and submatch offsets, so scripts get exact spans without re-parsing; `-U/--multiline` (with `--multiline-dotall`) matches across lines and rg avoids reading whole files when the pattern cannot match a newline; `-A/-B/-C` add context records. grep-ast (https://github.com/Aider-AI/grep-ast, `gast`) wraps this idea with tree-sitter: each hit is shown inside its enclosing function/class, and its TreeContext class is usable from Python.

**Why it matters here.** The zero-index fallback and the glue for everything else: the frontend's 'search this substring' step can shell out to rg --json and post-process submatch offsets deterministically (e.g. pull an ID out of the line), and grep-ast turns 'string found at line N' into 'string emitted inside function F', which is what an agent normally infers by reading around the match.

### infigraph (Intuit) — embedded code graph with Cypher, no LLM, no external DB

- **Link:** <https://github.com/intuit/infigraph>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Rust tool that parses 62 languages with tree-sitter (plus ANTLR grammar plugins) into an embedded LadybugDB (Kuzu successor) stored under `.infigraph/`, supports full Cypher (WITH, OPTIONAL MATCH, variable-length paths), BM25 + Model2Vec hybrid search, 50+ CLI commands (search, query, trace-callers, dead-code, routes), an MCP server with 82 tools, and an optional localhost web UI. 'Zero LLM dependency. Runs locally. No API keys. No network calls.'

**Why it matters here.** Same shape as codebase-memory-mcp but with a richer Cypher surface and a CLI you can call per query without a daemon; the `.infigraph/` directory is a local artifact. Fits the constraint as an in-process/embedded store, though it is a large dependency and the graph's call edges are heuristic (name-based) rather than type-resolved.

### semgrep pattern syntax and `--json` for extracting logging statements

- **Link:** <https://semgrep.dev/docs/writing-rules/pattern-syntax>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** Semgrep patterns are code with `$METAVAR` and `...` ellipses (`log.Printf($FMT, ...)`, `logger.$METHOD($MSG, ...)`), metavariable-pattern/pattern-either compose rules, and `--json` returns each match with file, start/end line and the bound metavariable text. Semgrep runs offline as a CLI but cannot be embedded as a library and is slower than ast-grep.

**Why it matters here.** Alternative to ast-grep for the one-time 'harvest all logging call sites and their format strings' step, with a larger registry of ready-made language rules; output is static JSON. Choose it if the team already uses semgrep in CI; otherwise ast-grep is lighter.

### PyCG (Python) and Jelly (JS/TS) — static call graphs as JSON files

- **Link:** <https://github.com/cs-au-dk/jelly>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Jelly builds a flow-insensitive call graph and points-to analysis for Node.js JS/TS, writing a JSON call graph (`-j cg.json`) and a browsable HTML visualization (`-m`), intentionally unsound but practical for dynamic code. PyCG (https://github.com/vitsalis/PyCG) emits an adjacency-list JSON (`{"mod.func": ["mod.other"]}`) or FASTEN format, handles higher-order functions and inheritance, but is archived since Nov 2023.

**Why it matters here.** Where you need real (not name-matching) callers/callees for a specific language, these produce a static JSON graph a frontend can load and traverse with plain BFS. Per-language tools mean per-language pipelines; PyCG's archive status means budget for maintenance or a fork.

### OpenTelemetry code.* semantic conventions on log records (code.file.path, code.line.number, code.function.name)

- **Link:** <https://opentelemetry.io/docs/specs/semconv/general/logs/>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** Stable since semconv v1.33.0 (renamed from code.filepath/code.lineno/code.function), these attributes are meant to be attached to spans, log records, and exception events so telemetry can 'jump straight from an alert or trace to the exact line of code'. Many loggers already emit caller info (zap AddCaller, logrus ReportCaller, python %(pathname)s/%(lineno)d).

**Why it matters here.** Check this before building anything: if the logz.io records for a service already carry code.file.path/line (or a logger caller field), 'find the code that emitted this line' is a field read plus a git checkout, and only the services without it need the Xu-style template table.

### cgrep — local Tantivy index with definition/references/callers and JSON output

- **Link:** <https://github.com/meghendra6/cgrep>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Rust CLI combining BM25 (Tantivy) full-text with tree-sitter symbol awareness; commands `search`, `definition`, `references`, `callers`, `map`, `read`, and an `agent locate` / `agent expand` two-stage flow returning minimal payloads; `--format json2 --compact` for deterministic output; a daemon is optional (one-off commands auto-bootstrap the index). HN thread: https://news.ycombinator.com/item?id=47013067

**Why it matters here.** Illustrates the 'local index as files, no server' compromise: Tantivy is an in-process library, so this stays within the constraint, and the locate-then-expand pattern is the token-frugal navigation the developer wants to replicate. Young project; verify language coverage for your stack.

### Engines: Code Navigation for AI SWEs — what we've learned

- **Link:** <https://www.engines.dev/blog/code-navigation>
- **Kind:** blog
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Compares lsproxy (AGPL), stack graphs (poor language coverage, hard .tsg rules), Meta's Glean (only C++ parser in OSS, Thrift RPC), multilspy (easy but server lives only with the Python process), and Sourcegraph's precise API (enterprise-only); they settled on multilspy in Docker.

**Why it matters here.** Adjacent: a practitioner map of the serverful options and why each hurt. Useful mainly to justify the 'static index file' route over running language servers for a web frontend.

### GitHub stack-graphs / tree-sitter-stack-graphs (archived Sep 9, 2025)

- **Link:** <https://github.com/github/stack-graphs>
- **Kind:** oss-project
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Rust framework for declarative name-binding rules (.tsg files) on top of tree-sitter, giving incremental, zero-config precise navigation; the CLI does `index SOURCE_DIR` into a local database and `query definition FILE:LINE:COL`. GitHub archived the repo on 2025-09-09 ('no longer supported or updated ... fork it'). The engines.dev write-up notes it 'doesn't have great support for a variety of languages and adding additional support is difficult'.

**Why it matters here.** Instructive design (per-file incremental graphs, no build needed, local DB) but a dead end as a dependency now; language coverage was limited to Python/JS/TS/Java. Prefer SCIP indexers or tree-sitter tags.

### lsif-sqlite (Microsoft lsif-node tooling)

- **Link:** <https://www.npmjs.com/package/lsif-sqlite>
- **Kind:** oss-project
- **Relevance:** `████░░░░░░` 4/10

**What it is.** `lsif tsc -p tsconfig.json | lsif sqlite create -o project.lsif.db` converts an LSIF dump into a SQLite DB that can answer definition/references/hover requests without a running language server (the vscode-lsif-extension browses such DBs).

**Why it matters here.** Same idea as `scip expt-convert` for TypeScript/JavaScript only, and LSIF is being superseded by SCIP; keep as a fallback if the TS toolchain already emits LSIF.

### Woboq CodeBrowser — static HTML cross-reference site for C/C++

- **Link:** <https://woboq.com/codebrowser.html>
- **Kind:** oss-project
- **Relevance:** `████░░░░░░` 4/10

**What it is.** libclang-based generator that emits static HTML per file plus a `refs/` directory used as the reference 'database' for tooltips and an index per directory; 'no software is required on the server other than a basic web server that can serve files'.

**Why it matters here.** Adjacent (C/C++ only) but a concrete existence proof of the target architecture: precompute references into a directory of JSON-ish files and serve them statically to a browser UI.

## Dead ends

_Queries and directions that produced nothing useful. Listed so nobody repeats them._

- Generic queries like 'find source code that emitted log line / map log message to logging statement' return only log-formatting best-practice articles and logger docs; the useful material is under academic terms ('log parsing from source code', 'message template', Xu 2009) and in logtrail-tools (https://github.com/sivasamyk/logtrail-tools, Java/slf4j only, pattern file + parser), which is the only small OSS implementation found.
- arXiv abstract pages (CodexGraph 2408.03910, 'Code Isn't Memory' 2606.22417, LocAgent) contain no implementation detail; only the /html/ full-text pages do. CodexGraph relies on a Neo4j-style graph DB per the RepoQA benchmark paper, so it violates the no-server constraint and was not pursued.
- github/stack-graphs is archived (2025-09-09) and PyCG is archived (2023-11-26); do not plan on either as a dependency.
- Searching for a general 'printf format string to regex' library found nothing dedicated; Python `parse` covers format()-style only, so printf/`{}`/slf4j templates need a small custom translator.
- GitHub returned 429/403 intermittently on raw README fetches (gnu.org manual, hmarr/codeowners, lsif-node tooling); lsif-node tooling README is truncated online so its full tool list was not verified.
- Sourcetrail came up only in passing and was not verified (project is discontinued as far as known); Glean was rejected for OSS use because it ships only a C++ parser and needs Thrift RPC.
- site:news.ycombinator.com queries mostly surfaced Show HN posts for new agent-code-search tools (XRAY MCP, Semble, CodeRLM, VT Code) that wrap ast-grep/tree-sitter or embeddings; none added a technique beyond ast-grep + BM25 + graph already covered.
