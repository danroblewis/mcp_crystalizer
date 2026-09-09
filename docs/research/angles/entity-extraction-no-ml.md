# Entity and ID extraction from unstructured text without ML

_Research angle `entity-extraction-no-ml`. Generated from the research workflow run on 2026-09-09._

## Angle verdict

Yes, there is a real reusable pattern here, and it is a layered one I would call "typed mask catalog + gazetteer" (or "mask-first entity extraction"). Layer 1: a one-time LLM pass turns recorded (tool_result, next_call_argument) pairs into a catalog of typed regexes, using DeepParse's contract (output a Python list of regexes, each with a variable category; validate with re.compile, dedupe, re-prompt on failure) and Regexulator's FP/FN tree-search loop with span-offset examples as the way to make those regexes reliable from 5-15 traces. Layer 2: at zero-LLM runtime, the catalog is consumed by Drain3 masking_instructions for machine text (logs, alerts, CLI output) so extract_parameters() returns (value, mask_name) slots, and by an Aho-Corasick/FlashText gazetteer built from one-time MCP catalog dumps for prose (Slack, Jira, Confluence), with RapidFuzz as a thresholded fallback for aliases and typos and partial_ratio_alignment to recover the substring that goes into a free-text search param. The mask names / entity types are the key output: they are what lets a deterministic planner route a TRACE_ID to logz.io and a SERVICE to Chronosphere without an LLM choosing. All five pieces are in-process libraries with file persistence, satisfying the no-server constraint. The honest gap in this angle is context-dependent selection (which of three services in a Slack thread is the one to investigate); regexes and gazetteers give you the candidates with offsets, and the ranking rule (nearest to the alert text, most frequent, first mentioned after the Jira key) has to be learned from traces separately - the Regexulator paper's tumour-size failure is exactly this limitation.

## Deep reads

### Drain3 - streaming log template miner with masking + extract_parameters()

- **Link:** <https://github.com/logpai/Drain3>
- **Evidence quality:** production-proven

**Key techniques**

- Two-stage pipeline: a masker applies an ordered list of (regex_pattern -> mask_with) instructions from drain3.ini (e.g. IP, NUM, HEX) so known ID shapes become typed tokens like <IP>, then the fixed-depth Drain tree clusters the pre-masked line; anything still variable becomes the untyped <*> wildcard.
- add_log_message(line) returns {change_type: cluster_created|cluster_template_changed|none, cluster_id, cluster_size, cluster_count, template_mined}; match(line, full_search_strategy='never'|'fallback'|'always') is inference-only: 'never' is tree-only O(log n) with possible false negatives, 'fallback' linear-scans clusters of the same token count only if the tree misses, 'always' picks the same-token-count cluster with the fewest wildcards.
- extract_parameters(template, message, exact_matching=True) builds a regex from the template: re.escape(template), then each <MASK> occurrence is replaced (sequentially, one unique named group per occurrence) by (?P<pN>joined_patterns) where joined_patterns is the union of the masking instruction regexes for that mask name (named groups inside them are rewritten to avoid collisions); the '*' mask (and every mask when exact_matching=False) becomes .+?. Returns an ordered list of ExtractedParameter(value, mask_name). Generated regexes are cached per template (MASKING/parameter_extraction_cache_capacity).
- State (tree + clusters) persists via File (JSON snapshot), Memory, Redis or Kafka handlers; PersistenceHandler is subclassable. max_clusters enables LRU eviction; sim_th (0.4), depth (4), max_children (100), extra_delimiters and parametrize_numeric_tokens are the core tuning knobs.

**How to apply it.** Run once over recorded logz.io / Chronosphere-alert / Slack-bot-message tool results (add_log_message) with the company's ID regex catalog as masking instructions, persist to a file; at no-LLM runtime call match() + extract_parameters() on each new result line to get typed slots (mask_name=POD, TRACE_ID, HTTP_STATUS, JIRA_KEY...) that feed the next MCP call's parameters. Helps most with logz.io, chronosphere, pagerduty incident bodies, and Slack bot posts; the typed mask names are what let a deterministic planner decide 'TRACE_ID -> logz search, POD -> gcloud describe'.

**Limitations.** Only useful for machine-generated, repetitive text; human prose in Slack/Confluence/Jira does not cluster into templates. Untyped <*> slots extract as .+? and are ambiguous. Masking is regex-first, so its quality is entirely the quality of your mask catalog (which is where the LLM step belongs). match() with 'never' can miss lines that were never seen in training.

**Quotes**

> Template parameters that do not match any custom mask in the preliminary masking phase are replaced with <*> by Drain core.

