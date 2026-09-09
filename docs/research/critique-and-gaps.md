# Critique and gap fills

_A completeness critic reviewed the draft report and listed what was missing. The highest-priority gaps were then researched separately._

## Gaps identified

### Gap 1: Pre-LLM program-synthesis-by-example for text extraction and substring selection was not searched: Microsoft PROSE / FlashFill / FlashExtract (Le & Gulwani 2014), Excel 'Column from Examples', Regel / RFixer / AlphaRegex (regex synthesis from positive/negative examples), classic wrapper induction (Kushmerick, RoadRunner), Snowball/DIPRE pattern bootstrapping from seed tuples, and Splunk's Interactive Field Extractor. The report's weakest row ('choosing which substring when several candidates exist') is exactly the problem FlashExtract solves deterministically from 2-3 highlighted spans, with no model at runtime and no model at synthesis time either.

- **Priority:** `█████████░` 9/10
- **Why it matters:** The report concludes context-dependent substring selection is 'unsolved in every angle' and routes all extractor synthesis through an LLM (Regexulator loop). A deterministic synthesizer that ranks programs by simplicity and validates on held-out spans could remove the LLM from step C entirely, handle typos/aliases via learned wrappers, and is directly usable in-process (PROSE SDK, prose-python bindings, Regel's Java implementation). It also gives a principled ranking for 'which of several candidates' via program consistency across traces.

### Gap 2: Battle-tested declarative 'extract from response, feed into next request' DSLs from ETL, API-testing, and RPA/low-code were not searched: Airbyte low-code declarative connectors (parent streams / SubstreamPartitionRouter, record selectors, paginators, error handlers), dlt transformers, Singer/Meltano, Hurl [Captures] with jsonpath/regex/xpath, Postman collections and Flows, Step CI, Karate, Tavern, k6, n8n/Windmill/Zapier expression languages, UiPath/Robocorp. Also GraphQL federation (Apollo @key entity resolution, Hasura remote joins, StepZen/Grafbase) and SPARQL 1.1 federated SERVICE as existing 'virtual graph over many APIs' engines.

- **Priority:** `████████░░` 8/10
- **Why it matters:** The report proposes inventing an Arazzo-plus-extract-plus-forEach YAML and a ~200-line interpreter, but several mature DSLs already ship exactly these semantics with pagination, retries, rate-limit handling, partitioning/fan-out, and regex/jsonpath captures over free-text bodies. Reusing one (or its schema) cuts implementation risk and gives the developer a concrete file format to author link rules in; Apollo-style @key resolvers are the closest existing model of 'MCP servers as a graph database'.

### Gap 3: The report never examines the fuzziness already built into the target backends' own query languages and what the specific MCP tools accept: Slack search modifiers (in:, from:, after:, before:, has:, quoted phrases), Confluence CQL (text ~ fuzzy, title ~, ancestor, lastModified), JQL (text ~, summary ~, wildcards, ORDER BY), Elasticsearch/Logz query_string, match_phrase_prefix, fuzziness, wildcard on keyword fields, PromQL label regex matchers (=~), Google Drive API q (fullText contains, name contains), git log -S/-G pickaxe and git grep. It also does not cover pagination limits, result caps, rate limits, or time-window parameters of each MCP tool.

- **Priority:** `████████░░` 8/10
- **Why it matters:** Much of the agent's 'phrasing a search query' fuzziness can be delegated to the backend (query_string with OR of extracted IDs, CQL ~ fuzzy, ES fuzziness:AUTO) instead of reimplemented locally; that changes the design of P4 templates and the 'free-text query phrasing' row rated poorly. Without knowing the exact parameter schemas, pagination and rate limits, the link table and fan-out rules cannot be designed and budgets cannot be estimated.

### Gap 4: Investigation-UI and 'ops notebook' prior art is thin (only MCP Inspector, Maltego, korrel8r graph view): Transposit (SQL joins over SaaS APIs built for on-call), Fiberplane notebooks, Jupyter runbook automation (Netflix papermill runbooks, nurtch/rubix), Datasette and its plugin ecosystem as a UI over SQLite, Observable notebooks, Splunk workflow actions and Kibana/Datadog drilldown URL templates (click a field value → templated query elsewhere), Rundeck/Shoreline/PagerDuty Automation Actions, Chrome DevTools Recorder and Selenium IDE record-and-replay, and Nushell/jc/Miller structured-pipeline tooling.

- **Priority:** `███████░░░` 7/10
- **Why it matters:** The deliverable is a web frontend; the report's section I is a one-paragraph sketch. Drilldown/workflow-action designs are the mainstream, LLM-free version of 'which tool next' surfaced to a human, and Datasette/Observable give ready-made UIs over exactly the SQLite index the report recommends. Transposit is direct prior art for the whole idea and its post-mortem would inform scope.

### Gap 5: No evaluation methodology or corpus-size guidance for the trace→template pipeline, and several load-bearing numbers in the report are unverified or look wrong: FTS5 'always full-scans' (FTS5 MATCH uses an inverted index), Semble ablation (+0.16 vs +0.02 NDCG), PagerDuty Related Incidents '5 minutes' rule, Model2Vec potion-base-32M dimensionality, Claude Code hook payload fields (prompt_id), Inspector CLI exit codes 3/4/5, RCACopilot 'four years' and '120–140-word summary', Blueprint First '~60% accepted unmodified', anthropics/oncall-kit existence, stack-graphs/PyCG archived status, and the '~200 line interpreter' / '~300 lines to reimplement Regexulator' estimates.

- **Priority:** `███████░░░` 7/10
- **Why it matters:** The developer will decide how many traces to record before starting and how to know the deterministic replay is faithful; the report gives Progressive Crystallization's thresholds but no offline replay-benchmark design. Wrong facts (FTS5 scanning, embedding dims, hook fields) would derail the index and recorder implementations early.

### Gap 6: Practitioner and 'LLM as compiler' community sources were only lightly touched: HN/Reddit/Lobsters threads and blog posts from people who compiled agent traces or Claude Code hook logs into scripts, DSPy-style compile-then-freeze, TypeChat/Instructor 'schema-fixed one-call' patterns, Tessl/Sourcegraph/Continue 'codify the workflow' efforts, SREcon 2025-2026 and KubeCon talks on deterministic AI-SRE playbooks, PagerDuty/incident.io/Rootly product blogs on catalog-driven automation, and semantic-web entity-linking work (URI minting per Jira key, JSON-LD @context over tool results, RDF/SPARQL federation) as the formal 'IDs as global identifiers' model.

- **Priority:** `█████░░░░░` 5/10
- **Why it matters:** The report admits its HN searches were generic dead-ends; targeted practitioner reports would tell the developer what broke in practice (schema drift, auth expiry, rate limits, trace noise) and whether anyone has open-sourced a trace-to-script compiler since mid-2026. The semantic-web framing supplies a ready vocabulary for the entity catalog (P7) and cross-server ID resolution the report says has no MCP convention.

## Claims in the draft that lacked a source

_These were either sourced, softened, or removed in the final report._

- P6 limits: 'FTS5 always full-scans (fine to low hundreds of thousands of rows)' — no source, and FTS5 MATCH queries use an inverted index; likely a confusion with sqlite-vec brute-force KNN or trigram substring scans.
- P6: Semble ablation figures ('hand-written rerank rules on BM25 gave +0.16 NDCG while adding the vector leg gave only +0.02') are attributed to the Semble repo/HN thread without a quoted table; unverified.
- P8: 'PagerDuty Related Incidents: two incidents are related if within 5 minutes and one service depends on the other' — the linked PagerDuty doc describes ML-based relatedness plus service dependencies; the exact 5-minute rule is not evidenced.
- P8: 'RCACopilot: per-alert-type handlers ran the diagnostic collection in production for four years before any LLM was added' and Section 5 'RCACopilot 120–140-word summary' — specific figures not tied to a quoted passage.
- Section 5: 'Blueprint First: ~60% of drafts accepted unmodified' — number not sourced to a quoted result.
- Section 4A: Claude Code PostToolUse hook payload fields listed as {session_id, prompt_id, tool_use_id, tool_name, tool_input, tool_response} — 'prompt_id' and 'tool_use_id' presence not verified against the hooks doc.
- P5: MCP Inspector CLI exit-code classes '3 auth, 4 unreachable, 5 tool error/isError' — cited to a dated docs URL (2026-07-28) that is not quoted; unverified.
- P5 / Section 6: 'every vendor server surveyed returns JSON-in-text, not structuredContent' and 'No server emits resource_link blocks' — a survey conclusion with no per-server evidence shown (Rovo, Slack, Grafana, PagerDuty, Google Drive, Logz.io).
- Section 4E: 'Model2Vec potion-base-32M ... 256-d' and P6 'Model2Vec static embeddings (8–30 MB ... ~500× faster)' — model name/dimensions not tied to the cited blog; 'no browser Model2Vec package (hand-port ~50 lines)' is an unsourced estimate.
- Section 4B: 'the interpreter is ~200 lines (arazzo-runner is a reference)' — the link points to the Arazzo spec, not to any runner implementation; line-count estimate unsupported.
- Section 4C: 'Regexulator-style FP/FN loop ... (~300 lines to reimplement)' — unsupported estimate.
- Section 3 (free-text query phrasing): 'Blind PRF-style expansion is a documented negative result' — no source cited.
- Section 6: 'stack-graphs and PyCG are archived' and 'Semgrep can't be embedded' — no sources cited.
- P3: 'Regexulator's own tumour-size failure is this case' — referenced as evidence for context-dependent selection failure without a quoted passage.
- Source list: 'https://github.com/anthropics/oncall-kit — mining incident history into playbooks with held-out validation' appears only in the source list, is never used in the body, and its existence/content is unverified.
- P1: Progressive Crystallization thresholds ('≥10 identical-sequence runs ... ≥50 runs with ≥99% classification consistency') and the '0% to 45% in eight months, >70% lower cost' outcome are cited to a 4-page paper the report itself calls 'thin on mechanism'; the mechanism attributions (auto-generated regression suite from traces, firmware-update demotion example) should be confirmed as actually in the paper.
- P2: 'Compiled AI ... break-even at ~17 executions, 57× fewer tokens at 1,000' and Section 1 'collapsed to ~20% on semantic fields' vs Section 3 'DocILE regex 10–20% on names/amounts' — internally inconsistent figures for the same result.
- P4: 'AutoRestTest ... embedding-similarity edge inference, 0.7 threshold' and P4 'Firefly ... over ~1,000 real MCP tools' — specific numbers not quoted from the papers.
- P10: aider weighting details ('sqrt(refs)', '×10 for long snake/camel identifiers', '×0.1 for identifiers defined in >5 files') — attributed to repomap.py without quoted code; exact constants unverified.

## Gap-fill research

## Fill for gap 1: Pre-LLM program-synthesis-by-example for text extraction and substring selection was not searched: Microsoft PROSE / FlashFill / FlashExtract (Le & Gulwani 2014), Excel 'Column from Examples', Regel / RFixer / AlphaRegex (regex synthesis from positive/negative examples), classic wrapper induction (Kushmerick, RoadRunner), Snowball/DIPRE pattern bootstrapping from seed tuples, and Splunk's Interactive Field Extractor. The report's weakest row ('choosing which substring when several candidates exist') is exactly the problem FlashExtract solves deterministically from 2-3 highlighted spans, with no model at runtime and no model at synthesis time either.

_Researcher's framing: Pre-LLM programming-by-example (PBE) and wrapper induction for turning recorded (text, chosen-span) agent traces into deterministic extractors that pick the right substring when several candidates exist. NOTE ON METHOD: the session's WebSearch budget was already exhausted (200/200) when this subagent started, so only 1 search ran; everything below was verified by WebFetch of primary sources (papers extracted locally with pypdf, GitHub/NuGet/OpenAlex/PyPI pages), with dead ends listed honestly.

HEADLINE: The problem the report calls unsolved ('which of N candidates') is exactly what FlashExtract's position DSL solves without any model: a span is described as Pair(Pos(start), Pos(end)) where Pos = RegPos((r1,r2),k) = 'the k-th position whose left side ends with token-regex r1 and right side starts with r2' (k negative = from the right), plus dynamic literal tokens mined from the text near the highlighted spans (e.g. '"Sample ID:"'). Candidate programs are intersected across examples (version-space), negative instances are enforced (Y' ∩ P(σ) = ∅), and consistent programs are ranked by simplicity and by subsumption (prefer the program that extracts fewer regions). FlashExtract needed 2.36-2.86 examples per field and 0.8 s on 75 real docs. STALKER's landmark automata (SkipTo(l1) SkipTo(l2) with wildcards _Number_/_Punct_/_HtmlTag_, learned by sequential covering, disjunctions for heterogeneous formats, hierarchical parent-then-child extraction) learned perfect rules from a single example for >half the rules and never needed more than 9. Neither has a usable in-process Python/JS build: PROSE is .NET, non-commercial-only, frozen Oct 2025; STALKER has no public code. So the realistic recipe is to re-implement the ~300-line FlashExtract/STALKER core in Python (the DSL and learn methods are fully specified in the papers), and use Snowball-style weighted left/middle/right context vectors as the fuzzy fallback ranker.

RECIPES (no LLM at synthesis or runtime):
(A) FlashExtract-style position synthesis. Tokens = ~30 char classes + 'dynamic tokens' = literals that occur within ±N chars of the chosen spans in ≥2 traces ('pod=', 'trace_id:', 'PROJ-', 'affected service:'). For each trace (text, [s,e]) enumerate start programs {AbsPos(k)} ∪ {RegPos((r1,r2),k): r1 = concat of ≤3 tokens matching a suffix of text[:s], r2 matching a prefix of text[s:], k = rank of that (r1,r2) match among all its matches in the text (from left or right)}; same for the end. Intersect the candidate sets across all traces for the same (source tool, target tool, param). Negatives come free from the trace: every other substring in the same text that matches the value-shape but was not chosen. Rank survivors: RegPos > AbsPos, dynamic literal token > generic class, fewer tokens, smaller |k|, and FlashExtract's CleanUp (drop programs whose output set is a superset of another survivor's). Validate on held-out traces; if no program survives, split the trace set by detected source format (Slack vs log vs wiki) and synthesize a Merge (disjunction) per format, which is exactly FlashExtract's N1 ::= Merge(SS1..SSn).
(B) STALKER landmark rules for the enclosing region first, then the value (hierarchical): 'SkipTo(Affected) SkipTo(:)' then value-level rule. Sequential covering generates a disjunction only when a single linear rule cannot cover all examples, so heterogeneous MCP results (JSON blob vs prose) get one branch each.
(C) Snowball/DIPRE context-vector ranking as the fuzzy fallback: candidate generator = union of typed regexes (Jira key, UUID/trace id, pod name, namespace, host, URL); for each trace compute weighted bag-of-terms vectors for a w-token window left, middle, right of the chosen span; cluster into patterns (single-pass, similarity threshold); at runtime score each candidate by inner product with the pattern centroids and pick the max; keep per-pattern selectivity (how often the pattern picks a candidate that later 'worked') to weight patterns. This tolerates extra commas, typos and aliases where DSL (A) demands exact tokens, and bootstraps from a handful of seeds. All in-memory / sqlite.
(D) grex (Rust, Apache-2, pip + WASM) learns the value-shape regex from the set of chosen values alone (positive-only, DFA-minimised, optional \d/\w generalisation) — use it to generate candidates for (A)/(C), never to choose among them (no context).
(E) Drain3 (pure Python) mines log-line templates with <*> parameter slots and extract_parameters() returns (value, mask type); use it to enumerate the variable slots of a log line, then (A)/(C) pick which slot.
(F) RegexGenerator (Trieste; Java, GPL-3) is the only OSS tool whose input is literally 'text + highlighted spans' with unhighlighted text as negatives; run it offline once per extractor if you have tens of spans; keep only the regex at runtime.

