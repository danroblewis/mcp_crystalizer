# Local fuzzy and semantic search with no extra server

_Research angle `local-fuzzy-search`. Generated from the research workflow run on 2026-09-09._

## Angle verdict

Yes, this angle yields a real, reusable pattern; call it the "Local Hybrid Tool-Result Index": every MCP result is normalized into one SQLite row, indexed three ways in the same file (an ID-preserving unicode61-tokenchars FTS5 index for BM25, a trigram FTS5 index for partial-ID substring lookup, and a sqlite-vec vec0 table of Model2Vec static embeddings partitioned by source), and queried with one parametrized RRF statement whose weights and hand-written rerank rules are fitted from recorded agent traces. It satisfies the hard constraint outright (stdlib sqlite3 or sqlite-wasm, no servers) and covers the specific pain points: trigram + highlight()/snippet() localize a partial pod name, commit prefix or ticket key inside long log lines and slack messages so extractors regex a small span rather than the whole blob; BM25 with column weights and column filters reproduces the search strings the agent typed into slack/confluence/jira; static embeddings give paraphrase-level matching between a ticket's wording and how the incident was described elsewhere at negligible CPU cost. The Semble ablation is the most decision-relevant evidence: domain-tuned reranking heuristics on top of BM25 delivered +0.16 NDCG while the vector leg added only +0.02 more, so the one-time LLM budget should go into having it write rerank rules and extractors from traces, with embeddings as a cheap add-on rather than the centerpiece. Boundaries of the angle: it replaces the agent's "which candidate / which substring" judgement, not the "which tool next" planning, which needs a separate graph/traversal layer that consumes this index's ranked output; FTS5 full scans and brute-force KNN are fine at corpus scale (tens to low hundreds of thousands of items) but not beyond; and the browser story is workable (sqlite-wasm + statically linked sqlite-vec + a hand-ported Model2Vec lookup) but not off-the-shelf.

## Deep reads

### SQLite FTS5 (bm25(), trigram tokenizer, unicode61 tokenchars, external-content tables, highlight/snippet)

- **Link:** <https://www.sqlite.org/fts5.html>
- **Evidence quality:** spec

**Key techniques**

- Trigram tokenizer: `CREATE VIRTUAL TABLE tri USING fts5(a, tokenize="trigram")` turns FTS5 into an indexed substring matcher. Any MATCH of >=3 unicode chars hits; queries under 3 chars match nothing. Case-insensitive by default (`case_sensitive 1` to change); it also makes `WHERE a LIKE '%cdefg%'` and `GLOB '*ij klm*xyz'` use the index (LIKE with ESCAPE is not optimized).
- unicode61 `tokenchars` option keeps identifiers whole for the BM25 index: `tokenize = "unicode61 tokenchars '-_'"` so JIRA-1234, pod-name-abc, snake_case survive as single tokens; the inverse `separators` option exists too. Default token classes are unicode L*, N*, Co.
- bm25(): `ORDER BY bm25(tbl, w_col1, w_col2, ...)` with hard-coded k1=1.2, b=0.75 and IDF = log((N-n+0.5)/(n+0.5)); the arguments are per-column weights (e.g. title 5x, sender 10x body). Unspecified columns get weight 1.0.
- External-content tables: `CREATE VIRTUAL TABLE fts_idx USING fts5(b, c, content='t1', content_rowid='a')` indexes rows that live in an ordinary table, kept in sync via three AFTER INSERT/DELETE/UPDATE triggers (the delete is `INSERT INTO fts_idx(fts_idx, rowid, b, c) VALUES('delete', old.a, old.b, old.c)`). `INSERT INTO ft(ft) VALUES('rebuild')` re-derives the index if it drifts. This lets one canonical row table carry several differently-tokenized FTS indexes.
- Index-size knobs: `prefix='2 3'` adds separate prefix indexes to speed `abc*`; `detail=column` / `detail=none` cut a 743 MiB index to 340 / 134 MiB but disable phrase+NEAR (column) and column filters (none). With trigram plus detail=column/none, query tokens longer than 3 chars are disallowed.
- Query language: implicit AND, `OR`, `NOT` (precedence NOT > AND > OR), phrases `"a b"`, prefix `thr*` (only outside quotes), initial-token `^one`, `NEAR("a b" "c d", 10)`, column filters `col:phrase`, `{c1 c2}:phrase`, `-col:phrase`.
- Auxiliary functions for extraction: `highlight(ft, col, open, close)` marks every matched phrase in a column; `snippet(ft, col, open, close, ellipsis, max_tokens<=64)` picks the fragment maximizing distinct matched terms, preferring starts of values or text after '.' / ':'.
- Maintenance: `'optimize'` merges b-trees into one; incremental `'merge', -500` then `'merge', 500`; `'secure-delete'` purges deleted rows' postings (locks the DB to FTS5 >= 3.42).