> In inference mode you should call template_miner.match(log_line). This will match log line against previously learned clusters only. No new clusters are created and templates of existing clusters are not changed.

> Parameter extraction is performed by generating a regular expression that matches the template and then applying it on the log message. When exact_matching is enabled (by default), the generated regex included the regular expression defined in relevant masking instructions.

### DeepParse: Hybrid Log Parsing with LLM-Synthesized Regex Masks (arXiv 2604.20553)

- **Link:** <https://arxiv.org/html/2604.20553v1>
- **Evidence quality:** research-prototype

**Key techniques**

- Offline reasoning phase: pick ~50 diverse lines per system with entropy-greedy sampling (normalize digits/hex, compute token-frequency entropy per line, accept a candidate only if Jaccard similarity to every already-selected line < 0.8), then prompt the LLM: 'Generate a Python list of regex patterns that capture the dynamic (variable) parts in the input log message while preserving the static structure.' Output is forced to be a Python list so it can be programmatically validated; greedy decoding, 512 max tokens. Each regex is tagged with a variable category (IP, PATH, USER, TIMESTAMP...).
- Validation loop: dedupe, canonical ordering by category, re.compile syntax check; failures fall back to heuristic patterns; a self-consistency step has the model re-parse its own outputs with a lightweight interpreter and re-prompts with targeted feedback on discrepancy ('typically converges within two attempts').
- Runtime 'Mask-First' strategy: incoming lines are matched against the synthesized regexes, matched spans replaced with typed placeholders (<VAR:IP>, <VAR:PATH>), and an extended Drain3 builds its parse tree over the pre-masked tokens. Deterministic: identical lines always get the same template ID.
- Cost/results: one-time per-system synthesis (they LoRA-fine-tune DeepSeek-R1:8B on 50 labeled examples per system, minutes) then zero LLM at runtime; 0.30 s per 100 logs vs 28.93 s for per-line LLaMA-7B parsing (~100x). Corrected LogHub-2k, 16 systems: avg Parsing Accuracy 0.9763 vs LLMParser 0.9587 vs Drain 0.3353; Grouping Accuracy 0.9413 vs 0.8873 vs 0.8605; p<0.01; +-0.4% PA variance across 5 seeds; >30% fewer false alarms in a LogBERT anomaly pipeline.

**How to apply it.** This is the user's plan, benchmarked: LLM writes a typed regex mask catalog once, Drain3 consumes it forever. Copy three specific things: (1) the entropy-greedy + Jaccard-diversity sampler for choosing which recorded tool results to show the LLM (keeps the one-time token spend tiny), (2) 'output a Python list of regexes, each with a variable category' as the contract, with re.compile + dedupe + re-prompt-on-failure validation, (3) mask-first then template mining. Applies to logz.io, chronosphere, pagerduty, gcloud/azure CLI output; the variable categories double as the type system for routing IDs to the next MCP tool.

**Limitations.** Evaluated only on LogHub-2k (2,000 lines/system), not LogHub-2.0 or proprietary logs; uses a fine-tuned 8B model rather than pure prompting, so the 97.6% number is not free; requires re-synthesis when log formats drift; a very new preprint with no independent replication. Says nothing about prose sources (Slack, wiki).

**Quotes**

> Generate a Python list of regex patterns that capture the dynamic (variable) parts in the input log message while preserving the static structure.

> Incoming logs are first matched against synthesized regexes, matched spans are replaced with typed placeholders (e.g., <VAR:IP>, <VAR:PATH>), and Drain builds its fixed-depth parse tree over the resulting pre-masked tokens

> By separating the reasoning phase from execution, DeepParse enables accurate, scalable, and cost-efficient log structuring without relying on brittle handcrafted rules or per-line neural inference.

### Regexulator / 'From Examples to Patterns: LLM-Generated Regular Expressions for Entity Extraction in Czech Clinical Texts' (Zelina, RASLAN 2024)

- **Link:** <https://nlp.fi.muni.cz/raslan/2024/paper6.pdf>
- **Evidence quality:** research-prototype

**Key techniques**