WHERE THESE FAIL vs LLM-written regex (Regexulator/DeepParse): (1) PBE requires ONE program consistent with ALL traces; if the agent chose by meaning (service named in the ticket title rather than in the stack trace) with no lexical cue, the version space empties or overfits to a disjunction of one branch per trace. (2) Agent noise: one inconsistent trace kills synthesis; cluster/dedupe traces (or drop outliers, as FlashFill does for noisy inputs) before learning. (3) Exact-token DSLs cannot absorb typos/aliases; only (C) or a normalisation table does. (4) Composing free-text search queries is a transformation, not an extraction — FlashFill-style Concat(substr, const) covers fixed templates only. (5) Whole-string regex synthesizers (Regel/AlphaRegex/RFixer) decide membership of whole strings, not localisation inside long text; Regel's PBE-only mode solved just 26% of 322 real tasks with ~4 positive + ~5 negative examples, and Regel's own thesis is that examples alone under-specify intent — the same reason a one-time LLM-written sketch or NL cue may still be needed for the residual cases. (6) Licensing: the only polished production engine (PROSE) is non-commercial-only and .NET; RegexGenerator is GPL-3._

### FlashExtract: A Framework for Data Extraction by Examples (Le & Gulwani, PLDI 2014)

- **Link:** <https://www.microsoft.com/en-us/research/wp-content/uploads/2016/12/pldi14-flashextract.pdf>
- **Kind:** paper
- **Relevance:** `██████████` 10/10

**What it is.** Text DSL (Fig. 7): a region is Pair(Pos(R0,p1), Pos(R0,p2)); a position attribute is AbsPos(k) or RegPos((r1,r2),k), 'the k-th position whose left side matches regex r1 and right side matches r2' (k<0 counts from the right); a regex is ≤3 tokens from ~30 char classes plus 'dynamic tokens' mined from frequent literals near the user's highlights. Sequences use LinesMap/StartSeqMap/EndSeqMap, FilterBool (Starts/Ends/Contains predicates incl. previous/next line), FilterInt(init,iter), Merge for disjunction. Learning is per-operator witness functions + version-space intersection; negative instances enforced (Y' ∩ P(σ)=∅); CleanUp drops any program whose output is a superset of a retained program's (ranking by subsumption, favouring 'user gave consecutive examples from the start'). Evaluation: 75 documents, 2.36 (abstract) / 2.86 (body) examples per field on average, 0.82-0.84 s synthesis; text files needed the most examples.

**Why it matters here.** This is the exact mechanism for 'which of N candidates': (r1,r2,k) triples encode left context, right context and ordinal, and a literal dynamic token like 'trace_id:' is a first-class token. The algorithm is fully specified in the paper and small enough to re-implement in Python from the (text, span) traces; negatives are derivable automatically from unchosen candidates in the same text.

### Automating String Processing in Spreadsheets Using Input-Output Examples (FlashFill, Gulwani, POPL 2011)

- **Link:** <https://www.microsoft.com/en-us/research/wp-content/uploads/2016/12/popl11-synthesis.pdf>
- **Kind:** paper
- **Relevance:** `████████░░` 8/10

**What it is.** Origin of the Pos(r1, r2, k) position expression, the version-space (DAG) representation of all consistent SubStr/Concat programs, and the ranking heuristics (prefer regex-based positions over constants, fewer tokens, smaller k). Also has noise detection and an interactive disambiguation model. Shipped as Excel Flash Fill.

**Why it matters here.** Gives the transformation half: if a downstream parameter is a template like '<service> <error class>', Concat of SubStr positions + constants can be synthesised from traces. Its ranking heuristics are the standard answer to 'several programs fit the examples, which one generalises'.

### Inference of Regular Expressions for Text Extraction from Examples (Bartoli, De Lorenzo, Medvet, Tarlao, IEEE TKDE 2016) + RegexGenerator

- **Link:** <https://github.com/MaLeLabTs/RegexGenerator>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** Multi-objective genetic programming that evolves an extraction regex from text in which the desired matches are highlighted; everything not highlighted acts as negative; produces regexes with lookarounds/context. Abstract (DOI 10.1109/tkde.2016.2515587): 'based solely on examples of desired behavior', 'results are highly competitive even with respect to human operators'. Java (ConsoleRegexTurtle CLI + GP engine), GPL-3.0, web demo at regex.inginf.units.it.

**Why it matters here.** Its input format is literally your trace format (text + span). Unlike FlashExtract it searches a general regex space stochastically, so it needs more examples and minutes of CPU, but it handles messy free text and multiple candidates via learned context. GPL-3 and JVM are the obstacles; run offline once per extractor and keep the regex.

### Hierarchical Wrapper Induction for Semistructured Information Sources (STALKER; Muslea, Minton, Knoblock, JAAMAS 2001)

- **Link:** <https://usc-isi-i2.github.io/papers/muslea01-jaamas.pdf>
- **Kind:** paper
- **Relevance:** `████████░░` 8/10

**What it is.** Extraction rules are linear landmark automata: sequences of SkipTo(landmark) where a landmark is a token sequence with wildcards (Number, Punctuation, HtmlTag, Anything); start and end rules learned separately; rules can be disjunctive. Induction is sequential covering: pick the shortest uncovered example as seed, generate initial candidates from its last token and matching wildcards, refine by adding landmark tokens, keep 'perfect disjuncts', repeat on uncovered examples. Documents are modelled as an embedded-catalog tree so extraction is hierarchical (parent region first). Results: 'requires no more than 9 examples for any rule ... for more than half of the rules it can learn perfect rules based on a single example'; up to two orders of magnitude fewer examples than WIEN; handles missing/permuted fields.

**Why it matters here.** Directly gives (i) a small deterministic rule language over tokens with wildcards, (ii) a covering algorithm that yields one disjunct per source format, and (iii) hierarchical extraction that mirrors nested MCP results (find the field/line first, then the value). One-example learning matches the sparse trace regime.

### Snowball: Extracting Relations from Large Plain-Text Collections (Agichtein & Gravano, DL 2000) and DIPRE (Brin 1998)

- **Link:** <https://www.cs.columbia.edu/~gravano/Papers/2000/dl00.pdf>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Starts from a handful of seed tuples, finds their occurrences, and represents each context as a 5-tuple <left, tag1, middle, tag2, right> where left/middle/right are weighted term vectors (middle weighted highest); patterns are centroids of single-pass clusters of these vectors; a new candidate matches a pattern by inner product above a threshold; pattern confidence = selectivity (how often it produces known-good tuples) and tuple confidence combines the confidences of the patterns that produced it; only reliable patterns/tuples survive to the next bootstrapping iteration. DIPRE used exact longest-common-substring left/middle/right strings.

**Why it matters here.** The fuzzy, in-process alternative to exact DSLs: learn weighted left/right context windows around each chosen span from traces, score all typed candidates in a new tool result, pick the max. Tolerates commas, typos, aliases; bootstraps from few seeds; needs only in-memory vectors or sqlite. Replace the named-entity tagger with typed regex candidate generators.

### Microsoft.ProgramSynthesis.Extraction.Text (PROSE SDK) on NuGet

- **Link:** <https://www.nuget.org/packages/Microsoft.ProgramSynthesis.Extraction.Text/>
- **Kind:** product
- **Relevance:** `██████░░░░` 6/10

**What it is.** Production FlashExtract engine as a .NET library (net8.0 / netstandard2.0 / net462), v10.16.5 released 2025-10-06. README states 'The PROSE SDK is available for non-commercial use only.' The GitHub repo microsoft/prose (MIT) contains only samples; it says 'As of October 14, 2025, we have stopped releasing new versions of the PROSE SDK' and the MS Research page says 'PROSE is not open source'. The current api-samples/Extraction.Text/SampleProgram.cs shows a table-shaped API: Table<ExampleCell> examples, session.AddExample(data, example), session.Learn(), program.Run(data).

**Why it matters here.** The only polished implementation of FlashExtract, but .NET-only, non-commercial licence, frozen; the old docs site (microsoft.github.io/prose/documentation/extraction-text/) now 404s. Usable for a one-off offline experiment to check whether a position program exists for your traces, not as an in-process component of a commercial web frontend.

### Regel: Multi-modal Synthesis of Regular Expressions (Chen, Wang, Ye, Durrett, Dillig, PLDI 2020)

- **Link:** <https://arxiv.org/abs/1908.03316>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6/10

**What it is.** Parses an English description into a hierarchical sketch and completes it by enumerative PBE search with symbolic-regex pruning over Concat/Or/Star/Optional/Repeat/character classes/NotContain, using positive/negative example strings. On 322 tasks Regel solves 80%, the NL-only baseline (DeepRegex) 43%, and the PBE-only baseline 26%; the adapted DeepRegex set averages 4 positive and 5 negative examples per task; PBE engine is an order of magnitude faster than an AlphaRegex adaptation; interactive mode proposes distinguishing inputs.

**Why it matters here.** Regexes here classify whole strings, not spans inside long text, so it is a value-shape learner at best. The 26% PBE-only number is the honest ceiling for 'examples alone' on realistic regexes and is the strongest argument for keeping a one-time LLM (or a hand-written sketch) for the residual hard cases.

### Wrapper Induction: Efficiency and Expressiveness (Kushmerick, Artificial Intelligence 118, 2000; WIEN)

- **Link:** <https://doi.org/10.1016/s0004-3702(99)00100-9>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6/10

**What it is.** Defines the LR, HLRT, OCLR, HOCLRT, N-LR, N-HLRT wrapper classes: an LR wrapper is a vector of 2K delimiter strings (left and right literal context) for K attributes; HLRT adds head/tail delimiters to skip confusing text; OCLR adds per-tuple open/close delimiters. Induction searches candidate delimiters among suffixes/prefixes of the text around labelled values; the paper gives PAC-style bounds on examples. Assumes ordered attributes, so missing/permuted fields are not handled (per Chang et al. survey).

**Why it matters here.** The simplest deterministic baseline: for a (tool, param) pair, learn the longest literal left delimiter and right delimiter shared by all traces. Works for templated tool output (Jira/PagerDuty fields), fails on free text — which motivates FlashExtract's token regexes and Snowball's soft contexts. Primary PDFs were unreachable; details confirmed via the Chang et al. survey.

### Drain3 – online log template mining (logpai)

- **Link:** <https://github.com/logpai/Drain3>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Pure-Python streaming log parser that clusters lines into templates with <*> wildcards; masking rules pre-normalise IPs/numbers; extract_parameters(template, line) returns the variable values with their mask type; persistence to file/Kafka/Redis.

**Why it matters here.** Enumerates the variable slots of a log line deterministically in-process, so 'which substring' reduces to 'which slot', which can then be learned from traces with a tiny classifier or by slot index per template. Complementary to (r1,r2,k) positions for Logz.io results.

### microsoft/prose GitHub repository (samples, MIT) and api-samples/Extraction.Text

- **Link:** <https://github.com/microsoft/prose>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Sample code for the SDK's DSLs: Detection, Extraction.Json/Text/Web, Matching.Text, Read.FlatFile, Split.Text, Transformation.Formula/Json/Text. Samples are MIT; the SDK binaries they call are non-commercial. States new releases stopped 14 Oct 2025.

**Why it matters here.** Useful to read the Extraction.Text sample to see what a learned extraction program looks like and how examples/negatives are expressed, before re-implementing the core in Python.

### utopia-group/regel implementation (MIT)

- **Link:** <https://github.com/utopia-group/regel>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Python 3.7 + Java (SEMPRE semantic parser) + Z3; Dockerfile provided; benchmarks as '$string$,+/-' lines; --synth_mode 5 runs the PBE engine with no sketch (examples only); interactive.py for iterative refinement.

**Why it matters here.** In-process is a stretch (JVM + Z3 + a trained parser model), but the pure-examples mode could be run once offline over trace-derived positive/negative value sets to produce a regex kept at runtime. MIT-licensed.

### RFixer: Automatic Repair of Regular Expressions (Pan, Hu, Chen, D'Antoni, OOPSLA 2019)

- **Link:** <https://github.com/rongpan/RFixer>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Given an incorrect regex plus positive and negative example strings, synthesizes the closest consistent regex; templates with holes for character classes solved via SMT (Z3), automaton- or regex-directed encodings, Max-SMT and CEGIS options. Java/Maven. Abstract (via OpenAlex, DOI 10.1145/3360565): scales to large UTF-16 alphabets and computes minimal repairs where prior tools failed. No LICENSE file found in the repo.

**Why it matters here.** Fits a 'patch the extractor when a new trace contradicts it' loop: keep the old regex, add the new positive/negative values, get a minimal repair with no LLM. Needs a JVM and Z3, and repairs whole-string regexes, not in-context spans.

### grex – regex generator from positive examples (Rust, Python bindings, WASM)

- **Link:** <https://github.com/pemistahl/grex>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Builds a minimal DFA over the given strings (Hopcroft minimisation, Brzozowski conversion) and emits a regex guaranteed to match exactly them, with optional generalisation flags (\d, \w, {min,max} repetition). Apache-2.0; pip package and WebAssembly build for browser/Node.

**Why it matters here.** Zero-effort value-shape learner from the set of values the agent actually used (Jira keys, pod names); positive-only and context-free, so it only generates candidates — pair it with a context ranker.

### A Survey of Web Information Extraction Systems (Chang, Kayed, Girgis, Shaalan, IEEE TKDE 2006)

- **Link:** <http://staff.csie.ncu.edu.tw/chia/pub/iesurvey2006.pdf>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Compares WIEN (LR/HLRT delimiter vectors), SoftMealy (finite-state transducer with contextual separator rules; handles missing/permuted attributes), STALKER, WHISK (covering algorithm, multi-slot '*(*)*(*)*' patterns over syntactic/semantic tagged text), RAPIER (single-slot pre-filler / filler / post-filler patterns with POS and WordNet classes), RoadRunner (unsupervised), and DIPRE/Snowball, by input needed and limitations.

**Why it matters here.** One-stop reference for the design space: delimiter wrappers (exact), landmark automata (tokens + wildcards), FSTs (permutations), and pre/filler/post patterns (free text). RAPIER's pre-filler/filler/post-filler triple is the same shape as FlashExtract's (r1, value, r2) and Snowball's (left, middle, right).