**How to apply it.** This is the storage substrate for the whole plan. Normalize every MCP result (a jira issue, one slack message, one confluence page section, one logz.io line, one pagerduty incident, one file chunk from read/grep) into a plain `items(id, source, ts, ref, title, body)` row; hang two external-content FTS5 indexes off it: `items_fts_bm25` with `unicode61 tokenchars '-_./:@'` for ID-preserving ranked search, and `items_fts_tri` with the trigram tokenizer for 'find the slack message/log line that contains this partial pod name / commit prefix / ticket key' without regexing every string. bm25 column weights (title > body) and column filters (`source:slack title:...`) let the query generator the LLM writes from traces express what the agent did when it typed a search string into slack_search or confluence_search. highlight()/snippet() output is exactly what the deterministic extractors should run their ID regexes on, since it localizes the matched region inside long messages/wiki pages. Zero dependencies: Python stdlib sqlite3, or sqlite-wasm in the browser.

**Limitations.** FTS5 always scans the whole FTS index for a MATCH; there is no metadata pre-filter inside the index (you filter in the outer SQL, fine at tens/hundreds of thousands of rows). Trigram needs >=3 characters and is case-insensitive by default (turn on case_sensitive for hashes if collisions matter). Trigram indexes are large; detail=column/none is incompatible with long trigram query tokens. Not a substring matcher for 1-2 char IDs. Sync triggers are the user's responsibility; drift silently produces odd results until 'rebuild'.

**Quotes**

> Substrings consisting of fewer than 3 unicode characters do not match any rows when used with a full-text query.

> In one test... the FTS index was 743 MiB on disk with detail=full, 340 MiB with detail=column and 134 MiB with detail=none.

> The fragment of text is selected so as to maximize the number of distinct queried terms it contains. Higher weight is given to snippets that occur at the start of a column value, or that immediately follow "." or ":" characters in the text.

### sqlite-vec: brute-force KNN in SQLite (vec0 virtual tables, binary quantization, metadata/partition columns, WASM)

- **Link:** <https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html>
- **Evidence quality:** production-proven

**Key techniques**

- vec0 virtual table: `create virtual table vec_articles using vec0(article_id integer primary key, headline_embedding float[384])`; insert vectors as JSON text or blobs; KNN via `where headline_embedding match ? and k = 20` returning rowid + distance. Types: float[N], int8[N], bit[N]. Also 'static' functions (vec_distance_cosine/L2 over a blob column) usable without the virtual table.
- Brute force only, but fast enough at corpus scale: SIFT1M (1M x 128-d, k=20) query 17ms static / 33ms vec0 vs Faiss 10ms vs numpy 136ms; GIST1M (500K x 960-d) 41ms vs Faiss 50ms. On disk, 100K float 3072-d vectors take 214ms/query; bit-quantized 11ms. 1M x 3072-d float is 8.5s, i.e. past the author's 100ms target.
- Binary quantization: `vec_quantize_binary()` maps <=0 -> 0, >0 -> 1, packs to bit[N]; 32x smaller (1M x 1536-d: 6.1 GB -> 192 MB) with ~5-10% quality loss and ~10x faster queries; Hamming distance on bit vectors. Matryoshka truncation: `vec_normalize(vec_slice(embedding, 0, 512))`.
- Since v0.1.6 (features/vec0 docs): metadata columns (TEXT/INTEGER/FLOAT/BOOLEAN, max 16) filterable with =, !=, <, <=, >, >= inside the KNN query (`and genre = 'scifi' and mean_rating > 3.5`); partition key columns (`user_id integer partition key`, max 4) physically collocate vectors so `=` constraints pre-filter (aim for hundreds of vectors per partition value, over-sharding slows KNN); auxiliary `+contents text` columns (max 16) store unindexed payload to avoid joins.
- Distribution: pip/npm/gem/cargo/go; runs in the browser only by static compilation into a SQLite WASM build (`sqlite-vec-wasm-demo` on npm, load via `sqlite3.oo1.DB`), because 'It's not possibly to dynamically load a SQLite extension into a WASM build of SQLite.' Sister projects sqlite-lembed (local GGUF embedding) and sqlite-rembed (remote) supply embeddings in SQL.