- Input contract: examples are (document_text, [(start, end), ...]) span pairs; in code: {"string": ..., "match": [{"start": 17, "end": 22}]}. Optional train/validation/test splits; validation set is only seen through metrics, never shown to the LLM.
- Prompt construction: each snippet shown twice, as a backtick-highlighted span inside 20-50 chars of surrounding context (stopping at linebreaks, so the LLM can use lookaheads/context) and as a bare extraction list; chain-of-thought instruction ('First, write down common patterns in the extraction'); answer must end with a single 'FINAL REGEX: ...' line, parsed by regex.search(r'FINAL REGEX:\s*(.*)'). Default 10 snippets for start nodes, 5 for improve nodes.
- Tree-of-Thought search: initial_splits start nodes (positive examples only), then improve nodes that receive the current pattern plus true positives ('should remain to match'), false negatives ('should also match') and false positives ('do NOT match') computed by running the pattern on the training set. Each executed node spawns branching_factor children; a worker pool executes nodes from a priority queue with priority = validation_score * depth_base^depth * sibling_base^sibling_index; stops on tree exhaustion or time_limit. Two internal checks per node: compile check (feed the re error back) and self-validation ('does your reasoning make sense?'). Node diversity via per-node example-sampling seed and temperature > 0.
- Scoring (evaluator.py): match_f1 over exact spans (tp if predicted span is in ground truth) and char_f1 over a numpy array where gt=1, pred=2, overlap=3; mean_f1 = (char_f1 + match_f1)/2 is the selection and priority metric.
- Results on the Czech clinical 'time' label vs RegexGenerator++ (genetic programming), 5 runs: exact-match F1 x100 at 8-15 snippets 90+-1 vs 75-78+-5..7; at 4-7 snippets 62 vs 37-38; at 64-127 87 vs 35-60. Wall time ~1.2-1.4 min for every training size (llama3.1:70b Q4 on one H100, 4 workers, 60 s limit, ~16 conversations per run) vs 0.1-43 min for RG++.

**How to apply it.** Exact recipe for the 'have an LLM write deterministic extractors from recorded traces' step. Build the span dataset automatically: for each recorded (tool_result, next_tool_call) pair, locate every argument string of the next call inside the previous result and record its character offsets; that is precisely Regexulator's (text, spans) input. Run the FP/FN improve loop once per (source tool, target param) pair to get a plain Python regex; the validation split guards against overfitting to a handful of traces. This is the right tool for semi-structured prose where Drain fails: Slack messages, Jira descriptions, Confluence runbooks, PagerDuty notes, and for learning which substring of a Jira ticket becomes the logz.io search query. Repo is MIT but tiny (6 commits, Ollama-only); the algorithm is ~300 lines and trivially re-implemented against the Claude API.

**Limitations.** Regexes only cover entities with a small number of surface forms; the paper's own tumour-size example shows it cannot learn context-dependent selection ('which of several dimensions') without many more examples. Evaluated on one Czech clinical dataset and only date/time labels quantitatively; entity results are qualitative on <10 examples. The GitHub repo has 0 stars and README documents nothing beyond the constructor; prompts and search live in prompt_templates.py / explorer.py / evaluator.py. Needs a validation split to be meaningful, which means enough recorded traces per extractor (paper says 4-7 snippets minimum, 8-15 for stable ~0.9 F1).

**Quotes**

> Through this iterative process, we expose the model to false positives and false negatives, improving precision and recall. We also branch the LLM conversations into a search tree to leverage the inherent parallelizability of LLMs, improve training speed and stabilize the overall performance of this method.

> Our method achieves reasonable performance with as few as 4–7 training snippets. If an exact match is not required and the approximate location of the extracted entity is sufficient, the method starts to work even with 1-3 training snippets.

> Write the final regex on the last line with a heading of FINAL REGEX: ... Make sure it is on the same line as the heading and is not inside a code block. Do not write anything else after that.

### ahocorasick_rs (G-Research) and FlashText - gazetteer matching from a known entity list

- **Link:** <https://github.com/G-Research/ahocorasick_rs>
- **Evidence quality:** production-proven

**Key techniques**

- ahocorasick_rs: AhoCorasick(patterns, matchkind=MatchKind.Standard|LeftmostFirst|LeftmostLongest, implementation=NoncontiguousNFA|ContiguousNFA|DFA, store_patterns=bool). find_matches_as_indexes(haystack) -> [(pattern_index, start, end)]; find_matches_as_strings(haystack, overlapping=False). overlapping=True only with Standard matchkind. LeftmostLongest 'returns the leftmost-in-the-haystack matching pattern that is longest' - resolves payments vs payments-api. DFA = slowest build, fastest scan. Str matching releases the GIL. Apache-2.0; README claims 1.5x-7x faster than pyahocorasick. No case-insensitivity and no word-boundary option exposed: normalize case yourself and post-filter boundaries.
- FlashText (pure Python, MIT): KeywordProcessor(case_sensitive=False); add_keyword('Big Apple', 'New York') maps surface form -> canonical name; add_keywords_from_dict({'java': ['java_2e', 'java programing']}) for many aliases per entity; extract_keywords(text, span_info=True) -> [('New York', 7, 16), ...]; replace_keywords for normalization; add_non_word_boundary('/') to make e.g. slashes part of tokens. Word-boundary semantics built in (a keyword only matches as a whole token), which ahocorasick_rs lacks.