### RoadRunner: Towards Automatic Data Extraction from Large Web Sites (Crescenzi, Mecca, Merialdo, VLDB 2001)

- **Link:** <http://www.vldb.org/conf/2001/P109.pdf>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Fully unsupervised wrapper generation: compare two (or more) pages of the same class with the ACME matching algorithm; mismatches become data fields, matches become template; output is a union-free regular expression. No labelled examples at all.

**Why it matters here.** For MCP tool results that are templated (rendered tickets, incident pages, cron-generated wiki pages) you can infer the template and the variable slots from two results without any trace; then only the slot→parameter mapping needs learning. Not for genuinely free text.

### Sumo Logic parse anchor operator + 'Parse Selected Text' UI

- **Link:** <https://www.sumologic.com/help/docs/search/search-query-language/parse-operators/parse-predictable-patterns-using-an-anchor/>
- **Kind:** spec
- **Relevance:** `█████░░░░░` 5/10

**What it is.** `| parse "user=*: severity=*:" as user, severity` — literal left/right anchors with * for the value; users can highlight text in a log and choose 'Parse Selected Text' to build the anchor pattern interactively (and an 'AI Parse Assist' variant exists).

**Why it matters here.** Industrial confirmation that the LR-delimiter wrapper (literal left anchor, literal right anchor) is what production log tools use for parse-by-example; a trivial synthesizer: take the longest literal left/right context common to all traces.

### Power Query 'Add a column from examples' (Excel/Power BI, built on PROSE)

- **Link:** <https://learn.microsoft.com/en-us/power-query/column-from-example>
- **Kind:** spec
- **Relevance:** `████░░░░░░` 4/10

**What it is.** User types example outputs for some rows; the engine infers an M transformation and previews it. Supported text extractions: First/Last Characters, Range, Text before Delimiter, Text after Delimiter, Text between Delimiters, Remove/Keep Characters, plus Combine/Replace and conditional columns; operates on the top 100 rows only.

**Why it matters here.** Shows the practical PBE menu for extraction: delimiter-relative (before/after/between the n-th delimiter) and positional. A Python re-implementation of just these six operators plus FlashExtract's token positions covers most 'pull the id out of this field' cases.

### Information-theoretic User Interaction: Significant Inputs for Program Synthesis (Ji, Sivaraman, Gulwani, Mooney, Chaudhuri, 2020)

- **Link:** <https://arxiv.org/abs/2006.12638>
- **Kind:** paper
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Turns a passive PBE learner (FlashFill/FlashExtract style) into an active one: sample programs from the version space, estimate which unlabeled inputs have maximal conditional entropy (candidate programs disagree most), cluster inputs, and ask about representatives; converges in few iterations on ~800 string tasks.

**Why it matters here.** With thousands of recorded traces you do not need all of them: replay traces in the order that maximises disagreement among surviving extractor programs, and only consult the (one-time) LLM or a human on those, removing the LLM from the routine path.

### prose-codeaccelerator on PyPI is a dependency-confusion placeholder

- **Link:** <https://libraries.io/pypi/prose-codeaccelerator>
- **Kind:** product
- **Relevance:** `███░░░░░░░` 3/10

**What it is.** The PyPI package named after Microsoft's Python 'PROSE Code Accelerator' (released 2023-03-01, 0 deps) is labelled 'Ethical research - dep confusion package. This is not a real package, do not install.' The real Code Accelerator was distributed from a Microsoft feed, not PyPI, and there is no other official Python binding.

**Why it matters here.** Warning: 'pip install prose-codeaccelerator' does NOT give you FlashExtract in Python. There are no first-party Python/JS/WASM bindings for PROSE.

### Splunk Interactive Field Extractor (field extractor / IFE)

- **Link:** <https://docs.splunk.com/Documentation/Splunk/latest/Knowledge/Buildfieldextractionswiththefieldextractor>
- **Kind:** product
- **Relevance:** `███░░░░░░░` 3/10

**What it is.** From product knowledge, NOT verified against the docs in this session (docs.splunk.com, help.splunk.com, Lantern and the Splunk blog all returned 403/404): the user highlights a value in a sample event, Splunk generates a PCRE with a named capture group and shows matches across sample events; the user can add more highlighted samples, mark wrong matches with 'Remove' (counterexamples), add 'required text' that must appear in the event, and edit the regex by hand; a delimiter-based mode exists for CSV-like events.

**Why it matters here.** Same interaction model as FlashExtract (positive spans + counterexamples + required-text = FilterBool predicate), which is evidence the model is usable for log data; but Splunk's generator is closed and not reusable.

### AlphaRegex (Lee, So, Oh, GPCE 2016) – kupl/AlphaRegexPublic

- **Link:** <https://github.com/kupl/AlphaRegexPublic>
- **Kind:** oss-project
- **Relevance:** `██░░░░░░░░` 2/10

**What it is.** Enumerative regex synthesis from positive/negative strings with over/under-approximation pruning; OCaml, MIT; targeted at introductory automata assignments (binary alphabets, e.g. outputs 0(0+1)*).

**Why it matters here.** Historically important but too small-alphabet/educational for log text; Regel's PBE engine supersedes it.

### Dead ends

- WebSearch: the session budget was already exhausted (200/200) before this subagent ran; only the first FlashExtract query executed, so the '6 distinct searches' requirement could not be met — all findings were obtained by fetching known primary URLs, GitHub/NuGet/OpenAlex/PyPI APIs, and extracting PDFs locally with pypdf.
- Splunk field extractor documentation: docs.splunk.com (latest, 9.1.0, 9.4.1) returned 403; help.splunk.com paths (9.4, 10.0) returned 404; lantern.splunk.com and splunk.com blog guesses 404; r.jina.ai proxy 401; DuckDuckGo html and Bing returned CAPTCHA/no snippets. The Splunk IFE entry is therefore from product knowledge, not verified.
- PROSE documentation site microsoft.github.io/prose/documentation/extraction-text/ now 404s and web.archive.org is not fetchable from this tool, so the old RegionLearner/SequenceLearner API details could not be re-verified; the current api-sample shows a table-shaped Session.AddExample API instead.
- prose-playground.cloudapp.net/data/LICENSE.txt (PROSE SDK licence text) no longer resolves; the non-commercial restriction was confirmed only from the NuGet README text.
- Semantic Scholar API rate-limited (429) on every call; OpenAlex worked as a substitute for RFixer and Bartoli abstracts but had no abstract for Kushmerick 2000.
- Kushmerick primary PDFs: sciencedirect (403), ijcai.org/Proceedings/97-1/Papers/107.pdf (404), cs.ucd.ie (connection refused), aaai.org JAIR (404); details taken from the Chang et al. 2006 survey instead.
- RFixer paper PDF: dl.acm.org 403, pages.cs.wisc.edu guess 404; repo README and OpenAlex abstract used instead; no LICENSE file found in rongpan/RFixer.
- Bartoli/Trieste lab pages (machinelearning.inginf.units.it, regex.inginf.units.it) returned 404 or timed out; the number of highlighted examples RegexGenerator needs could not be confirmed from a primary source.
- GitHub topic pages 'programming-by-example' and 'regex-synthesis' contain no Python/JS FlashExtract or in-context regex synthesizer; no open-source FlashExtract reimplementation was found (the paper is the implementation spec).
- STALKER: no public code exists; ISI links redirect; only the JAAMAS paper PDF (usc-isi-i2.github.io) was retrievable.
- RoadRunner project page (dia.uniroma3.it) does not resolve; only the VLDB 2001 paper was retrievable, so current code availability/licence is unknown.

## Fill for gap 2: Battle-tested declarative 'extract from response, feed into next request' DSLs from ETL, API-testing, and RPA/low-code were not searched: Airbyte low-code declarative connectors (parent streams / SubstreamPartitionRouter, record selectors, paginators, error handlers), dlt transformers, Singer/Meltano, Hurl [Captures] with jsonpath/regex/xpath, Postman collections and Flows, Step CI, Karate, Tavern, k6, n8n/Windmill/Zapier expression languages, UiPath/Robocorp. Also GraphQL federation (Apollo @key entity resolution, Hasura remote joins, StepZen/Grafbase) and SPARQL 1.1 federated SERVICE as existing 'virtual graph over many APIs' engines.

_Researcher's framing: Survey of battle-tested declarative "extract-from-response, feed-into-next-request" DSLs (ETL connectors, API-test runners, workflow engines) and "virtual graph over APIs" engines, evaluated against seven criteria: regex capture from free text, array fan-out, conditional branching, time-window propagation, pagination, secrets, embeddable runtime. Method note: the session's WebSearch budget was already exhausted (0 of the requested 6+ searches could run), so every finding below comes from WebFetch of primary documentation/source at known URLs; items whose docs could not be fetched are flagged rather than described from memory.

RECOMMENDATION (two formats to adopt/borrow, one graph model to borrow):
1) ADOPT the Airbyte low-code manifest vocabulary as the authoring schema (SubstreamPartitionRouter/parent_key/partition_field/extra_fields for fan-out, DpathExtractor + RecordFilter + AddFields for selection, DatetimeBasedCursor step/lookback_window/stream_interval for time windows, DefaultPaginator, DefaultErrorHandler/HttpResponseFilter/WaitTimeFromHeader for 429s, Jinja interpolation with the built-in `regex_search`/`regex_replace` filters and `format_datetime`/`day_delta`/`duration` macros). It is the only surveyed DSL that has ALL SEVEN properties, is JSON-Schema-defined (declarative_component_schema.yaml), and runs in-process via `pip install airbyte-cdk` / PyAirbyte `get_source(source_manifest=dict|Path)`. Caveat: it is HTTP-only and batch-oriented; you would front MCP tools with a ~50-line local HTTP shim (or POST MCP streamable-HTTP JSON-RPC directly and add a CustomDecoder to parse `result.content[0].text`).
2) BORROW Hurl's capture grammar (`name: jsonpath "$.x" regex /(…)/ nth 0 toDate "%+" dateFormat "%Y-%m-%d"`) as the per-field extractor micro-language: query + chainable filters over free text, including a `jsonpath` FILTER that parses JSON embedded in a string (exactly the MCP `content[0].text` shape). Hurl is a Rust crate (`hurl::runner::run` with a VariableSet), but has no loops/branches, so use it as the extractor syntax, not the orchestrator. If you prefer a JS/Node/browser runtime, Step CI's YAML (captures: jsonpath/regex/xpath/header, `if:` Filtrex conditions, env/secrets, retries) via `@stepci/runner` is the closest ready-made interpreter, missing only fan-out.
3) If a Go binary is acceptable, Flowpipe (Turbot) already implements the orchestrator: HCL pipelines with `for_each`, `if`, `loop { until }`, `retry`, `error { ignore }`, `max_concurrency`, an `http` step that auto-decodes JSON, and HCL functions (`regex`, `regexall`, `jsondecode`, `timeadd` are standard HCL/cty functions; verify in Flowpipe's function reference). Hub mods exist for Slack/Jira/PagerDuty. No index server involved.
4) GRAPH MODEL: Apollo Connectors (`@connect(http:{GET:"/x/{$this.id}"}, selection:"…", entity:true, batch)`) is the most exact existing expression of "each MCP tool is a reference resolver keyed by @key fields"; steal its shape (entity type -> key fields -> tool + arg mapping -> selection) for your link-rule table even if you don't run the Rust router. Steampipe's KeyColumns/required-quals and osquery's `required=True` columns are the SQL-flavoured equivalent (free-text search as a required `query` column, e.g. Steampipe's slack_search). None of the graph engines do regex capture from free text; that stays in the extractor layer.

Concrete Jira->Slack->Logz chains are given in the `relevance` field of the Airbyte, Hurl, Step CI, Flowpipe and Apollo Connectors findings; all assume a local shim `POST http://127.0.0.1:8787/call/{server}/{tool}` returning the tool's parsed JSON._

### Airbyte low-code declarative connector: SubstreamPartitionRouter / ParentStreamConfig

- **Link:** <https://docs.airbyte.com/platform/connector-development/config-based/understanding-the-yaml-file/partition-router>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** Airbyte's manifest.yaml lets a child stream fan out over every record of a parent stream: `parent_stream_configs: [{stream: "#/repositories_stream", parent_key: id, partition_field: repository, request_option: {inject_into: request_parameter|body_json|header|path}}]`, then `{{ stream_slice.repository }}` is interpolated into the child's path/params/body. The CDK source (substream_partition_router.py) adds `extra_fields` (extra parent record paths carried into the slice), `incremental_dependency`, `lazy_read_pointer`, and skips parent records where `parent_key` is missing (KeyError -> slice skipped). ListPartitionRouter fans out over a static list; GroupingPartitionRouter batches slices.

**Why it matters here.** This is exactly 'forEach record in tool A's result, call tool B with a field of it'. Criteria: fan-out YES (nested parents allowed), pagination YES (DefaultPaginator: PageIncrement/OffsetIncrement/CursorPagination with page_token_option injection), time windows YES (DatetimeBasedCursor start/end/step/lookback_window exposed as stream_interval), retries/rate-limit YES (DefaultErrorHandler, WaitTimeFromHeader, HttpResponseFilter actions RETRY/IGNORE/RATE_LIMITED), secrets YES (config + secrets in spec), branching PARTIAL (RecordFilter Jinja `condition`, HttpResponseFilter predicates; no goto), regex YES via `regex_search` filter (see next finding). Example chain (compressed): stream jira_issue: requester path /jira/get_issue, request_body_json {key: "{{ config['ticket'] }}"}, record_selector extractor field_path [] with AddFields error_sig: "{{ record['fields']['description'] | regex_search('(?m)^(?:Error|Exception):\\s*(.+)$') }}", service: "{{ record['fields']['description'] | regex_search('service[=:]\\s*([a-z0-9-]+)') }}", day: "{{ format_datetime(record['fields']['created'], '%Y-%m-%d') }}". stream slack_hits: partition_router SubstreamPartitionRouter parent_stream_configs [{stream: '#/streams/0', parent_key: error_sig, partition_field: error_sig, extra_fields: [[day],[service]]}], request_body_json {query: "\"{{ stream_slice['error_sig'] }}\" in:#incidents after:{{ stream_slice.extra_fields['day'] }}", count: 100, page: "{{ next_page_token['next_page_token'] or 1 }}"}, paginator DefaultPaginator/PageIncrement page_size 100, record_selector extractor field_path [messages, matches, '*'], AddFields trace_id: "{{ record['text'] | regex_search('trace[_-]?id[=: ]+([0-9a-f]{16,32})') }}", record_filter condition "{{ record['trace_id'] }}". stream logz_lines: partition_router parent stream '#/streams/1' parent_key trace_id, extra_fields [[error_sig]]; incremental_sync DatetimeBasedCursor cursor_field '@timestamp', start_datetime "{{ config['window_start'] }}", end_datetime "{{ config['window_end'] }}", step PT4H, lookback_window PT1H; request_body_json {query: "\"{{ stream_slice.extra_fields['error_sig'] }}\" AND trace_id:{{ stream_slice['trace_id'] }}", from: "{{ stream_interval['start_time'] }}", to: "{{ stream_interval['end_time'] }}"}; extractor field_path [hits, hits, '*', _source]. Limitation: HttpRequester only, so MCP tools need an HTTP shim or a CustomDecoder for JSON-RPC `result.content[0].text`; the engine is sync-all rather than interactive, so per-ticket runs are driven by config values.