**How to apply it.** Puts embeddings in the same .db as the FTS5 indexes so 'lexical + semantic' is one SQL statement and no vector service exists. For this problem the vectors are cheap static Model2Vec embeddings of each cached item; use `source text` as a partition key (or metadata column) so 'nearest slack messages to this jira title' or 'nearest confluence sections to this alert name' prefilter by source without scanning everything. At the expected on-call corpus size (thousands to low hundreds of thousands of items, 256-d) brute-force float search is well under 100ms; bit-quantize if it grows. Helps the slack, confluence, jira, pagerduty and google-drive legs (paraphrase-level matching between a ticket's wording and how people described the same incident elsewhere). For a browser frontend it works via the statically compiled wasm build.

**Limitations.** No ANN index, so latency scales linearly with rows x dims; the author's own numbers show float search past ~100K rows at high dims misses a 100ms budget. Vector hits return only a distance, not why they matched (no highlight() analogue), so deterministic extractors cannot localize the matched span from the vector leg alone. Metadata filtering supports only equality/range operators (no LIKE/IS NULL). The WASM package is explicitly a demo that does not follow semver.

**Quotes**

> Only brute-force vector search

> every element `<=0` to `0` and `>0` to `1`

> It's not possibly to dynamically load a SQLite extension into a WASM build of SQLite.

### Hybrid full-text + vector search in pure SQL (keyword-first, Reciprocal Rank Fusion, re-rank-by-semantics)

- **Link:** <https://alexgarcia.xyz/blog/2024/sqlite-vec-hybrid-search/index.html>
- **Evidence quality:** production-proven

**Key techniques**

- Schema: an ordinary `articles` table; an external-content FTS5 table `fts_articles using fts5(headline, content='articles', content_rowid='id')`; a `vec_articles using vec0(article_id integer primary key, headline_embedding float[768])` table. Both keyed by the same rowid so they join.
- Recipe 1, keyword-first: two CTEs, `fts_matches` (row_number() over (order by rank)) and `vec_matches` (row_number() over (order by distance), `k = :k`), `union all` with a match_type column so exact keyword hits are listed first and semantic hits fill in.
- Recipe 2, RRF in SQL: `fts_matches full outer join vec_matches on article_id`, score = `coalesce(1.0/(:rrf_k + fts_rank), 0.0) * :weight_fts + coalesce(1.0/(:rrf_k + vec_rank), 0.0) * :weight_vec`, `order by combined_rank desc`; defaults rrf_k=60, both weights 1.0. Weights are the tunable knob to favor one leg.
- Recipe 3, re-rank by semantics: take the top-k FTS5 hits, then `order by vec_distance_cosine(lembed(:query), lembed(articles.headline))`, i.e. lexical recall then semantic ordering, no vec0 table needed.
- Stated rough edges: FTS5 does a full search over the dataset each time with no metadata filtering inside the index; FTS5 query operators (phrases, NEAR, booleans) have no vector analogue so mixed queries get awkward; vector hits have no highlight().
- Corroborating field report (HN 47203790, user blakec): 15,800-file Obsidian vault -> 49,746 chunks in an 83 MB SQLite DB using potion-base-8M (256-d) + sqlite-vec + FTS5 BM25 + RRF; `--incremental` hashes chunk content and re-embeds only changed chunks (full reindex ~4 min, daily incremental <10 s); notes that BM25 IDF automatically down-weights JSON keys like id/status/type that appear in every chunk of tool output.

**How to apply it.** This is the concrete replacement for the agent's judgement of 'which of these 40 search hits is the one I want': a single parametrized SQL statement over the cached items table. The trace-driven step becomes: for each recorded agent search (slack_search 'checkout 500s us-east', confluence_search 'payment retry runbook'), replay it against the local index with the RRF query and fit :weight_fts, :weight_vec, :rrf_k and bm25 column weights so the item the agent actually clicked/used next ranks first. Recipe 3 (FTS recall, cosine re-rank) is the cheapest variant and needs no vec0 table. The blakec report is almost the same workload (mixed JSON + prose tool output, identifiers needing exact match) and shows the hash-based incremental reindex pattern to reuse for a tool-result cache that grows on every MCP call.

**Limitations.** Demo corpus is 14.5K short headlines, so none of the performance claims cover long slack threads or log lines; chunking of long documents is left to the reader. Weights and rrf_k are set by hand, no learning procedure. Requires a `lembed()`-style embedding function; substituting Model2Vec means embedding in the host language, not SQL. The blakec datapoint is a single HN comment, not a published system.

**Quotes**

> FTS5 tables perform a full search across the entire dataset everytime, there's no way of provided metadata filtering or indexing on a single FTS5 index.

> coalesce(1.0 / (:rrf_k + fts_matches.rank_number), 0.0) * :weight_fts + coalesce(1.0 / (:rrf_k + vec_matches.rank_number), 0.0) * :weight_vec

> Stack is Model2Vec (potion-base-8M, 256-dimensional embeddings) + sqlite-vec for vector search + FTS5 for BM25, combined via Reciprocal Rank Fusion. The database is 49,746 chunks in 83MB.

### Model2Vec / potion static embeddings: distill a sentence transformer into a token lookup table (HF blog + minish.ai docs; GitHub README is thin on mechanism)

- **Link:** <https://huggingface.co/blog/Pringled/model2vec>
- **Evidence quality:** production-proven

**Key techniques**

- Distillation, no dataset needed: run every token of the base model's vocabulary (~32k subwords) through the sentence transformer individually and store the output embedding; result is a |V| x d matrix. 'Output mode' takes ~30 s on CPU and yields a ~30 MB model; alternative modes distill a custom word vocabulary (domain terms) with subword fallback.
- PCA on the matrix reduces dimensionality and, counterintuitively, improves quality (~+2.8% in ablation) because it recenters/whitens the space; then Smooth Inverse Frequency weighting w = 1e-3 / (1e-3 + p) where p is approximated by Zipf's law over the tokenizer's frequency-sorted vocabulary (no corpus needed, ~+3.1%). Optional int8/f16 quantization and Tokenlearn pre-training (used for the potion models).
- Inference is tokenize -> lookup rows -> mean -> normalize; no attention, so ~500x faster than the source transformer and 15x smaller. model2vec-rs does 8000 samples/s single-thread and exposes `from_bytes(tokenizer.json, model.safetensors, config.json)` behind a `wasm` feature (built with `--no-default-features --features wasm --target wasm32-unknown-unknown`).
- Quality: M2V_base_output MTEB average 46.79 vs all-MiniLM-L6-v2 56.08 (classification 82.23 vs 84.10); beats GloVe/BPEmb on every task. potion-base-8M is 7.5M params / ~8 MB (256-d); potion-base-32M 32.3M params; potion-retrieval-32M for retrieval; potion-code-16M-v2 for code; potion-multilingual-128M.
- Only hard dependency is numpy; `StaticModel.from_pretrained('minishlab/potion-base-32M').encode([...])`; `distill(model_name='BAAI/bge-base-en-v1.5')` produces your own.

**How to apply it.** The embedding source for the vec0 leg at effectively zero CPU cost, so every MCP result can be embedded on ingest and every query embedded per keystroke, in-process (Python numpy, or Rust/wasm, or a ~50-line JS port: load tokenizer.json + a Float32 matrix, mean-pool). Because distillation is dataset-free and 30 s, the one-time LLM/engineering budget can go into distilling with a custom word vocabulary that includes the company's service names, alert names and jira components so those tokens get their own rows instead of being shattered into subwords. Most useful on the slack, confluence, jira, google-drive and pagerduty legs (paraphrase matching); potion-code-16M for the codebase leg. Not needed for the logz/chronosphere legs, where exact identifiers dominate.

**Limitations.** Bag-of-tokens: no word order, no negation, quality well below MiniLM (47 vs 56 MTEB avg), so it must be paired with BM25, never used alone. English-centric unless you take the 128M multilingual model. No official JS/npm package; browser use means a self-built wasm or a hand port. The GitHub README contains almost none of the mechanism; it lives in the HF blog and minish.ai docs.

**Quotes**

> for each of the 32k input tokens in a sentence transformer vocabulary, we do a forward pass, and then store the resulting embedding.

> reducing the dimensionality actually increased performance significantly. We think this is because PCA also normalizes the resulting space, in the sense of removing biases in the original vector space.

> When filesystem access is unavailable, use from_bytes instead of from_pretrained

### Semble (MinishLab): CPU-only hybrid code search for agents, with the ablation showing hand-written reranking signals beat adding vectors

- **Link:** <https://github.com/MinishLab/semble>
- **Evidence quality:** research-prototype

**Key techniques**

- Pipeline: tree-sitter code-aware chunking -> two retrievers (BM25 over identifiers/API names; potion-code-16M-v2 static embeddings) -> Reciprocal Rank Fusion -> code-aware reranking. Exposed as an MCP server with two tools: `search(query)` and `find_related(file, line)`.
- Reranking signals (the part that carries the result): adaptive weighting (symbol-shaped queries like `Foo::bar`, `_private`, `getUserById` get more lexical weight; prose queries stay balanced); definition boost (a chunk that defines the queried symbol outranks chunks that reference it); identifier stems (query `parse config` boosts chunks containing `parseConfig`, `ConfigParser`); file-coherence and noise penalties (tests, legacy shims, .d.ts stubs downranked).
- Ablation (benchmarks/README): BM25 raw 0.675 -> with ranking 0.834; potion-code raw 0.650 -> 0.821; BM25 + potion + ranking 0.854. Baselines: CodeRankEmbed hybrid 0.839-0.862 (137M transformer), ColGREP 0.693, ripgrep 0.126. Index 344 ms vs 116 s; query p50 0.91 ms vs 16 ms. So the reranking heuristics add ~+0.16 NDCG while the vector leg adds ~+0.02 on top of reranked BM25.
- Incremental indexing by walking the tree and comparing mtimes; model downloaded once to the HF cache; token-cost framing: 348 expected tokens per lookup vs 45,587 for ripgrep + read.
- HN critiques: 'the models are so heavily RL'd with grep that they do not trust results in other forms and will continually retry or reread'; end-to-end test with gpt-5.4 showed near-parity in context use (9.8% vs 10.9%) and slightly higher cost; 'extensive benchmarks testing the wrong thing' (retrieval NDCG is not task completion).

**How to apply it.** Directly the codebase leg (read/glob/grep/ast-grep): chunk by tree-sitter, index BM25 + potion-code into the same SQLite as the other sources, and rank. More important for the overall plan is the ablation: the reusable lesson is that a handful of cheap, explicit rerank rules tuned to the domain (exact-identifier boost, 'defines vs mentions', noise penalties) recover most of what the agent's judgement provides, and they are exactly the kind of deterministic code an LLM can write once from recorded traces (e.g. 'boost slack messages in #incidents-*', 'boost log lines whose service field equals the ticket component', 'penalize confluence pages last edited >2 years ago'). The agent-distrust critique disappears in the no-LLM-in-the-loop design, so the retrieval numbers apply directly.

**Limitations.** Benchmark queries and relevance labels are generated and judged by Claude Sonnet 4.6, so absolute NDCG numbers are soft. BM25 tokenization details, RRF k and weights, and the storage format are not documented in the README; you would read the code to copy them. Code-specific: the rerank signals are written for source files and must be re-derived for slack/log/wiki items. Single-vendor project from the same team that makes Model2Vec.

**Quotes**

> Symbol-like queries (`Foo::bar`, `_private`, `getUserById`) get more lexical weight

> A chunk that defines the queried symbol is ranked above chunks that merely reference it

> the models are so heavily RL'd with grep that they do not trust results in other forms and will continually retry or reread

## All sweep findings

_16 findings, sorted by relevance score._

### SQLite FTS5 (bm25(), trigram tokenizer, prefix indexes, external-content tables)

- **Link:** <https://www.sqlite.org/fts5.html>
- **Kind:** spec
- **Relevance:** `██████████` 10/10

**What it is.** SQLite's built-in full-text engine. bm25() ranking with per-column weights (k1=1.2, b=0.75 hard-coded); the 'trigram' tokenizer (SQLite >= 3.34) turns FTS5 into an indexed substring matcher (any >=3-char substring, case-insensitive by default, and it also accelerates LIKE '%x%' / GLOB); prefix='2 3' indexes accelerate 'abc*' queries; 'detail=column/none' cut index size ~54% / ~82% at the cost of phrase/NEAR queries; external-content tables let you index rows that live in a normal table (kept in sync by triggers). Runs in Python's stdlib sqlite3 and in browser WASM builds.

**Why it matters here.** The natural local store for cached MCP tool results (tickets, slack messages, wiki pages, log lines). Trigram tokenizer directly answers 'find the message/log line containing this partial ID or substring' without regex over everything; bm25 with column weights answers 'rank candidate documents for the next query'. Zero dependencies, single file, satisfies the no-server constraint. Caveats: trigram needs >=3 chars; no metadata filtering inside FTS5 (filter in the outer SQL).

### sqlite-vec: brute-force vector search extension for SQLite (runs in WASM)

- **Link:** <https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html>
- **Kind:** oss-project
- **Relevance:** `█████████░` 9/10

**What it is.** SQLite extension adding vec0 virtual tables for KNN search; installs via pip/npm/cargo/gem/Go; statically compiles into SQLite WASM for browsers (~1.5MB build, see alexgarcia.xyz/sqlite-vec/wasm.html; dynamic loading is impossible in WASM). Brute-force only (no ANN yet); author reports 1M x 128-dim in 17ms vs Faiss 10ms; binary quantization (32x smaller) and Matryoshka truncation supported; float vectors above ~100K rows miss a 100ms budget, bit-quantized ones do not.

**Why it matters here.** Lets embeddings live in the same .db as FTS5 so hybrid search is one SQL statement. At the scale of a corporate on-call corpus (thousands to low hundreds of thousands of items), brute force is fine and avoids running any vector service. Pair with a static embedding model (Model2Vec) so embedding is CPU-cheap and one-time.

### Hybrid full-text + vector search with SQLite (keyword-first, RRF, re-rank-by-semantics)

- **Link:** <https://alexgarcia.xyz/blog/2024/sqlite-vec-hybrid-search/index.html>
- **Kind:** blog
- **Relevance:** `█████████░` 9/10

**What it is.** Pure-SQL recipes combining FTS5 and sqlite-vec via CTEs: keyword-first (exact matches first, then semantic fill), Reciprocal Rank Fusion 1/(k+rank) with tunable per-source weights, and 'FTS then re-order by vector distance'. Demo: 14.5K news headlines, 4.3MB text, 768-dim Snowflake Arctic embeddings via sqlite-lembed. Caveat noted: FTS5 always scans the whole table; no metadata pre-filtering. Simon Willison's write-up and the HN thread (news.ycombinator.com/item?id=47203790) report the same stack (FTS5 + sqlite-vec + potion-base-8M 256-dim + RRF) used specifically for indexing agent tool outputs, with hash-based incremental reindexing.

**Why it matters here.** This is the concrete 'flexibility and fuzziness' replacement: the LLM's judgement of 'which candidate is the right one' becomes RRF over BM25 + cosine, tunable with weights learned from recorded traces. The HN comment is an almost identical use case (tool outputs mixing JSON/config with prose, identifiers needing exact match, error context needing semantic match).

### Model2Vec / potion static embeddings (Python, Rust, WASM feature)

- **Link:** <https://github.com/MinishLab/model2vec>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** Distills any sentence-transformer into a static token-embedding lookup table (tokenize -> index -> mean -> normalize; no attention). Models 8MB-30MB; ~500x faster than the source transformer on CPU; potion-base-32M reported at ~92% of MiniLM quality (HN thread); only dependency is numpy; can distill your own in ~30s on CPU. model2vec-rs adds a 'wasm' feature for from_bytes() loading (8000 samples/s single-thread) but there is no official npm package - browser use requires a self-built WASM or implementing the lookup in JS (trivial: tokenizer + matrix). HF's parallel 'static-retrieval-mrl-en-v1' (1024-dim, Matryoshka-truncatable, 87.4% of all-mpnet on NanoBEIR, 100-400x faster on CPU) is the same idea.

**Why it matters here.** Cheapest way to get 'semantic-ish' matching of a jira title to slack messages or wiki pages at zero LLM cost and negligible CPU; embedding cost is a matrix lookup so you can embed every tool result on the fly. Quality is below MiniLM but well above pure lexical for paraphrased descriptions. Best browser story is to port the lookup to JS yourself (tokenizer + Float32 matrix ~8-30MB).

### Semble: code search MCP server using Model2Vec + BM25 + RRF (Show HN)

- **Link:** <https://news.ycombinator.com/item?id=48169874>
- **Kind:** forum
- **Relevance:** `████████░░` 8/10

**What it is.** MinishLab's open-source code search for agents: potion-code-16M static embeddings + BM25 fused with RRF and reranked with code-aware signals so exact identifier matches win; ~250ms to index a typical repo, ~1.5ms/query on CPU, 0.854 NDCG@10 (~99% of a transformer). Thread critiques: agents (Claude Code) distrust non-grep results and re-run ripgrep, sometimes using more tokens end-to-end; retrieval benchmarks do not equal task completion.

**Why it matters here.** A worked example of exactly this stack applied to the codebase leg of the user's chain (read/glob/grep/ast-grep), and evidence that CPU-only hybrid retrieval is fast enough for interactive use. The critique is instructive: with NO LLM in the loop the 'agent distrust' problem disappears, so the retrieval quality numbers matter directly.

### Drain3: streaming log template miner with parameter extraction

- **Link:** <https://github.com/logpai/Drain3>
- **Kind:** oss-project
- **Relevance:** `████████░░` 8/10

**What it is.** Production implementation of the Drain algorithm: fixed-depth parse tree clusters log lines into templates with <*> wildcards; regex masking pre-pass (IP, hex, int, custom) names variable slots; extract_parameters() returns the ordered variable values with their mask names for any line matching a template; persistence to file/redis/kafka; max_clusters LRU bound. Pure Python.

**Why it matters here.** Direct answer to 'IDs of interest are embedded in long unstructured log lines': mine templates from logz.io results once, then deterministically extract the variable parts (request IDs, hostnames, trace IDs) to feed into the next tool call. Also usable on Slack messages/alert text that follow templated formats (pagerduty, chronosphere alerts). This is the extractor an LLM would otherwise write ad hoc.

### TraceCompiler: compiling noisy agent traces into deterministic workflows

- **Link:** <https://arxiv.org/html/2608.02680>
- **Kind:** paper
- **Relevance:** `████████░░` 8/10

**What it is.** Clusters agent traces by intent (sentence-embedding cosine, avg linkage), then compiles them into executable workflows by proving data flow rather than trusting adjacency. Every tool-argument binding is classified as constant / user input / copied from a prior output / deterministic transformation / LLM decision; edges are kept only with evidence tuples, and LLM nodes are emitted only for bindings evidence cannot resolve. Results: 34->11 calls on a sample intent; 0.928 precision / 0.943 recall recovering dependencies over 15,775 edges; refuses to compile under-determined side-effecting steps.

**Why it matters here.** Adjacent to this angle but the most precise formalization of the user's plan (record traces -> derive extractors/query generators -> run without LLM). The five-way binding taxonomy is a ready design for the extractor layer: 'copied from prior output' and 'deterministic transformation' bindings are where the fuzzy-substring extractors from this angle plug in; 'LLM decision' bindings are the residual minimal-LLM use. AWO (arxiv 2601.22037) is a lighter cousin: greedy extraction of frequent tool-call chains into parameterized meta-tools (5.6-11.9% fewer LLM calls).

### Keyqueries: formulating a search query from a document so the document ranks top

- **Link:** <https://downloads.webis.de/publications/papers/hagen_2016b.pdf>
- **Kind:** paper
- **Relevance:** `███████░░░` 7/10

**What it is.** Gollub/Hagen et al. (Bauhaus Weimar): a keyquery for document d is a minimal keyword combination that returns d in the top-k of a reference search engine; generated by extracting keyphrases from d and enumerating small term subsets (with covering/minimality properties) against the engine. Used to find related papers on a 200K-paper corpus; on par with citation-graph and Google Scholar 'related' baselines, and complementary to them.

**Why it matters here.** A principled, non-LLM way to approximate 'the agent picks a good search string for tool X given the content of result Y': treat each target tool (slack search, confluence search, jira JQL text) as the reference engine, and learn from recorded traces which keyphrase subsets of the source document actually retrieve the item the agent chose. Directly converts trace data into per-tool query templates with verifiable retrieval behavior.

### YAKE: unsupervised single-document keyword extraction

- **Link:** <https://github.com/LIAAD/yake>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Statistical keyphrase extractor (ECIR'18 best short paper) using term position, frequency, casing, context spread; no training, corpus, or dictionaries; language-agnostic; configurable n-gram length, dedup (Levenshtein/Jaro/seqm) and window. Surveys rate it the balanced choice among RAKE/YAKE/KeyBERT: KeyBERT is most accurate but needs a transformer; RAKE is fast but leaves stopword-laden n-grams; YAKE sometimes emits duplicates.

**Why it matters here.** Cheap first-pass generator of candidate search terms from a jira ticket or slack thread to feed free-text search params (slack search, confluence CQL text, logz query). Combine with a corpus-level IDF computed over the user's own cached tool results and with an identifier regex pass (YAKE will not prefer 'svc-payments-7f3a' over 'customer').

### FlashText / Aho-Corasick gazetteer matching (Python, Rust; ahocorasick_rs, daachorse)

- **Link:** <https://github.com/vi3k6i5/flashtext>
- **Kind:** oss-project
- **Relevance:** `███████░░░` 7/10

**What it is.** Trie/Aho-Corasick keyword extractor: add thousands of keyword->canonical-name pairs and extract all occurrences in one linear pass, orders of magnitude faster than an alternation regex at large vocab sizes. Whole-word, exact (case configurable), no fuzzy, no patterns. Faster reimplementations: flashtext2 (Rust, 3-10x), blitztext, G-Research ahocorasick_rs, BurntSushi aho-corasick (Rust), daachorse (double-array).

**Why it matters here.** The known-entity half of extraction: once you have enumerated service names, host names, team names, dashboard names, jira project keys, etc. from earlier tool results, a gazetteer pass finds every mention inside slack/wiki/log text and maps aliases to canonical IDs deterministically. Complements regex (unknown IDs by shape) and Drain (IDs by log slot).

### RapidFuzz (Python) / rapidfuzz-rs: fast Levenshtein, Jaro-Winkler, token-sort ratios

- **Link:** <https://github.com/rapidfuzz/RapidFuzz>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** C++/Rust fuzzy-string library with bit-parallel Levenshtein and Jaro-Winkler, process.extract/cdist for one-to-many matching, and token_sort/token_set ratios for word-order-insensitive comparison. Widely reported as the fastest general-purpose option; Jaro-Winkler recommended for short identifiers, token ratios for multi-word titles.

**Why it matters here.** For reconciling near-identical identifiers across tools (a service called 'payments-api' in jira, 'payments_api' in chronosphere labels, 'PaymentsAPI' in confluence) and for de-duplicating candidate IDs before fan-out. Runs in-process; no browser build, but the same metrics are trivial to reimplement in JS for small candidate sets.

### lucivy: tantivy fork with substring-across-token-boundaries BM25 search (Python, Node, WASM)

- **Link:** <https://github.com/L-Defraiteur/lucivy>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Fork of tantivy 0.22 that replaces the query layer with a suffix-FST engine: finds 'mutex' inside 'pthread_mutex_lock', matches 'spin_lock' against 'spin lock'/'spinlock'/'spin-lock' via relaxed separators, Levenshtein/Jaro-Winkler fuzzy, regex over literal-selected candidates, exact byte-offset highlights. Linux-kernel benchmark (94K files, 857MB): substring 18ms, fuzzy 44ms, but index is 5.8x the text size. Bindings for Python, Node, browser WASM, Rust, C++. Upstream tantivy (quickwit-oss/tantivy, tantivy-py) is the mature Lucene-like alternative with fuzzy/regex queries but token-level matching only.

**Why it matters here.** Purpose-built for identifier-heavy corpora where the interesting substring sits inside a bigger token (log lines, metric names, code). More capable than FTS5 trigram (fuzzy + separator relaxation + highlights give you the exact span to extract) at the cost of a young single-maintainer codebase and a large index. Good candidate for the codebase/log legs; FTS5 trigram is the safer default.

### FlexSearch / MiniSearch / Orama / Fuse.js: pure-JS in-browser indexes

- **Link:** <https://github.com/nextapps-de/flexsearch>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** FlexSearch: tokenizer modes strict/forward/reverse/full (memory factor 1x to n(n-1)), contextual index (Gulliver's Travels index grows 2.9MB->21.5MB at depth 2), phonetic encoders (up to ~70% index compression with more fuzziness), persistence to IndexedDB/SQLite/Postgres, worker support; fastest of the group. MiniSearch: zero-dep, BM25-style ranking, fuzzy + prefix + field boosting + auto-suggest, serializable JSON index, in-memory only. Orama: <2KB core, BM25, typo tolerance, facets, vector and hybrid search, plugins for persistence; runs in browser/Node/Deno/edge. Fuse.js: ~4KB, Bitap fuzzy, fine under ~10K items, slow beyond.

**Why it matters here.** If the web frontend does the fuzzy matching client-side (the browser as the 'graph query engine'), these are the options: MiniSearch or Orama for ranked lexical search over a few thousand cached tool results, FlexSearch 'forward'/'full' tokenizer or Orama vectors when you need substring or semantic matching. All avoid any server; FlexSearch's SQLite/IndexedDB persistence and Orama's vector type make them the closest browser analogues of FTS5 + sqlite-vec.

### Transformers.js in-browser embeddings (SemanticFinder demo)

- **Link:** <https://github.com/do-me/SemanticFinder>
- **Kind:** oss-project
- **Relevance:** `██████░░░░` 6/10

**What it is.** Frontend-only semantic search: transformers.js runs ONNX models (default WASM backend with q8 quantization, WebGPU optional, q4/fp16 available) and computes cosine similarity client-side. all-MiniLM-L6-v2 is ~23-30MB quantized (384-dim). Reported cost: ~1-2 minutes on CPU to embed a 660K-character text in 1000-char chunks; subsequent queries ~2s; model cached in browser after first load.

**Why it matters here.** Establishes the ceiling for true transformer embeddings in the browser: workable for a few thousand short items embedded once and cached, too slow to embed every log line on the fly. Prefer a static-embedding lookup (Model2Vec-style) for hot paths and reserve MiniLM/bge-small (via transformers.js or fastembed ONNX on the backend) for one-time indexing or reranking a handful of candidates.

### bm25s: numpy/scipy BM25 with memory-mapped indexes

- **Link:** <https://github.com/xhluca/bm25s>
- **Kind:** oss-project
- **Relevance:** `█████░░░░░` 5/10

**What it is.** Eagerly computes BM25 scores into scipy sparse matrices; up to ~500x faster than rank-bm25 and faster than Elasticsearch on most BEIR sets in the paper (arXiv 2407.03618); numpy-only core, optional PyStemmer/numba; mmap loading drops RAM 4.36GB->0.49GB on NQ. No incremental updates - rebuild on change.

**Why it matters here.** Pure in-process Python alternative to FTS5 when you want to control tokenization (e.g. split on _ - . / so identifiers become searchable parts) and ship the index as files. Best for read-mostly corpora rebuilt in batch (confluence dumps, codebase); worse than FTS5 for continuously appended slack/log data.

### Query rewriting from click logs / synonym mining (classic, pre-LLM)

- **Link:** <https://www.semanticscholar.org/paper/Query-Rewriting-using-Automatic-Synonym-Extraction-Mandal-Khan/1346c1bdf3092fcc91af81a6f21abca4c99b5b05>
- **Kind:** paper
- **Relevance:** `█████░░░░░` 5/10

**What it is.** SIGIR eCom 2019 (Mandal, Khan et al., eBay): mine synonym candidates from query-query transitions within sessions, query->clicked-title pairs, and title-title co-occurrence, then filter with a classifier to separate true synonyms from merely related terms. Related lineage: TaoBao query rewriting (CIKM'22), ORCAS click dataset, and old patents on learning rewrite rules from query logs. Note: classical pseudo-relevance feedback (RM3) can hurt recall badly on hard queries per recent arXiv studies, so blind expansion is risky.

**Why it matters here.** Instructive template for the 'one-time LLM' plan: the recorded agent traces are your click log (query the agent issued -> result it actually used). Mine (source-entity -> search-string) and term-synonym pairs from them exactly as these systems mine sessions, then compile into a static synonym/alias table per tool. Warns against blind PRF-style expansion without a filter.

## Dead ends

_Queries and directions that produced nothing useful. Listed so nobody repeats them._

- site:arxiv.org query rewriting without LLM / RM3 / synonym expansion -- results are dominated by 2025-2026 LLM-PRF papers; the only non-LLM takeaway is that RM3-style blind expansion often underperforms BM25 (recall@1000 0.51 vs 0.77 on hard sets), i.e. a negative result rather than a technique.
- 'query templates per tool MCP deterministic chaining' -- returned MCP spec/prompt-primitive articles and MCP-Diag (a deterministic JSON-schema translation layer for CLI output, not a query-generation approach); nothing on per-tool search-string templates.
- rule-based identifier extraction (ticket IDs, trace IDs) -- only generic NER/regex marketing pages and Spark NLP RegexMatcher; no library specialized for observability identifiers beyond Drain3 masking. Assume hand-written regex + gazetteer.
- model2vec in the browser -- no npm package or official JS build exists; only model2vec-rs 'wasm' feature and third-party 'easy-embeddings'/Ternlight blog posts. Would require a self-built WASM or a JS reimplementation of the lookup.
- sqlite-lembed (llama.cpp embeddings inside SQLite) -- work-in-progress, no batching, CPU-only prebuilt binaries; not recommended over fastembed/Model2Vec for embedding tool results.
- SPLADE / learned sparse expansion -- needs a BERT-size model at query time (or inference-free document-side variants that still need model-side indexing); too heavy relative to static embeddings for this budget, and no browser build found.
- keyqueries paper via WebFetch -- PDF not parseable by the fetch tool; extracted locally with pypdf instead (definition and evaluation confirmed, exact enumeration algorithm is in the cited Gollub et al. paper not fetched).
- Semantic Scholar page for the eBay synonym paper returned empty content; the SIGIR-eCom PDF also failed to parse, so the summary relies on the search abstract and secondary descriptions.