**How to apply it.** The cheapest 'fuzzy' component: dump the catalogs once via MCP (Jira project keys and component names, Slack channel names, k8s namespaces/deployments from gcloud/azure, service names from the codebase repo list and Chronosphere label values, PagerDuty service names, Confluence space keys, on-call team names) into a single automaton keyed to (entity_type, canonical_id). Scan every tool result - Slack threads, wiki pages, ticket bodies, log lines - and emit typed mentions with offsets. Use FlashText's alias dict for surface forms the LLM discovers in traces ('pay svc' -> payments-api) and ahocorasick_rs when catalogs reach tens of thousands of entries or throughput matters. This is what lets a deterministic planner say 'this Slack message mentions service X and namespace Y, so query Chronosphere for X in Y'.

**Limitations.** Exact match only: typos, camelCase variants and pluralization miss unless you enumerate aliases. Short catalog entries (e.g. a service named 'api' or 'core') produce false positives in prose; you need per-entry word-boundary/minimum-length rules and a stoplist. Catalogs go stale; re-dump on a schedule. ahocorasick_rs README exposes no case folding or boundary handling.

**Quotes**

> LeftmostLongest: returns the leftmost-in-the-haystack matching pattern that is longest

> keyword_processor.extract_keywords('I love big Apple and Bay Area.', span_info=True)  # [('New York', 7, 16), ('Bay Area', 21, 29)]

> 1.5× to 7× as fast as pyahocorasick

### RapidFuzz - typo-tolerant matching against a catalog

- **Link:** <https://github.com/rapidfuzz/RapidFuzz>
- **Evidence quality:** production-proven

**Key techniques**

- process.extract(query, choices, scorer=fuzz.WRatio, limit=N, score_cutoff=T, processor=utils.default_process) -> [(choice, score, index_or_key)]; process.extractOne for the best hit; process.cdist for a full query x choices score matrix. choices may be a dict, in which case the key is returned - handy for alias -> canonical_id.
- Scorers with distinct semantics: ratio (Indel similarity), partial_ratio ('Searches for the optimal alignment of the shorter string in the longer string and returns the fuzz.ratio for this alignment' - i.e. fuzzy substring, exhaustive for needles <= 64 chars), partial_ratio_alignment (same but returns the alignment: score plus src/dest start/end offsets, so you can recover the matched span), token_sort_ratio (order-insensitive), token_set_ratio (returns 100 if one token set is a subset of the other - 'PaymentsAPI service' vs 'payments api'), WRatio (weighted combination used as the default). score_cutoff: 'For ratio < score_cutoff 0 is returned instead' and extract drops those entries. C++ core, MIT.

**How to apply it.** Second pass after the exact gazetteer: take candidate tokens/ngrams from Slack, Jira and Confluence text that the Aho-Corasick scan did not hit, default_process them (lowercase, strip punctuation), and extractOne against the catalog with token_set_ratio or WRatio and a cutoff of ~85-90 to map 'payment svc', 'PaymentsAPI', 'payments-api-v2' to the canonical service. partial_ratio_alignment gives the span inside a long log line or wiki paragraph so the substring can be handed to a free-text search param (logz.io query, Confluence CQL, Slack search). Also usable to choose among several candidate search strings by scoring them against the string the agent actually used in recorded traces.

**Limitations.** Fuzzy scores are relative, so thresholds must be tuned per entity type and per catalog size; short strings score high spuriously. Linear in catalog size per query (fine for thousands, not millions, unless you batch with cdist). Docs page for partial_ratio_alignment lists only the signature; return fields come from the API (ScoreAlignment). Not a substitute for context: it says 'this looks like payments-api', not 'this is the service the incident is about'.

**Quotes**

> Searches for the optimal alignment of the shorter string in the longer string and returns the fuzz.ratio for this alignment.

> For ratio < score_cutoff 0 is returned instead. Default is 0, which deactivates this behaviour.

> process.extractOne("cowboys", choices, scorer=fuzz.WRatio)  # ('Dallas Cowboys', 83.07692307692308, 3)

## All sweep findings

_18 findings, sorted by relevance score._

### Drain3 - streaming log template miner with masking + extract_parameters()

- **Link:** <https://github.com/logpai/Drain3>
- **Kind:** oss-project
- **Relevance:** `█████████░` 9/10