### Airbyte CDK interpolation filters and macros (regex_search, regex_replace, format_datetime, day_delta, duration)

- **Link:** <https://raw.githubusercontent.com/airbytehq/airbyte-python-cdk/main/airbyte_cdk/sources/declarative/interpolation/filters.py>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** The Jinja environment used in every interpolated manifest string ships custom filters: `regex_search(value, regex)` ("Match a regular expression against a string and return the first match group if it exists"), `regex_replace(value, regex, replacement)`, `hash`, `hmac`, `base64encode/decode`, `string`. macros.py adds `now_utc()`, `today_utc()`, `timestamp()`, `str_to_datetime()`, `day_delta(num_days, format)`, `duration(iso8601)`, `format_datetime(dt, format, input_format)`, `sanitize_url()`, `camel_case_to_snake_case()`, `generate_uuid()`, `max/min`. Interpolation context exposes config, record, response, headers, stream_slice, stream_partition, stream_interval, stream_state, next_page_token, last_record, last_page_size.

**Why it matters here.** Answers the key question 'can Airbyte pull an ID out of a Jira description or a Slack message?': yes, one regex group per filter call, composable with AddFields into a derived field that a downstream SubstreamPartitionRouter can key on. Time-window arithmetic (`day_delta`, `duration`, `format_datetime`, `stream_interval`) is built in. Source URL for macros: https://raw.githubusercontent.com/airbytehq/airbyte-python-cdk/main/airbyte_cdk/sources/declarative/interpolation/macros.py

### Hurl captures and filter chains (jsonpath/regex/xpath + chained filters, Rust library)

- **Link:** <https://hurl.dev/docs/capturing-response.html>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** Hurl entries are `REQUEST / HTTP <code> / [Captures] name: <query> <filter>*`. Queries: status, header, cookie, body, bytes, xpath, jsonpath, regex (capture group required), variable, url, redirects, duration, certificate, sha256/md5. Filters chain: regex, replaceRegex, split, nth, first, last, count, jsonpath (applies JSONPath to a STRING value), xpath, toInt/toFloat/toDate/dateFormat/daysAfterNow, urlEncode/Decode, base64*, htmlUnescape. Example from docs: `name: jsonpath "$.user.id" replaceRegex /\d/ "x"` and `jsonpath "$.name" split "," nth 0`. Captured vars are reused as `{{name}}`; `--variable`, `--variables-file`, `--secret` (redacted in logs), `--retry/--retry-interval`, `--repeat`, `--parallel/--jobs`, `--json` (HAR-like output). Docs explicitly: no loops, no conditionals. Library: `hurl::runner::run(content, filename, &RunnerOptions, &VariableSet, &LoggerOptions) -> HurlResult` (docs.rs/hurl).

**Why it matters here.** Best-in-class 'extract a substring of interest from a long unstructured body' syntax; the `jsonpath` filter on strings means it can unwrap MCP JSON-RPC `result.content[0].text` directly with no shim. Criteria: regex YES, secrets YES (redaction), retry YES, pagination NO, fan-out NO, branching NO, embeddable YES (Rust crate; also a CLI with JSON output). Example chain: `POST http://127.0.0.1:8787/call/jira/get_issue\nAuthorization: Bearer {{shim}}\n{"key":"{{ticket}}"}\nHTTP 200\n[Captures]\nerror_sig: jsonpath "$.fields.description" regex /(?m)^(?:Error|Exception):\s*(.+)$/\nservice: jsonpath "$.fields.description" regex /service[=:]\s*([a-z0-9-]+)/\nday: jsonpath "$.fields.created" toDate "%+" dateFormat "%Y-%m-%d"\n\nPOST http://127.0.0.1:8787/call/slack/search_messages\n{"query":"\"{{error_sig}}\" in:#incidents after:{{day}}","count":100}\nHTTP 200\n[Captures]\ntrace_id: jsonpath "$.messages.matches[0].text" regex /trace[_-]?id[=: ]+([0-9a-f]{16,32})/\n[Asserts]\njsonpath "$.messages.matches" count > 0\n\nPOST http://127.0.0.1:8787/call/logz/search\n{"query":"\"{{error_sig}}\" AND service:{{service}} AND trace_id:{{trace_id}}","from":"{{day}}T00:00:00Z","size":500}\nHTTP 200\n[Captures]\nlines: jsonpath "$.hits.hits[*]._source.message"`. Note `matches[0]` — Hurl cannot iterate; your interpreter would re-run the tail entry per match (e.g. `--variables-file` per item). Filters doc: https://hurl.dev/docs/filters.html ; CLI: https://hurl.dev/docs/manual.html ; lib: https://docs.rs/hurl/latest/hurl/runner/fn.run.html

### Flowpipe (Turbot): HCL pipelines with for_each / if / loop / retry and an http step

- **Link:** <https://flowpipe.io/docs/flowpipe-hcl/step/index>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** Single Go binary, pipelines-as-code in HCL, mods on hub.flowpipe.io (Slack, Jira, PagerDuty, etc.). Every step supports `for_each` ("A step instance will be created for each item in the map or list", `each.value`), `if`, `depends_on` (auto-inferred from references), `retry { max_attempts strategy=exponential|linear|constant min_interval max_interval }`, `loop { until = ... }` with per-iteration arg overrides via `result`, `throw`, `error { ignore = true }`, `timeout`, `max_concurrency`. The `http` step returns `status_code`, `response_headers`, `response_body` (auto-decoded when Content-Type is application/json) and later steps reference `step.http.whos_in_space.response_body.people[*].name`. Docs example: `step "http" "add_a_user" { for_each = ["Jerry","Elaine","Newman"] url = ... request_body = jsonencode({ user_name = "${each.value}" }) }`.

**Why it matters here.** The only surveyed OSS tool with fan-out + conditionals + loops + retries + a JSON-aware HTTP step in one declarative file, runnable locally (`flowpipe pipeline run`) with no index/search server. Regex comes from HCL's standard `regex()`/`regexall()` functions and time windows from `timeadd()`/`timestamp()` (standard HCL/cty functions; confirm in Flowpipe's function reference, which was not fetched). Not embeddable as a library in Python/JS (Go process), and credentials live in Flowpipe `connection` blocks. Example: `pipeline "investigate" {\n  param "ticket" { type = string }\n  param "shim" { type = string default = "http://127.0.0.1:8787/call" }\n  step "http" "jira" { url = "${param.shim}/jira/get_issue" request_body = jsonencode({ key = param.ticket }) retry { max_attempts = 3 strategy = "exponential" } }\n  step "transform" "sig" { value = {\n    error_sig = try(regex("(?m)^(?:Error|Exception):\\s*(.+)$", step.http.jira.response_body.fields.description)[0], "")\n    service   = try(regex("service[=:]\\s*([a-z0-9-]+)", step.http.jira.response_body.fields.description)[0], "")\n    from = timeadd(step.http.jira.response_body.fields.created, "-1h")\n    to   = timeadd(step.http.jira.response_body.fields.created, "4h") } }\n  step "http" "slack" { if = step.transform.sig.value.error_sig != "" url = "${param.shim}/slack/search_messages" request_body = jsonencode({ query = "\"${step.transform.sig.value.error_sig}\" in:#incidents", count = 100 }) }\n  step "transform" "trace_ids" { value = distinct(flatten([for m in step.http.slack.response_body.messages.matches : regexall("trace[_-]?id[=: ]+([0-9a-f]{16,32})", m.text)])) }\n  step "http" "logz" { for_each = step.transform.trace_ids.value max_concurrency = 4 url = "${param.shim}/logz/search" request_body = jsonencode({ query = "\"${step.transform.sig.value.error_sig}\" AND service:${step.transform.sig.value.service} AND trace_id:${each.value}", from = step.transform.sig.value.from, to = step.transform.sig.value.to, size = 500 }) error { ignore = true } }\n  output "lines" { value = { for k, r in step.http.logz : k => r.response_body.hits.hits[*]._source.message } }\n}`. http step: https://flowpipe.io/docs/flowpipe-hcl/step/http

### Airbyte low-code CDK overview + PyAirbyte in-process manifest execution

- **Link:** <https://airbytehq.github.io/PyAirbyte/airbyte.html>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** `ManifestDeclarativeSource` (airbyte-cdk, `pip install airbyte-cdk`) interprets manifest.yaml; the component schema is a JSON Schema (declarative_component_schema.yaml) that documents DpathExtractor, ResponseToFileExtractor, CustomRecordExtractor/CustomRecordFilter/CustomPartitionRouter (Python class_name hooks), AsyncRetriever (submit job / poll / fetch), and DynamicDeclarativeStream. PyAirbyte's `get_source(name, config, source_manifest=True|dict|Path|str)` runs a manifest in-process and `get_records()` streams records without a cache (or `read()` into local DuckDB).

**Why it matters here.** Confirms the embeddable-runtime criterion: Python in-process, local DuckDB optional, no server. Custom Python components give an escape hatch for the fuzzy bits (a CustomRecordExtractor can parse MCP `content[0].text`). Overview URL: https://docs.airbyte.com/platform/connector-development/config-based/low-code-cdk-overview ; schema: https://raw.githubusercontent.com/airbytehq/airbyte-python-cdk/main/airbyte_cdk/sources/declarative/declarative_component_schema.yaml

### Step CI workflow YAML + @stepci/runner (Node library)

- **Link:** <https://docs.stepci.com/reference/workflow-syntax.html>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** YAML tests with sequential HTTP/GraphQL/gRPC/SSE steps. `captures:` per step support `jsonpath`, `xpath`, `regex` (over raw body, e.g. `regex: <title>(.*?)<\/title>`), `header`, `selector`, `cookie`, `body`; reuse as `${{captures.x}}`; `env` and `components.credentials` for secrets; per-step `if: captures.title == "Example Domain"` (Filtrex); retries with count/interval; CSV test data; JSON Schema/status/jsonpath checks; plugins via npm. `@stepci/runner` exposes `run(workflow, { ee })`, `runFromFile()`, `runFromYAML()` and an EventEmitter with step/test/done events.

**Why it matters here.** Closest ready-made JS interpreter for the developer's 'YAML + ~200 line interpreter' plan: regex YES, branching YES (`if`), secrets YES, retry YES, embeddable YES (Node, browser-adjacent), pagination NO, fan-out NO (no forEach; you'd wrap the runner and call it per item), time windows only via env/captures string interpolation. Example: `version: "1.1"\nname: jira-slack-logz\nenv: { shim: http://127.0.0.1:8787/call, ticket: OPS-4412 }\ntests:\n  investigate:\n    steps:\n      - name: jira\n        http:\n          url: ${{env.shim}}/jira/get_issue\n          method: POST\n          json: { key: "${{env.ticket}}" }\n          captures:\n            error_sig: { regex: "(?:Error|Exception):\\s*([^\\n\"]+)" }\n            created:   { jsonpath: $.fields.created }\n          check: { status: /^20/ }\n      - name: slack\n        if: captures.error_sig != ""\n        http:\n          url: ${{env.shim}}/slack/search_messages\n          method: POST\n          json: { query: "\"${{captures.error_sig}}\" in:#incidents", count: 100 }\n          captures:\n            trace_id: { regex: "trace[_-]?id[=: ]+([0-9a-f]{16,32})" }\n      - name: logz\n        if: captures.trace_id != ""\n        http:\n          url: ${{env.shim}}/logz/search\n          method: POST\n          json: { query: "\"${{captures.error_sig}}\" AND trace_id:${{captures.trace_id}}", from: "${{captures.created}}", size: 500 }\n          captures: { lines: { jsonpath: "$.hits.hits[*]._source.message" } }`. Runner README: https://raw.githubusercontent.com/stepci/runner/main/README.md

### Apollo Connectors: @source/@connect with $this, entity: true, batch, and the JSONSelection mapping language

- **Link:** <https://www.apollographql.com/docs/graphos/schema-design/connectors/entities>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** Declarative REST-to-GraphQL in schema SDL: `@source(name, http:{baseURL, headers})` and `@connect(source, http:{GET:"/products/{$args.id}"}, selection:"id name", entity: true, batch, errors)`. `$this.id` references parent-object fields so a type-level connector acts as a reference resolver (`type Price @key(fields:"id") @connect(http:{GET:"/products/{$this.id}/price"} ...)`); `entity: true` marks a Query field as the @key entity resolver; `batch` and `$batch` group N representations into one request. The selection language does aliasing, nested selection (`$args { user { username: handle } }`), path expressions over arrays, and `$` = response root; the fetched pages do not show any regex facility.

**Why it matters here.** The most exact existing embodiment of 'MCP servers as a graph database': one declarative entry per (entity type, key fields, tool, arg mapping, output selection), and the router's query planner decides which fetches to chain (Sequence/Parallel/Flatten, `_entities(representations:)`, see query-plan finding). Criteria: fan-out YES (list fields and batching are handled by the planner), pagination NO, time windows via `$args`, branching NO, regex NO (documented mapping language has none), secrets via `$config`, runtime = Apollo Router (Rust binary; Connectors need GraphOS composition via rover — check licence). Borrow the schema shape even if not running the router. Example: `extend schema @source(name:"shim", http:{baseURL:"http://127.0.0.1:8787/call", headers:[{name:"Authorization", value:"Bearer {$config.token}"}]})\ntype Issue @key(fields:"key") @connect(source:"shim", http:{POST:"/jira/get_issue", body:"key: $this.key"}, selection:"key summary: fields.summary description: fields.description created: fields.created") {\n  key: ID! summary: String description: String created: String\n  slackHits(query: String!): [SlackMessage] @connect(source:"shim", http:{POST:"/slack/search_messages", body:"query: $args.query count: 100"}, selection:"$.messages.matches { ts text channel: channel.name }")\n}\ntype SlackMessage { ts: ID! text: String channel: String\n  logs(query: String!, from: String!, to: String!): [LogLine] @connect(source:"shim", http:{POST:"/logz/search", body:"query: $args.query from: $args.from to: $args.to size: 500"}, selection:"$.hits.hits._source { message }") }\ntype LogLine { message: String }\ntype Query { issue(key: ID!): Issue @connect(source:"shim", http:{POST:"/jira/get_issue", body:"key: $args.key"}, selection:"key summary: fields.summary", entity: true) }` — note the search strings must be passed as `$args` by the caller or pre-extracted by the shim, because the mapping language cannot regex them out of `description`. Directives reference: https://www.apollographql.com/docs/graphos/schema-design/connectors/directives ; mapping: https://www.apollographql.com/docs/graphos/schema-design/connectors/mapping

### Apollo Federation query plans: Fetch/Sequence/Parallel/Flatten and _entities(representations)

- **Link:** <https://www.apollographql.com/docs/graphos/reference/federation/query-plans>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** The router compiles a client query into a plan of Fetch nodes (one subgraph op each), Sequence/Parallel, and Flatten(path: "hotels.@") nodes that merge entity fetches back into list positions. Entity fetches carry two fragments separated by `=>`: the representation (`__typename` + @key fields) and the fields to resolve, sent as `_entities(representations: [...])`. Verbatim example: `QueryPlan { Sequence { Fetch(service:"hotels") { { hotels { id address __typename } } }, Flatten(path:"hotels.@") { Fetch(service:"reviews") { { ... on Hotel { __typename id } } => { ... on Hotel { reviews { rating } } } } } } }`. Subgraphs implement `__resolveReference(representation)` (entities intro page).

**Why it matters here.** A concrete, proven algorithm for the 'programmatic flexibility' the developer wants: given a declared graph of (type, key, resolver-tool) triples, the planner picks tool order, batches representations, and re-attaches results at list paths — i.e. exactly the 'which tool next / fan-out / merge' decision, done without an LLM. A ~200-line interpreter can implement a simplified version: walk the requested selection, for each unresolved entity field find the tool whose key it can satisfy. Entities intro: https://www.apollographql.com/docs/graphos/schema-design/federated-schemas/entities/intro

### dlt rest_api declarative source and @dlt.transformer

- **Link:** <https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Python-dict config: resources with `endpoint: {path, params, paginator (json_link|header_link|offset|page_number|cursor|auto), data_selector (JSONPath), response_actions (status/content -> ignore/raise), incremental {start_param, end_param, cursor_path, initial_value, convert}}` and parent-child via `params: {issue_number: {type: "resolve", resource: "issues", field: "number"}}` with `include_from_parent`. `processing_steps` allow filter/map/yield_map Python callables. Imperatively, `@dlt.transformer(data_from=users)` receives each parent item and yields child rows; pipe syntax `users(limit=100) | users_details`; async transformers supported.

**Why it matters here.** Second-best ETL schema after Airbyte: `resolve` = fan-out, paginators, incremental windows, response_actions = error handling, secrets.toml. Regex is NOT declarative (needs a `map` callable), and it's Python-only, in-process, no server. Good fallback if the team prefers Python dicts over YAML. Resource/transformer docs: https://dlthub.com/docs/general-usage/resource

### Windmill OpenFlow spec (forloopflow, branchone/branchall, whileloopflow, input_transforms)

- **Link:** <https://www.windmill.dev/docs/openflow>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** OpenFlow is Windmill's open JSON/YAML flow format: modules of kind rawscript/script/flow/identity/forloopflow (iterator = JS expression, optional parallel)/whileloopflow (stop_after_if)/branchone (first true predicate)/branchall (parallel branches)/aiagent; `input_transforms` map each input to a static value or a JavaScript expression over `results.<step_id>` and `flow_input`; per-module retry (constant/exponential), sleep, cache TTL, stop_after_if, suspend/approval, mock, continue-on-error.

**Why it matters here.** Cleanest published JSON schema for a DAG with fan-out + branching + retries; the extraction step would be a rawscript (JS/Python) so regex is free-form code, not declarative. Executing it needs the Windmill server+worker (self-hosted Postgres) — heavier than the constraint allows, so borrow the schema (forloopflow/branchone/input_transforms) rather than the runtime. Branches doc: https://www.windmill.dev/docs/flows/flow_branches

### JSONata regex and predicate support (basis for extractor expressions)

- **Link:** <https://docs.jsonata.org/regex>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** JSONata (JS, with Java/Python/Rust ports) has regex literals `/pattern/flags`, `$match`, `$contains`, `$split`, `$replace`, predicate filtering `Account.Order.Product['Product Name' ~> /hat/i]`, and a matcher object `{match, start, end, groups, next()}` for iterating all matches. Postman FQL is JSONata-derived (Postman docs could not be fetched to confirm).

**Why it matters here.** If the team wants one expression language instead of JSONPath + separate regex filters, JSONata gives selection, filtering, array mapping and regex capture in a single string, runnable in-browser. Good candidate for the `extract:` field values in their YAML.

### Steampipe plugin SDK: KeyColumns / required quals, per-row hydrate, retry & rate limiting; slack_search table

- **Link:** <https://steampipe.io/docs/develop/writing-plugins>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Postgres FDW + gRPC plugins expose APIs as tables. `KeyColumns: [{Name:"repository_full_name", Require: plugin.Required}, {Name:"updated_at", Require: plugin.Optional, Operators: [">",">="]}]` push WHERE predicates into API parameters; JOINs cause Get/List hydrate calls per matched row; `HydrateConfig{Depends}` orders dependent calls; `DefaultRetryConfig{ShouldRetryErrorFunc(429)}`, `IgnoreConfig(404)`, tag-scoped client-side rate limiters, `Memoize` caching. The Slack plugin's `slack_search` table takes a required `query` column in Slack search syntax; plugins are also 'available as a standalone Exporter CLI'.

**Why it matters here.** SQL-over-APIs is the other established 'virtual graph' model: joins = nested-loop tool calls keyed by required columns, and free-text search is modelled as a required qual (exactly the 'search string param' case). Time windows map to Optional operators on timestamp columns. No regex capture from bodies (you'd use Postgres regexp functions after the fact). Runtime is a local Postgres FDW process — not an index server, but not embeddable in a browser/Python process either. osquery's table spec (`required=True`, `context.constraints["path"].getAll(EQUALS)`) is the same pattern in-process: https://osquery.readthedocs.io/en/stable/development/creating-tables/ ; slack_search: https://hub.steampipe.io/plugins/turbot/slack/tables/slack_search

### GraphQL Mesh: OpenAPI handler + additionalTypeDefs/additionalResolvers (JS, embeddable)

- **Link:** <https://the-guild.dev/graphql/mesh/docs/handlers/openapi>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Turns OpenAPI/JSON-Schema-described REST endpoints into GraphQL types/operations (`selectQueryOrMutationField`, `operationHeaders: {Authorization: '{context.headers.authorization}'}`, `queryParams`), and stitches sources together with `additionalTypeDefs: | extend type Store { bookSells: [Sells!]! }` plus `additionalResolvers: ['./resolvers']` whose SDK calls accept `args`, `selectionSet`, `valuesFromResults`, and `dataLoaderOptions` for batching.

**Why it matters here.** The open-source, Node-embeddable analogue of Apollo Connectors/Hasura joins: cross-source links are declared as type extensions with resolvers that map parent fields to another source's arguments (with DataLoader batching). Regex/extraction happens in the resolver JS. Runs in-process in Node (no server needed beyond your own web backend). Extending guide: https://the-guild.dev/graphql/mesh/docs/guides/extending-unified-schema

### Arazzo Specification 1.1 (OpenAPI Initiative workflows)

- **Link:** <https://spec.openapis.org/arazzo/latest.html>
- **Kind:** spec
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Workflow -> Steps referencing operationId/operationPath/workflowId; `parameters` (in path/query/header/cookie) and `requestBody`; `successCriteria` with condition types simple (`$statusCode == 200`), regex (with `context: $response.body`), jsonpath (RFC 9535), xpath; `onSuccess/onFailure` actions `goto`/`retry (retryAfter, retryLimit)`/`end` gated by criteria; `outputs` as runtime expressions (`$response.body#/fields/summary`, `$steps.jira.outputs.sig`, `$inputs.ticket`). Spec explicitly has no loop/forEach construct; regex is only a boolean criterion, not a capture. Runners per the README: arazzo-cli, Arazzo Runner (Jentic), Redocly Respect CLI (Arazzo tests over live APIs, 'No support for XPath'), Specmatic; parsers in TS/JS and Itarazzo.

**Why it matters here.** Confirms the report's premise: Arazzo alone lacks capture-by-regex and fan-out, so 'Arazzo-plus-extract-plus-forEach' is a real gap. Worth borrowing only its naming (`$steps.<id>.outputs.<name>`, `$inputs`, `successCriteria`, `onFailure: retry`) so files stay convertible to Arazzo tooling. README/tooling list: https://raw.githubusercontent.com/OAI/Arazzo-Specification/main/README.md ; Respect: https://redocly.com/docs/respect

### Tavern (pytest YAML API tests): save blocks, JMESPath, $ext functions, validate_regex

- **Link:** <https://tavern.readthedocs.io/en/latest/basics/>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Stages of request/response; `response.save.json: {returned_id: id}` uses JMESPath (`thing.nested[0]`); headers and redirect query params also savable; values reused with Python format strings `{returned_id}`, magic vars `{tavern.env_vars.SECRET_TOKEN}`; `$ext` external Python functions for custom extraction; `tavern.helpers.validate_regex` matches a regex and exposes named groups as `{regex.name}` in later stages; `max_retries`, pytest marks/parametrize, `is_defaults`.

**Why it matters here.** Regex capture YES (named groups), secrets via env, retries YES, branching only via pytest skip marks, fan-out only via parametrize (static), pagination NO, runtime Python/pytest (heavy for a web backend). JMESPath (also used by n8n `$jmespath` and AWS CLI) is a reasonable alternative to JSONPath for the extractor layer.

### n8n expressions and string transformation functions

- **Link:** <https://docs.n8n.io/build/work-with-data/transform-data/expressions-for-data-transformation.md>
- **Kind:** product
- **Relevance:** `█████░░░░░` 5/10

**What it is.** `{{ }}` expressions with `$json`, `$('Node').item.json`, `$input`, `$jmespath`, JS/IIFE allowed, Luxon dates; built-in string helpers `extractEmail()`, `extractUrl()`, `extractDomain()`, `extractUrlPath()`, `hash()`, `parseJson()`, `removeMarkdown()`, `replaceSpecialChars()`, `toDateTime()` (regex via plain JS `.match`). Nodes: Loop Over Items (splitinbatches), Split Out, If/Switch. Workflows export/import as JSON.

**Why it matters here.** Illustrates useful 'fuzzy substring' helpers (extract first email/URL/domain from a blob) worth copying into the extractor layer. But n8n is a server product with a JSON format tied to its node runtime, so not adoptable as the interpreter under the no-extra-servers constraint. String reference: https://docs.n8n.io/build/work-with-data/transform-data/expression-reference/string.md

### Meltano Singer SDK parent-child streams (parent_stream_type / get_child_context)

- **Link:** <https://sdk.meltano.com/en/latest/parent_streams.html>
- **Kind:** oss-project
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Child stream sets `parent_stream_type = EpicsStream` and implements `get_child_context(record, context) -> {"group_id": record["group_id"], "epic_iid": record["iid"]}` (or `generate_child_contexts` for one-to-many); context keys fill `path = "/groups/{group_id}/epics/{epic_iid}/issues"`; `state_partitioning_keys` controls per-parent bookmarks; `ignore_parent_replication_key`. The core Singer spec (SCHEMA/RECORD/STATE messages, catalog) has no declarative parent/child notion; tap-rest-api-msdk is config-driven (JSONPath `records_path`, pagination styles, `source_search_field/query`) but documents no parent/child support.

**Why it matters here.** Same fan-out idea as Airbyte but code-first (Python classes), so it doesn't give an authoring file format; useful only as a reference design for state partitioning per parent key. Singer spec: https://github.com/singer-io/getting-started/blob/master/docs/SPEC.md ; tap-rest-api-msdk: https://raw.githubusercontent.com/Widen/tap-rest-api-msdk/main/README.md

### Karate DSL (JVM) — call/callonce, JsonPath, retry until, #regex

- **Link:** <https://raw.githubusercontent.com/karatelabs/karate/v1.4.1/README.md>
- **Kind:** oss-project
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Gherkin-flavoured DSL on the JVM (Java 11+, standalone JAR, GraalJS embedded): `def id = response.path('$.id')` or `response.id`; embedded expressions `#(var)` in JSON; `call read('x.feature') data` loops a feature over a JSON array (data-driven fan-out); `retry until response.status == 'done'`; `match response == '#regex ...'`; arbitrary JS for conditionals; `configure retry`.

**Why it matters here.** Functionally covers regex, fan-out, branching, retry and secrets, but authoring is a Gherkin+JS hybrid rather than a data schema and the runtime is JVM-only — a poor fit for a browser/Node or Python web frontend with tight budgets. Reference only.

### Hasura remote relationships / remote joins

- **Link:** <https://hasura.io/docs/2.0/remote-schemas/remote-relationships/index/>
- **Kind:** product
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Three directions (DB->remote schema, remote schema->DB, remote schema->remote schema); a table column (or static value) is mapped to a remote field argument (e.g. `user_id` -> `Order(id:)`); Hasura 'batches calls to Remote Schemas to avoid the n+1 problem' and builds an execution plan that pushes work to sources.

**Why it matters here.** Same key->argument join model as Apollo entities but requires the Hasura server and GraphQL on both sides; no regex, pagination or time-window semantics beyond arguments. Reference for batching behaviour only.

### SPARQL 1.1 Federated Query SERVICE + FedX bound joins + Comunica (browser)

- **Link:** <https://www.w3.org/TR/sparql11-federated-query/>
- **Kind:** spec
- **Relevance:** `████░░░░░░` 4/10

**What it is.** `SERVICE <endpoint> { ?person foaf:name ?name }` routes a sub-pattern to a remote endpoint; `SERVICE SILENT` tolerates failure; the spec notes implementers may inject already-bound variables via `VALUES` ('bound join'). FedX (RDF4J, Java) implements this with `boundJoinBlockSize` (default 25) and `enableServiceAsBoundJoin`; Comunica (`@comunica/query-sparql`) runs federated SPARQL in Node and the browser against multiple heterogeneous sources.

**Why it matters here.** Conceptually the purest 'query language over a federation of APIs with variable bindings flowing between calls', and Comunica proves it can run in-browser without a server. But every MCP tool would have to be wrapped as a triple-pattern source, and SPARQL has no first-class regex capture into new bindings (only REGEX/REPLACE filters), so adoption cost is high. FedX: https://rdf4j.org/documentation/programming/federation/ ; Comunica browser: https://comunica.dev/docs/query/getting_started/query_browser_app/

### Transposit (defunct) — SQL joins over SaaS APIs for on-call

- **Link:** <https://www.transposit.com/>
- **Kind:** product
- **Relevance:** `███░░░░░░░` 3/10

**What it is.** Transposit's site now carries a shutdown notice ('made the difficult decision to shut our doors') and says Harness is licensing its product and technology. The original SQL-over-APIs docs are no longer served (docs path 404, web.archive.org unreachable from this environment), and the GitHub org only has SDK/infra repos (transposit-js-sdk archived).

**Why it matters here.** Confirms the exact product idea (JOIN across Jira/Slack/PagerDuty operations for on-call) existed and was commercially retired; no reusable code or spec survives publicly, so it's a design precedent only.

### k6 (Grafana) load-testing scripts

- **Link:** <https://grafana.com/docs/k6/latest/using-k6/scenarios/>
- **Kind:** oss-project
- **Relevance:** `██░░░░░░░░` 2/10

**What it is.** JavaScript scripts executed by a Go runtime; chaining via `res.json('path')`, JS regex, `http.batch` for parallel requests, `check`, `SharedArray`; extensible via xk6 Go extensions.

**Why it matters here.** Not declarative (plain JS) and purpose-built for load generation; only relevant as evidence that a 'JS-in-Go' runtime is another embeddable option. Not recommended.

### Dead ends

- WebSearch: the session's search budget was already exhausted (200/200) before this task started, so none of the requested 6+ search queries could run; all findings come from WebFetch of primary docs at known URLs.
- StepZen @materializer/@rest/@sequence: stepzen.com docs paths (custom-graphql-directives/directives, directives-reference, features/linking-data, connecting-backends/linking-data) returned 404 or ECONNRESET, and the IBM API Connect Essentials mirror returned only a navigation index — not evaluated from primary sources.
- Postman Flows / FQL: every documentation path tried (postman-flows/overview, build-flows/blocks, reference/blocks-list, flows-query-language/introduction-to-fql, fql-overview, fql-reference, fql-function-reference) was a 404 or an overview without block/FQL detail; only confirmed FQL exists and flows deploy to Postman Cloud (proprietary runtime).
- Transposit SQL docs: www.transposit.com/docs/references/sql/ is gone, web.archive.org is blocked from this environment; only the shutdown/Harness-licensing notice was retrievable.
- Robocorp/RPA Framework HTTP docs: robocorp.com redirects to sema4.ai which returned HTTP 403; UiPath not fetched. RPA record-and-replay is UI-level and unlikely to fit regardless.
- Karate: the current GitHub README is a stub pointing to docs.karatelabs.io; details taken from the v1.4.1 tagged README instead.
- Arazzo IMPLEMENTATIONS.md returned 404 (tooling list taken from the repo README); jentic/arazzo-runner GitHub URL 404.
- Flowpipe function reference (regex/regexall/timeadd availability) was not fetched — the HCL example assumes standard HCL/cty functions; verify before relying on it.
- n8n legacy doc URLs (/code/expressions/, /code/cookbook/expressions/) are 404; located current pages via docs.n8n.io/sitemap.md.
- Apollo Connectors batching page (schema-design/connectors/batching) 404; batching semantics summarized from the directives and entities pages only.

## Fill for gap 3: The report never examines the fuzziness already built into the target backends' own query languages and what the specific MCP tools accept: Slack search modifiers (in:, from:, after:, before:, has:, quoted phrases), Confluence CQL (text ~ fuzzy, title ~, ancestor, lastModified), JQL (text ~, summary ~, wildcards, ORDER BY), Elasticsearch/Logz query_string, match_phrase_prefix, fuzziness, wildcard on keyword fields, PromQL label regex matchers (=~), Google Drive API q (fullText contains, name contains), git log -S/-G pickaxe and git grep. It also does not cover pagination limits, result caps, rate limits, or time-window parameters of each MCP tool.

_Researcher's framing: Backend-native fuzziness and MCP parameter schemas, gathered from primary docs/source (WebSearch quota was already exhausted in this session, so everything below came from direct WebFetch of vendor docs, GitHub source, and API references). Cross-backend synthesis for the P4 query templates: (1) Every backend accepts a "precision ladder" that can be generated without an LLM: exact phrase -> AND of tokens -> OR of tokens -> prefix/wildcard -> edit-distance fuzzy, but only Elasticsearch/Logz.io (query_string ~N / fuzziness:AUTO, on non-Logz ES) and Confluence CQL (Lucene `~` on text/title) offer true edit-distance fuzziness; JQL silently IGNORES fuzzy/proximity/boost and only supports trailing `*` plus stemming; Slack supports quotes, `-` exclusion, `in:/from:/after:/before:/on:/during:/is:thread/has:` and a trailing `*` wildcard (min 3 chars) but no fuzziness; Drive `fullText contains` matches whole tokens only (no prefix) while `name contains` is prefix-of-word only; PromQL/LogQL/Chronosphere/Cloud Logging give anchored (PromQL, Loki stream selectors) or unanchored (Loki line filter |~, Chronosphere =~, Cloud Logging =~) RE2 regex, so "fuzziness" must be expressed as alternation regex `(?i)(id1|id2|id3)` built from extracted IDs. (2) Time windows: Slack has only day granularity (after:/before:/on:), Logz.io search defaults to today+yesterday (use dayOffset or an explicit @timestamp range filter; scroll limited to 2 consecutive daily indexes), Loki/Prometheus/Chronosphere/Grafana MCP take RFC3339 or `now-1h`, PagerDuty since/until ISO, Drive modifiedTime RFC3339, JQL/CQL relative `-7d` / now("-4w"), git --since/--until free-form dates. (3) Match-span localisation for the next hop: Elasticsearch/Logz.io `highlight` (default <em></em>, 100-char fragments, 5 per field) and Slack search.messages `highlight=true` (U+E000/U+E001 markers, but korotovsky's MCP hard-codes highlight=false) and Confluence search `excerpt=highlight` are the only backends that return spans; git grep -n --column and grep-style tools return line/column; everything else needs a local regex re-scan of the returned text. (4) Caps/rates that constrain fan-out: Slack search 100/page, 100 pages, Tier 2 (~20/min); conversations.history 999/page (15 and 1 req/min for new non-Marketplace apps); Jira burst 100 GET/s and hourly point quota, Rovo MCP 1-10 credits per call with 25-150 credits/user/month on Jira/Confluence plans; Confluence search limit capped at 25 with body views; Logz.io 10,000 hits/query (1,000 with aggs), scroll 1,000/page, 100 concurrent requests/account; Grafana MCP Loki limit 100 default; Chronosphere Prometheus tools default limit 100, step 60s; PagerDuty list limit max 100/page (MCP model allows 1-1000, warns at 1000); Drive pageSize max 1000, 325k quota-units/user/min._