**What it is.** Production Python implementation of the Drain fixed-depth-tree log parser. You feed raw lines via add_log_message(); it clusters them into templates with <*> wildcards. Masking instructions are a config list of (regex -> mask name) pairs (IP, HEX, NUM, custom), applied before clustering so known ID shapes become typed slots. extract_parameters() then returns an ordered list of ExtractedParameter(value, mask_name) for a line by compiling the template into a regex; match() gives an inference-only mode against learned clusters with no tree mutation, and state persists to file/memory/Redis/Kafka.

**Why it matters here.** Directly usable in-process on logz.io / Slack / Chronosphere output: run it once over recorded tool results to learn templates, then in the no-LLM runtime use match()+extract_parameters() to pull out the variable slots (pod names, request ids, error codes) which become parameters for the next MCP call. The mask list is the natural place to plug in the user's ID regex catalog. Pure python, file persistence, no server.

### DeepParse: Hybrid Log Parsing with LLM-Synthesized Regex Masks

- **Link:** <https://arxiv.org/abs/2604.20553>
- **Kind:** paper
- **Relevance:** `█████████░` 9/10

**What it is.** Uses an LLM in an offline 'reasoning phase' on small log samples to mine reusable variable patterns, converts them into regex masks, then runs plain Drain with those masks deterministically at parse time - no per-line neural inference. Reports 97.6% average parsing accuracy across 16 Loghub benchmarks, better variable extraction and consistency than both heuristic (Drain) and LLM-only parsers, and 36% lower latency in a downstream anomaly-detection pipeline.

**Why it matters here.** This is almost exactly the user's plan (one-time LLM use -> deterministic extractor -> zero-LLM runtime) validated on the log-parsing task. Pattern to copy: LLM output is a mask/regex catalog fed into Drain3's masking_instructions, not a parser itself. Gives a defensible accuracy ceiling for the approach.

### Regexulator / 'From Examples to Patterns: LLM-Generated Regular Expressions' (RASLAN 2024)

- **Link:** <https://github.com/ZepZep/regexulator>
- **Kind:** paper
- **Relevance:** `█████████░` 9/10

**What it is.** MIT-licensed Python tool (paper: nlp.fi.muni.cz/raslan/2024/paper6.pdf) that hands an LLM (via Ollama, e.g. llama3.1:70b) example documents with character-offset match spans and asks for a regex; then runs a tree search of 'improve' nodes where each node is shown the current pattern's false positives and false negatives on a validation split, branching in parallel and prioritizing by mean of exact-match F1 and character F1. On the time/date datasets it beat RegexGenerator++ (GP) at every training size and was far more stable: exact-match F1 0.90 with 8-15 snippets vs 0.75-0.78 for RG++, and it works with 4-7 snippets; wall time is flat (~1.3 min) regardless of example count.

**Why it matters here.** A ready-made recipe for the 'have an LLM write deterministic extractors from recorded traces' step: input format is (text, [start,end] spans) which is exactly what you get by diffing a tool result against the ID the agent later used in the next call. The FP/FN feedback loop is the key trick that makes LLM-written regexes reliable; the resulting pattern is a plain Python regex with zero runtime LLM cost.

### ahocorasick_rs / pyahocorasick / FlashText - gazetteer matching from a known entity list

- **Link:** <https://github.com/G-Research/ahocorasick_rs>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** In-process multi-pattern exact matchers. ahocorasick_rs wraps the Rust aho-corasick crate: build AhoCorasick(list_of_names) then find_matches_as_indexes(text) returns (pattern_idx, start, end); match kinds Standard (all/overlapping matches), LeftmostFirst, LeftmostLongest; README claims 1.5x-7x faster than pyahocorasick. FlashText (vi3k6i5) is the pure-Python trie variant with word-boundary semantics and keyword->canonical-name mapping, benchmarked far faster than alternation regexes once you have thousands of keywords.

**Why it matters here.** The cheapest 'fuzziness' win: load the service catalog, team names, k8s namespaces, Jira project keys, alert names, and known hostnames from one-time MCP dumps into an automaton and scan every Slack message/wiki page/log line for mentions. LeftmostLongest resolves 'payments' vs 'payments-api'. No ML, no server, sub-millisecond per document.

### RapidFuzz - typo-tolerant matching against a catalog

- **Link:** <https://github.com/rapidfuzz/RapidFuzz>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** C++-backed Python string-similarity library (MIT). process.extract / extractOne score a query against a list of choices with scorers such as fuzz.ratio, partial_ratio (substring inside a longer string), token_sort_ratio and token_set_ratio (order/duplicate-insensitive), plus a score_cutoff to bound results.

**Why it matters here.** Complements Aho-Corasick for the cases where humans in Slack/Jira write 'payment svc' or 'PaymentsAPI' instead of the canonical 'payments-api': normalize tokens from the text and fuzzy-map them to catalog entries with a threshold. Also useful for choosing which of several candidate search strings to feed a free-text MCP param when the agent trace shows the agent paraphrased.

### iocextract - regex extractor for indicators from unstructured security text

- **Link:** <https://github.com/InQuest/iocextract>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Python library/CLI that pulls URLs, IPv4/IPv6, emails, MD5/SHA1/SHA256/SHA512 hashes, YARA rules and phone numbers out of arbitrary text (reports, chat, PDFs), including 'defanged' forms like hxxp:// and example[.]com, with optional refanging. Design explicitly favours recall over precision ('flexible regex ... accepting potential false positives'), and exposes a custom-regex hook (single capture group) for user-defined indicator shapes. Related: iocsearcher, extract_iocs.

**Why it matters here.** The security community solved the same 'IDs embedded in long prose' problem years ago with pure regex plus normalization. Steal the architecture: one module per entity shape (jira key, uuid, git sha, pod name, trace id, hostname, error code), each returning normalized candidates, plus a refang-style canonicalizer (strip backticks, brackets, URL wrappers, Slack <link|text> markup) before matching.

### Grafana Loki: derived fields and the LogQL pattern parser

- **Link:** <https://grafana.com/blog/2021/08/09/new-in-loki-2.3-logql-pattern-parser-makes-it-easier-to-extract-data-from-unstructured-logs/>
- **Kind:** product
- **Relevance:** `███████░░░` 7/10

**What it is.** Two adjacent Grafana mechanisms. (1) Loki data-source 'derived fields': a per-source config of name + single-capture-group regex (e.g. "trace_id":"([a-f0-9]+)") + URL template, which turns a substring in a log line into a clickable link into Tempo - i.e. extraction rule + target-tool template. (2) LogQL `| pattern "<_> <method> <path> => <_> (HTTP/1.1 <status>)"`: a capture/literal template language where literals anchor and <_> skips; claimed an order of magnitude faster than the regexp parser and much easier to author than regex.

**Why it matters here.** Derived fields are the exact data model the user needs: {entity_name, extractor, target_tool_param_template}. The pattern-parser syntax is worth adopting as the DSL the LLM emits instead of raw regex - it is trivial to implement in-process (split on literals), far less error-prone for an LLM to write, and is what Drain templates already look like.

### FlashProfile / Microsoft PROSE (pyprose) - synthesizing syntactic profiles and FlashFill extractors from 1-4 examples

- **Link:** <https://arxiv.org/abs/1709.05725>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6/10

**What it is.** FlashProfile (OOPSLA 2018) clusters a column of strings by syntactic similarity and emits a set of regex-like patterns describing each cluster (median 0.7 s over 153 tasks). It is built on Microsoft PROSE, the programming-by-example framework behind Excel FlashFill; PROSE's Transformation.Text and Extraction.Text DSLs learn substring/extraction programs from typically 1-4 input-output examples and the learned program is saved and re-run without further learning. pyprose (pyprose.readthedocs.io) is a Python wrapper over the .NET SDK.

**Why it matters here.** Profiling: given a bag of values the agent passed as parameters in traces, FlashProfile-style clustering auto-discovers the ID shapes present (e.g. 'PROJ-1234', 'pod-abc12-xyz', 40-hex) without an LLM. Extraction: PBE gives a non-LLM alternative for learning 'substring of field X' extractors from traces. Caveat: PROSE is .NET (via pyprose), which may be too heavy; the ideas are more portable than the code.

### Mattermost autolink plugin and GitHub custom autolinks

- **Link:** <https://github.com/DHaussermann/mattermost-plugin-autolink>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Mattermost's autolink plugin config is a list of {Pattern: Go regex with (?P<name>...) named groups, Template: markdown with ${name} substitution, Scope: optional team/channel}. Examples: (MM)(-)(?P<jira_id>\d+) -> Jira browse URL; GitHub PR URL -> abbreviated link. GitHub's own custom autolinks (docs.github.com) are a simpler prefix-based scheme (e.g. 'TICKET-' + alphanumeric) with the constraint that prefixes may not overlap (TICKET vs TICK) because both would match 'TICKET123a'.