### Elasticsearch query_string query: syntax and parameters

- **Link:** <https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-query-string-query>
- **Kind:** spec
- **Relevance:** `██████████` 10/10

**What it is.** Syntax: field:value, "phrase", wildcards ? and * (leading wildcards costly; allow_leading_wildcard), regex /.../, fuzzy `term~` (max 2 edits) or `term~1`, proximity "a b"~5, ranges [a TO b], boost ^, boolean + - AND OR NOT, grouping. Reserved chars to escape: + - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \ /. Params: default_field (default *), fields, default_operator (OR), fuzziness, fuzzy_max_expansions (50), analyze_wildcard, minimum_should_match, lenient, time_zone. Throws on invalid syntax (unlike simple_query_string).

**Why it matters here.** The richest single fuzzy grammar available: one query_string can encode the entire precision ladder as `"exact phrase"^4 OR (tok1 AND tok2)^2 OR tok1~1 OR tok2~1 OR prefix*` with minimum_should_match, so the backend ranks exact over fuzzy. Extracted IDs must be escaped per the reserved list (hyphens/colons in trace IDs, URLs).

### Logz.io Search API (POST /v1/search): DSL restrictions, size caps, default time range

- **Link:** <https://api-docs.logz.io/docs/logz/search/>
- **Kind:** spec
- **Relevance:** `██████████` 10/10