**Why it matters here.** A tiny, proven config format for 'recognize an ID in free text and route it to a system': the user's web frontend can store extractor rules as exactly this shape, with Template pointing at an MCP tool+param instead of a URL. GitHub's overlap rule is a reminder to order/longest-match ID prefixes when several projects share prefixes.

### LILAC / LUNAR / LogParser-LLM - LLM log parsers with adaptive parsing caches

- **Link:** <https://arxiv.org/abs/2310.01796>
- **Kind:** paper
- **Relevance:** `██████░░░░` 6/10

**What it is.** LILAC (FSE 2024) pairs an in-context-learning LLM parser with an adaptive parsing cache (a tree of templates) so each new log line is first matched against cached templates and the LLM is only queried on cache misses; templates are refined when inconsistencies appear. Reported speed comparable to Drain with 66.8%/69.5% better grouping/template F1 than prior baselines. LUNAR (FSE 2025) is unsupervised and stores revised templates as regexes in a template database; LogParser-LLM uses a prefix tree to suppress repeat LLM calls.

**Why it matters here.** Shows the 'LLM only on cache miss' budget model with concrete numbers; if the user is willing to allow rare LLM calls, this is the pattern (cache regexes/templates keyed by tool + template, query LLM only for never-seen shapes). Adjacent rather than direct: these target template mining, not deciding which tool to call next.

### Grok patterns: Datadog Grok Parser, pygrok/py3grok, Vector VRL parse_grok/parse_key_value

- **Link:** <https://docs.datadoghq.com/logs/log_configuration/processors/grok_parser/>
- **Kind:** product
- **Relevance:** `██████░░░░` 6/10

**What it is.** Grok is the de-facto library of named reusable sub-patterns (%{IP:client}, %{UUID}, %{HOSTNAME}, %{NUMBER}, %{WORD}, %{GREEDYDATA}) composed with %{MATCHER:extract:filter} syntax. Datadog's Grok Parser turns log text into attributes that become searchable facets (advice: <=10 rules per processor). pygrok/py3grok are pip-installable in-process implementations (use the `regex` module). Vector's VRL adds parse_key_value, parse_logfmt, parse_regex for the same job in a Rust pipeline.

**Why it matters here.** Gives you a vetted starting catalog of ID sub-patterns (UUID, HOSTNAME, IPV4, HTTPDATE, etc.) rather than writing them from scratch, and a composition syntax that is easier for an LLM to emit correctly than raw regex. Datadog's 'facets' are the conceptual equivalent of the user's extracted entities.

### RegexGenerator / RegexGenerator++ (Bartoli et al., genetic programming from examples)

- **Link:** <https://github.com/MaLeLabTs/RegexGenerator>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Java tool from the University of Trieste that evolves extraction regexes purely from labelled example matches using multi-objective GP (IEEE Computer 2014, EuroGP 2015); online demo at regex.inginf.units.it. The RASLAN 2024 comparison found it needs 8+ snippets to be reliable (exact-match F1 ~0.75-0.78) and its runtime grows with example count (up to ~40 min for the 'large' config with 128+ snippets).

**Why it matters here.** The strongest fully non-LLM regex synthesizer with runnable code; usable as a zero-LLM fallback for synthesizing extractors from trace-derived spans, but expect worse few-shot behaviour than an LLM-with-feedback loop. README is sparse on input format - inspect the ConsoleRegexTurtle CLI.

### logpai/logparser - benchmark of 13 log parsers on Loghub

- **Link:** <https://github.com/logpai/logparser>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Toolkit and benchmark (ICSE'19 'Tools and Benchmarks for Automated Log Parsing', arXiv 1811.03509) implementing Drain, Spell, IPLoM, AEL, LenMa, Logram, Brain, etc. against 16 loghub_2k datasets. Drain had the best average accuracy among the classic parsers; newer entries Brain and Tipping report ~1.00 parsing accuracy on most datasets.

**Why it matters here.** Use it to sanity-check Drain3 against alternatives (Brain, Tipping) on the user's actual logz.io samples before committing; all are pure-Python/offline. Also a source of ready ground-truth for testing extractors.

### Smore: Data Extraction via Semantic Regular Expression Synthesis

- **Link:** <https://arxiv.org/abs/2305.10401>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Extends regex with semantic 'type' holes (e.g. a city, a person) and synthesizes them from few positive/negative examples using neural sketch generation plus type-directed enumerative synthesis; claims better performance than standard regex synthesizers and neural baselines on complex extraction.

**Why it matters here.** Instructive for the 'fuzzy' part: identifiers like 'service name' or 'team' are semantic categories rather than syntactic shapes. In the user's setting the semantic hole can be filled by gazetteer matching (Aho-Corasick over the service catalog) instead of a neural classifier, giving a Smore-like combined syntactic+dictionary extractor with no runtime ML.

### spaCy rule-based matching: PhraseMatcher and EntityRuler

- **Link:** <https://spacy.io/usage/rule-based-matching>
- **Kind:** spec
- **Relevance:** `█████░░░░░` 5/10

**What it is.** spaCy can run purely rule-based NER: PhraseMatcher does exact (optionally lower-cased) matching of large terminology lists; Matcher supports token-attribute patterns with operators and regex; EntityRuler wraps both to write entities into Doc.ents and can run with a blank pipeline (no statistical model). Patterns are JSONL and can be serialized to disk.

**Why it matters here.** An option if the user wants tokenization-aware rules (e.g. 'the X service' context patterns) rather than raw regex, at the cost of a heavier dependency. For pure ID/gazetteer extraction Aho-Corasick + regex is lighter and faster.

### lnav log format definitions

- **Link:** <https://docs.lnav.org/en/latest/formats.html>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** lnav describes log formats as JSON: a PCRE2 regex with named captures, per-field types (string/integer/float/json/quoted) and flags such as identifier:true (colour as an ID) and hidden. Formats are auto-detected by trying each regex against the first lines of a file.

**Why it matters here.** A good concrete schema for a local, file-based extractor registry (regex + typed captures + 'this capture is an identifier' flag + auto-detection by trial matching). Zero server, well-worn by years of use.

### Reptile: converting RegExes for log parsing into Dynatrace Pattern Language with LLM assistance

- **Link:** <https://arxiv.org/abs/2506.19539>
- **Kind:** paper
- **Relevance:** `████░░░░░░` 4/10

**What it is.** Rule-based regex-to-DPL translator with GPT-4 used only to optimize hard cases; 73.7% of 946 enterprise regexes converted safely, F1/MCC > 0.91 on the LLM-optimized subset, best-effort on the rest.

**Why it matters here.** Adjacent evidence that 'deterministic rules first, LLM only for residual cases, validated on real samples' is a workable split for pattern languages; also lists real-world regex categories an enterprise actually uses for log extraction.

### tree-sitter-log (Tudyx) - generic tree-sitter grammar for log files

- **Link:** <https://github.com/Tudyx/tree-sitter-log>
- **Kind:** oss-project
- **Relevance:** `███░░░░░░░` 3/10

**What it is.** A tree-sitter grammar aiming to be general enough for most log files, integrated into the Helix editor for highlighting. README lists 'add url parsing' as TODO and node coverage is undocumented; it is positioned as a highlighter rather than an extractor.

**Why it matters here.** Checked because the angle asked about tree-sitter for logs: it is not a practical extraction tool today. Writing a custom grammar would be more work than a regex/pattern catalog and gives little over Drain templates for free-form Slack/wiki text.

## Dead ends

_Queries and directions that produced nothing useful. Listed so nobody repeats them._

- site:news.ycombinator.com queries for 'extract identifiers from unstructured logs without ML' returned only generic structured-logging threads; the one relevant thread (item 21590794) just recommends goaccess/lnav/angle-grinder/awk.
- commonregex / regex-patterns libraries only cover PII-style shapes (email, phone, URL, IP, date); no maintained library was found that packages ops-specific ID regexes (Jira keys, k8s pod names, git SHAs, trace ids) - these will need to be hand-written or LLM-generated, seeded from grok's pattern library.
- tree-sitter grammars for logs exist only as syntax highlighters (Tudyx/tree-sitter-log, AndroidIDE logcat); no evidence of anyone using tree-sitter for identifier extraction.
- LILAC's README does not document the parsing cache internals or LLM-call counts; the numbers are only in the paper, and the cache is template-level, not tool-routing.
- RegexGenerator README lacks input format, example counts and maintenance status; the project appears dormant (2015-2016 papers).
- Smore (semantic regex synthesis) needs a neural sketch generator at synthesis time and its runtime semantic matchers are model-backed; code availability was not confirmed.
- Splunk automatic key=value extraction (KV_MODE) and Datadog facets are product features tied to their servers; only the concept (auto k=v discovery, grok processors) transfers.
- Microsoft PROSE/FlashFill is .NET-only (pyprose is a wrapper over the SDK); FlashProfile code availability was not confirmed from the abstract.
- The 'From IOCs to Regex' paper (arXiv 2604.12228) is LLM-generated regex for threat reports with 99.1% hit rate / 0.8% FP, but the abstract does not say whether code is released; treated as supporting evidence rather than a tool.