**What it is.** Body accepts Elasticsearch Search DSL: query (required), from, size (max 10,000, default 10), sort (not on analyzed fields like message), _source.includes, post_filter, docvalue_fields, stored_fields, highlight, aggregations (size <= 1,000, no multi-level bucket nesting). Limits: 10,000 hits per non-aggregated query, 1,000 for aggregated; searches default to the last 2 calendar days (UTC) unless dayOffset or a timestamp filter is given; NO fuzzy expansions and NO leading wildcards allowed. Auth via X-API-TOKEN; 100 concurrent API requests per account (https://api-docs.logz.io/docs/logz/logz-io-api/).

**Why it matters here.** Critical design constraint: on Logz.io you cannot use `term~` / fuzziness or `*id`; the fuzz ladder collapses to phrase -> AND tokens -> OR tokens -> trailing wildcard on keyword fields, with regexp on short fields if permitted. `highlight` IS supported, so span localisation works. Always send an explicit @timestamp range from the previous hop's window because the 2-day default will silently miss older incidents.

### Slack Help: Search in Slack (modifier syntax)

- **Link:** <https://slack.com/help/articles/202528808-Search-in-Slack>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** Authoritative list of Slack search modifiers: "exact phrase" quoting, -term / -in: / -from: exclusion, in:#channel|@person, from:@name, with:@name (DMs/threads), has::emoji:, has:pin, is:saved, is:thread, before:/after:/on:DATE, during:month|year, creator:@name, and a trailing wildcard `rep*` requiring at least 3 characters. No edit-distance fuzziness exists.

**Why it matters here.** Defines the exact grammar a deterministic Slack query generator may emit. Recipe: base = `"<phrase>"` OR-fallback `tok1 tok2` (Slack ORs bare terms? no: bare terms are ANDed by the UI; use separate calls per token if OR needed) + `after:YYYY-MM-DD before:YYYY-MM-DD` + `in:#chan` from the previous hop; final fallback `prefix*` (>=3 chars) for truncated IDs. Dates are day-granular only.

### Slack Web API: search.messages

- **Link:** <https://docs.slack.dev/reference/methods/search.messages>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** Params: query (required), count (default 20, max 100), page (max 100), cursor, sort=score|timestamp, sort_dir, highlight, team_id. Rate limit Tier 2 (20+/min). highlight=true wraps matched terms in U+E000 (\xEE\x80\x80) and U+E001 (\xEE\x80\x81). Modifiers in:channel, in:<@UserID>, from:<@UserID>/botname documented; nearby multiple matches collapse to one result.

**Why it matters here.** Hard caps for the link table: max 10,000 results (100x100) per query, ~20 calls/min. highlight=true is the only Slack mechanism to localise the matched substring for the next hop; if your MCP server does not expose it you must re-scan text locally with the same token list.

### korotovsky/slack-mcp-server: conversations_search_messages parameter schema

- **Link:** <https://github.com/korotovsky/slack-mcp-server>
- **Kind:** oss-project
- **Relevance:** `█████████░` 9/10

**What it is.** conversations_search_messages args: search_query (free text + inline modifiers, or a message permalink), filter_in_channel, filter_in_im_or_mpim, filter_users_with, filter_users_from, filter_date_before/after/on/during (YYYY-MM-DD; on/during mutually exclusive with before/after), filter_threads_only (appends is:thread), limit (README says 1-100 default 20; handler code default 100), cursor (base64 "page:N"). Handler source shows filters are simply appended as `in:`, `from:`, `with:`, `before:`, `after:` tokens, highlight is hard-coded false, and results are CSV with columns MsgID, UserID, UserName, RealName, Text, Channel, ThreadTs, Time, Permalink, Reactions, HasMedia. conversations_history/replies take channel_id, thread_ts, limit as count or duration ("1d","2w") and cursor. Search requires a user token (not xoxb).

**Why it matters here.** Exact schema to target from templates; because the server composes the Slack query itself, you can pass modifiers inline in search_query OR via filter_* fields, but no highlight spans come back—localise substrings locally. Cursor is a page counter so parallel page fetches are safe. Source of handler descriptions: https://raw.githubusercontent.com/korotovsky/slack-mcp-server/master/pkg/handler/conversations.go

### Confluence: Advanced searching using CQL (operators, Lucene text syntax, functions)

- **Link:** <https://developer.atlassian.com/cloud/confluence/advanced-searching-using-cql/>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** `~` (CONTAINS) works only on text, title and space.title and matches 'either an exact match or a fuzzy match' using Lucene syntax: wildcards `*`/`?`, fuzzy `word~`, phrase "..." quoting, reserved-character escaping. Other operators =, !=, >, >=, <, <=, IN, NOT IN, !~; keywords AND/OR/NOT; ORDER BY field asc|desc; functions now(), startOfDay()/endOfDay()/startOfWeek()/startOfMonth()/startOfYear() variants, currentUser().

**Why it matters here.** Confluence is one of only two backends with real edit-distance fuzziness. Recipe: `type in (page,blogpost) AND lastModified >= now("-30d") AND (title ~ "<phrase>" OR text ~ "<phrase>")` -> fallback `text ~ "tok1 tok2"` (implicit OR of tokens) -> `text ~ "tok1~ tok2~"` (fuzzy) -> `text ~ "prefix*"`; add `space in (...)` / `ancestor = <id>` when known; escape Lucene reserved chars in extracted IDs (hyphens in ticket keys like ABC-123 should be phrase-quoted).

### Jira Cloud: Search syntax for text fields (JQL ~ operator)

- **Link:** <https://support.atlassian.com/jira-software-cloud/docs/search-syntax-for-text-fields/>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** `~` on text, summary, description, comment. Multi-char wildcard `*` (word end); `?` is auto-converted to trailing `*`. Fuzzy `~`, proximity and boosting `^` are explicitly IGNORED. Phrase: `text ~ "\"jira software\""` (escaped inner quotes). Boolean inside the string: OR is default, AND, NOT, +required, -prohibit, grouping. Stemming is on by default (customize -> customized/customer...). Reserved SQL words and stop words are dropped from the index.

**Why it matters here.** JQL cannot absorb edit-distance fuzziness, so the ladder is: `text ~ "\"<exact phrase>\""` -> `text ~ "tok1 AND tok2"` -> `text ~ "tok1 tok2"` (OR) -> `text ~ "prefix*"`. Bare terms are OR'd by default; put IDs like ABC-123 in escaped quotes. Always append `ORDER BY updated DESC` and bound with `updated >= -14d`.

### Elasticsearch simple_query_string query

- **Link:** <https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-simple-query-string-query>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** Operators: + (AND), | (OR), - (NOT), "phrase", * prefix, ( ), ~N fuzziness on terms / slop on phrases; backslash escaping. Params: fields (with ^boost, wildcards), default_operator, flags (e.g. OR|AND|PREFIX), fuzzy_max_expansions (50), fuzzy_prefix_length (0), fuzzy_transpositions (true), minimum_should_match, quote_field_suffix, analyze_wildcard, lenient. Never errors; invalid parts are ignored.

**Why it matters here.** Safer target for machine-generated queries built from messy extracted substrings (log fragments with parentheses/colons): it degrades instead of 500-ing. Use `flags` to restrict to OR|AND|PHRASE|PREFIX|FUZZY so stray characters cannot change semantics.

### Elasticsearch highlighting (defaults and response shape)

- **Link:** <https://www.elastic.co/guide/en/elasticsearch/reference/8.17/highlighting.html>
- **Kind:** spec
- **Relevance:** `█████████░` 9/10

**What it is.** highlight.fields per field; highlighter type unified (default)/plain/fvh; pre_tags/post_tags default <em>/</em>; fragment_size 100; number_of_fragments 5; no_match_size 0; order:"score" to sort fragments; require_field_match true; highlight_query for a different query; matched_fields. Results at hits.hits[].highlight.<field>[] as fragment strings.

**Why it matters here.** This is the mechanism to get the matched span out of long log lines for the next hop without an LLM: request highlight on message with custom pre/post tags (e.g.  HL ) and number_of_fragments:0 to get the whole field with markers, then regex-extract the neighbourhood of the marker as the next search string.

### CQL field reference (per-field operators, date formats, relative dates)

- **Link:** <https://developer.atlassian.com/cloud/confluence/cql-fields/>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** ancestor/content/id/parent: =, !=, IN, NOT IN. created/lastModified: =, !=, >, >=, <, <= with formats "yyyy/MM/dd HH:mm", "yyyy-MM-dd HH:mm", "yyyy/MM/dd", "yyyy-MM-dd" and relative now("-4w"). creator/contributor/mention/watcher: accountId or name, currentUser(). title and text support ~ and !~; label/macro/fileExtension/pageStatus use =/IN. space by key (quote keys starting with digits). type in page, blogpost, comment, attachment, whiteboard, database, embed, folder.

**Why it matters here.** Gives the deterministic time-window clause (`lastModified >= "2026-08-01"` or now("-2w")) and structural narrowing (ancestor/space/label) that templates can fill from previous hops (space key from a page URL, ancestor id from a parent page).

### Atlassian Rovo MCP Server: supported tools catalogue

- **Link:** <https://support.atlassian.com/atlassian-ai-gateway/docs/supported-tools/>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** Official tool list: Jira read group getJiraIssue, listJiraProjects, searchJiraIssuesUsingJql, listJiraIssueComments, listJiraBoards, getJiraBoardSprintData; Confluence read group getConfluenceContent, listConfluenceContent, listConfluenceSpaces, listConfluenceComments; search_confluence group searchConfluence ('Search Confluence content with CQL'); search_atlassian `search` = semantic natural-language search across Jira/Confluence (beta, up to 10 credits); search_code (searchCode, getCodeFile, getCodeSymbol, diffCodeSymbols); Teamwork Graph getTeamworkGraphContext (up to 10 credits); JSM ops alerts/schedules; Bitbucket, Loom, Goals, Projects, Teams tools. A `discover`/`executeRead` deferred-tool pathway exists. Per-tool parameter schemas (maxResults/nextPageToken) are NOT published on this page.

**Why it matters here.** Confirms the JQL and CQL tools exist and that the semantic `search` tool is credit-expensive (10x)—templates should prefer searchJiraIssuesUsingJql/searchConfluence and only use `search` as a last-resort fuzzy fallback. Parameter names must be read from the live tools/list response since Atlassian does not document them.

### Grafana MCP (grafana/mcp-grafana): Prometheus/Loki/Sift tool parameters

- **Link:** <https://github.com/grafana/mcp-grafana>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** query_prometheus(datasource_uid, expr/query, start_time, end_time, step_seconds, query_type range|instant; RFC3339 or now-1h); query_loki_logs(datasource_uid, query LogQL, start, end, limit default 100 capped by --max-loki-log-limit, direction forward|backward); query_loki_stats(selector); list_loki_label_names/values(matchers, start, end); list_prometheus_label_values, list_prometheus_metric_names(regex, limit, page); search_dashboards(query, folder_uid, tag, starred, limit); find_error_pattern_logs / find_slow_requests (Sift, ephemeral investigations); list_incidents(limit, offset). README advises setting the Loki limit at least 1 below server max_entries_limit_per_query to detect truncation.

**Why it matters here.** Provides concrete parameter names for the metrics/logs hops and the 100-line default that dominates token budgets; list_prometheus_metric_names(regex) and list_loki_label_values(matchers) are cheap 'discovery' calls to turn an extracted service name into valid label values before building the final PromQL/LogQL.

### LogQL log queries: stream selectors, line filters, parsers

- **Link:** <https://grafana.com/docs/loki/latest/query/log_queries/>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** Stream selector matchers =, !=, =~, !~ (regex fully anchored, RE2, must match whole string incl. newlines). Line filters |= (contains), != , |~ (regex, NOT anchored, . matches newline), !~; chainable; (?i) prefix for case-insensitive; ip() filter; parsers | json, | logfmt, | pattern "<f>", | regexp "(?P<n>re)"; label filter expressions with string/duration/number/bytes; line_format/label_format. Line filters early = fastest.

**Why it matters here.** Deterministic Loki recipe: `{service=~"(svc1|svc2)"} |~ "(?i)(id1|id2|id3)"` then narrow with `| json | traceID="..."`. Regex alternation of all extracted IDs is the backend-side 'OR of tokens'; add `|= "exact phrase"` first for the precise tier.

### PromQL basics: label matchers, anchored regex, __name__ trick

- **Link:** <https://prometheus.io/docs/prometheus/latest/querying/basics/>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** Matchers =, !=, =~, !~; regex is RE2 and fully anchored (env=~"foo" == ^foo$); {__name__=~"job:.*"} selects metrics by name pattern; a selector needs a metric name or one non-empty matcher ({job=~".+"} not ".*"). offset and @ modifiers; durations ms/s/m/h/d/w/y with arithmetic. Instant query (query, time) vs range (query, start, end, step); results limited to ~11,000 points.

**Why it matters here.** Recipe: build `sum by (pod) (rate(http_requests_total{service=~"(?i).*checkout.*", status=~"5.."}[5m]))`; because regex is anchored, always wrap fuzzy service names as `.*name.*`. Use list_label_values first to resolve extracted names into exact label values and avoid regex entirely when possible. Keep step so that (end-start)/step < 11,000.

### Chronosphere MCP: logs tools argument schema (logs.go)

- **Link:** <https://raw.githubusercontent.com/chronosphereio/chronosphere-mcp/main/mcp-server/pkg/tools/logs/logs.go>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** query_logs_range(query optional e.g. `service="gateway" AND level="ERROR"` with pointer to a 'Log Query Syntax' MCP resource, time_range required, page_token default "", limit default 0, offset 0) returns timeSeries or gridData with timestamp/message/severity/service by default; get_log(id = logID field); get_log_histogram(query, time_range, group_by) with 100 buckets; list_log_field_names(query, time_range, limit 100); list_log_field_values(query, time_range, field_name, limit 100). Tool dirs: configapi, events, logs, metricusage, monitors, prometheus, traces.

**Why it matters here.** Exact schema for the Chronosphere log hop: results carry a logID so a follow-up get_log can fetch the full line only for hits that matched, which is the cheapest way to localise substrings. The histogram tool is a near-free way to test whether a candidate query matches anything before paying for rows.

### Chronosphere Logging query syntax

- **Link:** <https://docs.chronosphere.io/investigate/querying/query-logs/query-syntax.md>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** Grammar: KEY =|!=|=~|!~|: VALUE combined with AND/OR/NOT and parentheses; `:` is substring contains; `=~`/`!~` are RE2 regex applied to the first 1,024 chars; comparisons <, <=, >, >=; EXISTS; keys with colons via ["key"]; a bare double-quoted string is full-text search anywhere in the log. Pipe transformations: extend, filter, join, lookup, limit, parse/parse-where, project, sort, summarize, top-nested, make-series; aggregations count/avg/sum/min/max/dcount/percentile and *if variants. Recommends always including a primary key (service, severity). Logs Explorer defaults to the last hour; downloads up to 10,000 logs.

**Why it matters here.** Chronosphere recipe: `service="<svc>" AND ("<exact phrase>" OR message:"id1" OR message:"id2")` -> fallback `message=~"(?i)(id1|id2|prefix.*)"`; `| summarize count() by service` for cheap existence checks. Substring `:` gives OR-of-tokens fuzziness without regex cost.

### Google Drive files.list query grammar (ref-search-terms)

- **Link:** <https://developers.google.com/workspace/drive/api/guides/ref-search-terms>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** Terms/operators: name (contains, =, !=; 'contains' does PREFIX matching only: name contains 'Hello' matches HelloWorld but 'World' does not), fullText (contains; matches only entire string tokens; phrase when the operand is wrapped in double quotes; searches name, description, indexableText and file content/metadata), mimeType, modifiedTime/createdTime/viewedByMeTime (<, <=, =, !=, >, >= RFC 3339 UTC), trashed, starred, parents in, owners/writers/readers in, sharedWithMe, properties/appProperties has, visibility, shortcutDetails.targetId. Combine with and/or/not; escape single quotes as \'.

**Why it matters here.** Drive recipe: `fullText contains '"exact phrase"' and modifiedTime > '2026-08-01T00:00:00'` -> `(fullText contains 'tok1' or fullText contains 'tok2')` -> `name contains 'prefix'`; no wildcard/fuzzy exists and tokens must be whole words, so strip punctuation from extracted IDs (e.g. search 'INC' and '12345' separately if the ID tokenises).

### git log: pickaxe (-S/-G), --grep, time bounds, machine-readable format

- **Link:** <https://git-scm.com/docs/git-log>
- **Kind:** spec
- **Relevance:** `████████░░` 8/10

**What it is.** -S<string> finds commits changing the count of occurrences of a string (fixed string; --pickaxe-regex makes it BRE), -G<regex> matches added/removed lines, --pickaxe-all; --grep=<pattern> on messages with --all-match (AND of multiple greps), --invert-grep, -i, -E/-F/-P; --since/--after and --until/--before accept absolute or relative dates ("2 weeks ago", "yesterday"), --since-as-filter; --author (multiple = OR); -n/--max-count, --skip; -- <path>, --follow; --format with %H %h %an %ae %ad %s %b %B and -z for NUL-separated parsing.

**Why it matters here.** Deterministic code-history hop: `git log --since="<ticket time - 7d>" --until="<now>" -i -E --grep="(ABC-123|<slug>)" --all-match -n 50 --format=%H%x09%ad%x09%s`, then `git log -S"<extracted identifier>" --pickaxe-regex -- <paths>` to find the change that introduced/removed the string. -F makes extracted strings safe without escaping.

### Slack conversations.history limits (incl. 2025 non-Marketplace rule)

- **Link:** <https://docs.slack.dev/reference/methods/conversations.history>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** limit default 100, max 999; cursor or oldest/latest (+inclusive) pagination; Tier 3 (50+/min) for Marketplace/internal apps. Since 2025-05-29 newly installed non-Marketplace commercial apps get 1 request/minute and limit max/default 15 objects.

**Why it matters here.** Budget estimation for the slack thread-expansion hop: internal apps get 999 msgs/call at ~50 calls/min; if your token is a non-Marketplace distributed app, plan 15 msgs/min. Prefer time-bounded oldest/latest from the ticket timestamp rather than cursor walks.

### Confluence REST v1 GET /wiki/rest/api/search (CQL endpoint: cursor, limit, excerpt)

- **Link:** <https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** Params: cql (required), cqlcontext, cursor, next, prev, limit, start, includeArchivedSpaces, excludeCurrentSpaces, excerpt, expand, sitePermissionTypeFilter. Cursor-based pagination via _links.next/prev URLs; limit is forced to max 25 when body.export_view/styled_view are expanded. Response has results[], excerpt, totalSize, cqlQuery, searchDuration. (Allowed excerpt values—highlight/indexed/none/highlight_unescaped/indexed_unescaped—and the @@@hl@@@ ... @@@endhl@@@ marker format are from prior knowledge; the page did not enumerate them.)

**Why it matters here.** Confluence is a backend that CAN return match spans (excerpt=highlight) usable to localise the matched substring for the next hop, if your MCP tool passes the excerpt param through. Plan 25 results/page when bodies are expanded.

### Rovo usage limits (credits per user/month; credits per MCP call)

- **Link:** <https://support.atlassian.com/rovo/docs/rovo-usage-limits/>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** Rovo credits per user per month: Jira 25/70/150 and Confluence 25/70/150 (Standard/Premium/Enterprise); Service Collection and Teamwork Collection 250/700/1,500. MCP calls that deliver enriched context consume 1-10 credits per call; extra usage $0.01/credit. No published requests/hour figure for the MCP server.

**Why it matters here.** Directly bounds fan-out: at 25 credits/user/month on Standard, a single investigation chain that uses semantic `search` or getTeamworkGraphContext (10 credits) could exhaust a user's allowance; keep P4 templates on JQL/CQL tools and count calls per run.

### JQL operators reference (~, WAS, CHANGED predicates, date functions)

- **Link:** <https://support.atlassian.com/jira-software-cloud/docs/jql-operators/>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** CONTAINS ~ / !~, IN / NOT IN, IS EMPTY, WAS / WAS IN / WAS NOT, CHANGED with predicates AFTER, BEFORE, BY, ON, FROM, TO, DURING (e.g. `status CHANGED FROM "To Do" TO "Done" AFTER -7d`). Relative dates -1w, -3d, startOfDay(), created >= -1w; ORDER BY created DESC.

**Why it matters here.** Lets the deterministic Jira template express on-call time windows structurally (`status CHANGED TO Done DURING ("-2d", now())`, `assignee WAS currentUser()`), which is more precise than free-text and cheaper than fuzzy search.

### sooperset/mcp-atlassian: confluence_search plain-text -> siteSearch ~ fallback text ~

- **Link:** <https://mcp-atlassian.soomiles.com/docs/tools/confluence-search.md>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Open-source Atlassian MCP (98 tools). confluence_search(query, limit 1-50, spaces_filter): if the query is not CQL it is wrapped as `siteSearch ~ "query"` (relevance-ranked, mirrors UI search) and automatically falls back to `text ~ "query"` if siteSearch is unsupported. jira_get_issue(issue_key, fields incl. "*all", expand renderedFields|transitions|changelog, comment_limit, include, use_display_names). Docs index: https://mcp-atlassian.soomiles.com/llms.txt

**Why it matters here.** A concrete, copyable heuristic for the 'free-text query phrasing' problem: siteSearch ~ gives Confluence's own relevance ranking (fuzzy) with zero local logic; also shows the practical result cap (50) used by an MCP wrapper. Useful if the Rovo server's undocumented params prove too opaque.

### Elasticsearch fuzziness AUTO rules

- **Link:** <https://www.elastic.co/docs/reference/elasticsearch/rest-apis/common-options>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** fuzziness accepts 0, 1, 2 or AUTO (AUTO:[low],[high], default AUTO:3,6): terms of 0-2 chars must match exactly, 3-5 chars allow 1 edit, >5 chars allow 2 edits. AUTO is the recommended value.

**Why it matters here.** Deterministic rule for how much fuzz each extracted token gets; short IDs (e.g. 'pod-7f') stay exact, long hostnames get 2 edits. Mirror this when you build the local fallback matcher.

### Elasticsearch multi_match types and feature support

- **Link:** <https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-multi-match-query>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** Types best_fields (default), most_fields, cross_fields, phrase, phrase_prefix, bool_prefix. Fuzziness supported by best_fields, most_fields, bool_prefix only (not phrase/phrase_prefix/cross_fields). operator and minimum_should_match on best/most/cross/bool_prefix; tie_breaker on best_fields/cross_fields. Field boosts "subject^3" and wildcard field names "*_name".

**Why it matters here.** When the Logz.io/ES tool accepts raw DSL, the ladder maps to types: phrase -> best_fields operator:and -> best_fields operator:or fuzziness:AUTO -> bool_prefix. Note fuzzy and phrase cannot be combined in one multi_match; use a bool/should of two clauses.

### Logz.io Scroll API (/v1/scroll)

- **Link:** <https://api-docs.logz.io/docs/logz/scroll/>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** First call: query DSL, size (max 1,000, default 10), scroll (<= 5 minutes, default 1m), sort/_source; subsequent calls send only scroll_id. scroll_id expires after 20 minutes; empty array signals end; query range limited to 2 consecutive daily indexes (default today+yesterday); scope is the token's account only.

**Why it matters here.** For bulk extraction (e.g. every log line mentioning a request ID over a day), scroll gives 1,000/page but only across 2 days—windows longer than that must be split into multiple day-pair scrolls, which the fan-out planner must model.

### Chronosphere MCP: Prometheus tools and time parsing

- **Link:** <https://raw.githubusercontent.com/chronosphereio/chronosphere-mcp/main/mcp-server/pkg/tools/prometheus/tools.go>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** query_prometheus_range(query, time range, step_seconds default 60, limit default 100 series (0 = unlimited), offset); query_prometheus_instant(query, time RFC3339 default now); list_prometheus_label_values(label_name, selectors[], time range, limit 100, offset); list_prometheus_series(selectors[] required, limit 100, offset). Time parser (mcp-server/pkg/tools/pkg/params) accepts relative now-1h/now-30m/now-7d or -1h (units d/h/m only), Unix seconds, or RFC3339. list_traces(service | operation | trace_ids, time_range, limit, offset); trace_ids cannot combine with service/operation.

**Why it matters here.** Gives the deterministic time-window encoding (`now-2h`) and the 100-series default cap; list_prometheus_label_values with selectors resolves fuzzy service names to exact values. Traces are only addressable by service/operation/trace_id, so trace IDs extracted from logs go straight into trace_ids.

### PagerDuty MCP server (archived, moved to hosted mcp.pagerduty.com): IncidentQuery and pagination models

- **Link:** <https://raw.githubusercontent.com/PagerDuty/pagerduty-mcp-server/main/pagerduty_mcp/models/incidents.py>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** list_incidents(query_model: IncidentQuery) with status[] triggered|acknowledged|resolved, since/until datetime (ISO), user_ids, service_ids, teams_ids, urgencies high|low, date_range="all", request_scope all|teams|assigned, limit 1-1000 default 100, sort_by up to two of incident_number|created_at|resolved_at|urgency with :asc/:desc. LogEntryQuery: since, until, limit 100, offset 0, is_overview, team_ids, time_zone. base.py: DEFAULT_PAGINATION_LIMIT 20, MAXIMUM_PAGINATION_LIMIT 100, MAX_RESULTS 1000 with a warning when count == 1000. Repo archived 2026-09-04; hosted server at https://mcp.pagerduty.com/mcp (EU: mcp.eu.pagerduty.com). Underlying REST GET /incidents caps limit at 100 per page (OpenAPI https://raw.githubusercontent.com/PagerDuty/api-schema/main/reference/REST/openapiv3.json). No free-text search on incidents—only structured filters plus incident_key.

**Why it matters here.** PagerDuty has no fuzzy text search at all: the deterministic hop is purely structural (service_ids from a service name resolved via list_services, since/until from the ticket window, statuses). Matching an extracted phrase must be done locally over incident titles/alert bodies, so fetch with limit 100 pages and filter client-side.

### Drive files.list parameters (pageSize, orderBy, corpora, incompleteSearch)

- **Link:** <https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** q, pageSize default 100 max 1000, pageToken/nextPageToken, orderBy keys createdTime, modifiedTime, name, name_natural, folder, starred (+ desc), corpora user|domain|drive|allDrives, includeItemsFromAllDrives + supportsAllDrives required for shared drives, spaces, fields; response incompleteSearch flag. Quotas (https://developers.google.com/workspace/drive/api/guides/limits): 1,000,000 quota units/min per project, 325,000/min per user; 403 userRateLimitExceeded or 429, exponential backoff.

**Why it matters here.** Pagination and caps for the Drive hop; set includeItemsFromAllDrives or corporate shared-drive docs will be silently missing; check incompleteSearch before concluding 'no match'. The archived reference gdrive MCP (https://raw.githubusercontent.com/modelcontextprotocol/servers-archived/main/src/gdrive/README.md) exposes a single `search(query)` tool that (per its source, from prior knowledge) wraps the string as fullText contains '<escaped>' with pageSize 10—so raw q syntax is not reachable through it.

### Cloud Logging query language (has ':' substring, =~ regex, SEARCH(), global restriction)

- **Link:** <https://docs.cloud.google.com/logging/docs/view/logging-query-language>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** Operators =, !=, <, >, <=, >=, `:` (has/substring: textPayload:"error"), =~ / !~ regex. A bare quoted term is a global restriction matched with has across all fields. AND/OR/NOT (NOT highest precedence, AND may be omitted, - for NOT). timestamp >= "2023-11-29T23:00:00Z" (RFC3339; YYYY-MM-DD accepted). SEARCH("...") does case-insensitive token matching on indexed fields. Quote strings containing spaces or ~ = ( ) : > < , . * or starting with + - . ; query max 20,000 chars.

**Why it matters here.** Cloud Logging recipe: `resource.type="k8s_container" AND timestamp>="..." AND ("exact phrase" OR jsonPayload.message:"id1" OR jsonPayload.message:"id2")` -> `jsonPayload.message=~"(?i)(id1|id2)"` -> `SEARCH("tok1 tok2")`. The 20k-char limit means you can OR hundreds of extracted IDs in one call.

### git grep: multi-pattern boolean search, tree scoping, positions

- **Link:** <https://git-scm.com/docs/git-grep>
- **Kind:** spec
- **Relevance:** `███████░░░` 7/10

**What it is.** -e <pattern> repeated with --and/--or/--not and ( ); -F/-E/-P/-G pattern types; -i; -w word boundary; -l, -n, --column, -c/--count; -A/-B/-C context; --all-match; --max-depth; -z; --threads; search an arbitrary tree/commit (`git grep pat HEAD~5`) and pathspecs after -- ('*.go', ':^Documentation').

**Why it matters here.** git grep -n --column -e id1 --or -e id2 returns file/line/column spans—the code-side equivalent of ES highlight—so extracted IDs can be localised precisely for the next hop (e.g. to feed ast-grep a function name). --and of two extracted tokens is the 'precise' tier, --or the fuzzy tier, -i -w to control noise.

### Jira Cloud REST API rate limiting (points quota, burst, headers)

- **Link:** <https://developer.atlassian.com/cloud/jira/platform/rate-limiting/>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** Hourly points quota: 65,000 global default; per-tenant Standard 100,000+10xusers, Premium 130,000+20xusers, Enterprise 150,000+30xusers, cap 500,000. Reads cost 1 point (identity reads 2). Burst limits by method: GET/POST 100 req/s, PUT/DELETE 50 req/s; custom endpoints 5-400 req/s. 429 with Retry-After, X-RateLimit-Remaining, X-RateLimit-Reset, RateLimit-Reason. Enforcement from 2 March 2026. (Enhanced search endpoint /rest/api/3/search/jql—nextPageToken/isLast pagination, maxResults default 50 with a documented max of 5000, POST /search/approximate-count—is from prior knowledge; the API page was truncated, see https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/.)

**Why it matters here.** Budget model for Jira hops: effectively unlimited for a single investigation (1 point/read), but the Rovo credit budget is the binding constraint, not REST rate limits. Use nextPageToken-style pagination and request only needed `fields` to reduce payload/tokens.

### Elasticsearch wildcard query (keyword fields, case_insensitive)

- **Link:** <https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-wildcard-query>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** Params value (? and *), boost, rewrite, case_insensitive. Leading * or ? is expensive and can be blocked by search.allow_expensive_queries=false. Recommends n-gram tokenizer or combining with match/bool for heavy use.

**Why it matters here.** For extracted substrings that are part of a keyword field (e.g. kubernetes.pod_name.keyword: 'checkout-*'), wildcard with case_insensitive:true is the deterministic fuzzy option; Logz.io additionally forbids leading wildcards (see Logz.io search API).

### jklnr/logzio-mcp-server (unofficial) tool schema; official logzio/logzio-mcp-server not found

- **Link:** <https://github.com/jklnr/logzio-mcp-server>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Unofficial Logz.io MCP: search_logs(query, timeRange 1h|6h|12h|24h|3d|7d|30d or from/to ISO-8601, logType, severity, limit 1-1000, sort asc|desc), query_logs(luceneQuery required, from/to, size 1-1000, sort), get_log_stats(timeRange/from/to, groupBy); config region us|us-west|eu|ca|au|uk, --max-results default 1000. npm also lists 'logzio-mcp-server' 1.0.2 by oleander (repo README 404). The GitHub org path github.com/logzio/logzio-mcp-server returned 404 on main and master.

**Why it matters here.** Shows the two shapes a Logz.io MCP typically exposes: a Lucene query_string passthrough (so the query_string grammar above applies, minus fuzzy/leading wildcard) plus a coarse timeRange enum. Verify your corporate server's exact schema via tools/list; expect size caps of 1,000.

### Loki HTTP API: query_range parameters and time formats

- **Link:** <https://grafana.com/docs/loki/latest/reference/loki-http-api/>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** GET /loki/api/v1/query_range: query, limit (default 100), start, end, since (duration relative to end), step, interval, direction (backward default). Timestamps: nanosecond Unix epoch, float seconds, or RFC3339/RFC3339Nano. /label/<name>/values: start (default 6h ago), end, since, query (selector). /series: match[] repeated.

**Why it matters here.** Underlying caps and time formats behind the Grafana MCP Loki tools; server-side max_entries_limit_per_query (Loki default 5000, from prior knowledge) bounds any single hop.

### gcloud observability MCP: listLogEntries parameters

- **Link:** <https://raw.githubusercontent.com/googleapis/gcloud-mcp/main/packages/observability-mcp/src/tools/logging/logging_api_tools.ts>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** listLogEntries(resourceNames[] required e.g. projects/<id>, filter string, orderBy 'timestamp asc'|'timestamp desc' default asc, pageSize default 50, pageToken) returning JSON. Sibling tools list_log_names, list_time_series (Monitoring filter), list_traces, get_trace, list_group_stats. gcloud-mcp core exposes run_gcloud_command with a deny-list (https://github.com/googleapis/gcloud-mcp).

**Why it matters here.** The filter param is a raw Cloud Logging query string, so the Cloud Logging grammar below is the fuzzy surface; default 50 entries and no highlight—localise locally. Default ascending order means you must set orderBy desc to get the newest lines first within a token budget.

### Azure MCP Server: Azure Monitor tools (workspace/resource log query, metrics)

- **Link:** <https://learn.microsoft.com/en-us/azure/developer/azure-mcp-server/tools/azure-monitor>
- **Kind:** spec
- **Relevance:** `██████░░░░` 6/10

**What it is.** monitor workspace log query: Query (KQL, or predefined `recent` / `errors`), Resource group, Table name, Workspace name (required), Hours, Limit (optional). monitor resource log query: Query, Resource ID, Table name, Hours, Limit. Metrics query: Metric names (comma list), Metric namespace, Resource name, Aggregation, Start/End time ISO (default 24h ago..now), Filter (OData), Interval (PT1M/PT1H), Max buckets default 50, Resource type. Activity log: Resource name, Event level, Hours, Top. Metric definitions: Search string (case-insensitive on name/description), Limit default 10. Workbooks list: Name contains (case-insensitive), Modified after, Max results 50 default/1000 max.

**Why it matters here.** KQL is the fuzzy surface here: generate `<Table> | where TimeGenerated > ago(<Hours>h) | where Message has_any ("id1","id2") or Message matches regex "(?i)(id1|id2)" | take <Limit>`; KQL's has/has_any (term-based), contains (substring) and matches regex cover the ladder. Hours + Limit are the only budget knobs exposed.

### modelcontextprotocol/servers git MCP tool schema

- **Link:** <https://raw.githubusercontent.com/modelcontextprotocol/servers/main/src/git/README.md>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Tools git_status, git_diff(_unstaged/_staged) (context_lines default 3), git_show, git_log (max_count default 10, start_timestamp/end_timestamp ISO 8601 or relative), git_branch (contains/not_contains SHA), git_add, git_commit, git_reset, git_create_branch, git_checkout; all require repo_path. No grep, pickaxe or --grep parameter is exposed.

**Why it matters here.** If your 'arc/git' MCP is this reference server, none of the git-native fuzziness (-S, -G, --grep) is reachable—only time-bounded git_log with max_count 10—so message matching must be done locally over fetched commits, or you must add a thin custom tool that shells out to git log/grep with the flags above.

### Elasticsearch match_phrase_prefix caveats

- **Link:** <https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-match-query-phrase-prefix>
- **Kind:** spec
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Last term is a prefix; max_expansions default 50 terms chosen alphabetically per shard, so a common prefix may miss the intended term; slop default 0; alternatives search_as_you_type / match_bool_prefix.

**Why it matters here.** Warns that prefix-completion of truncated IDs from log lines is unreliable on high-cardinality fields (trace IDs, pod names); prefer wildcard on keyword fields or longer prefixes.

### Dead ends

- WebSearch: session budget (200/200) already exhausted before this task; all research done via direct WebFetch of known/guessed URLs.
- github.com/logzio/logzio-mcp-server (main and master) returned 404; no official Logz.io MCP repo or docs page (docs.logz.io/docs/user-guide/mcp-server/) was found. Only unofficial servers (jklnr/logzio-mcp-server, oleander's npm 'logzio-mcp-server', thiagoluiznunes/mcp-logzio) exist; the corporate server's schema must be read from tools/list.
- Logz.io metrics/PromQL API docs: api-docs.logz.io/docs/logz/metrics-api/, /prometheus-query/, docs.logz.io metrics-api pages all 404.
- Atlassian Rovo MCP: no public page documents per-tool parameters (maxResults, nextPageToken/cursor) or a requests-per-hour rate limit; support.atlassian.com/atlassian-rovo-mcp-server/docs/available-tools-and-permissions/ and /rate-limits/ 404; only credit costs (1-10 per call) are published.
- Jira enhanced search /rest/api/3/search/jql details (maxResults max 5000, nextPageToken, isLast, approximate-count): API reference page was truncated by WebFetch and changelog pages did not contain CHANGE-2046; values in findings are from prior knowledge and should be verified.
- PagerDuty developer portal pages (rate limits, list-incidents reference, mcp-server docs) render client-side and came back empty; rate-limit numbers (commonly cited 960 req/min per account) could not be verified from a primary source.
- Confluence search `excerpt` allowed values and the @@@hl@@@ highlight marker format were not enumerated on the REST page fetched; stated from prior knowledge.
- Chronosphere MCP docs page (docs.chronosphere.io/integrate/mcp-server.md) and log-limits page contain no query-result caps or rate limits; the logs tool 'limit' default 0 semantics (unlimited vs server default) are not documented.
- Azure MCP azmcp-commands.md was truncated by the fetch (only non-monitor sections visible); Microsoft Learn page was used instead.
- Google Workspace MCP (taylorwilsdon) README does not document search_drive_files parameters; docs live at workspacemcp.com which was not fetched.
- mcp-atlassian jira-search page (docs/tools/jira-search.md) 404; jira_search limit/start_at defaults not confirmed.
