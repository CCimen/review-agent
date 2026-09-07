# Optional code graph evaluation

Evaluated 7 September 2026 for `ra-quality-2026-09-614.2.9`.

Keep CRG optional. The evidence supports a bounded pilot using structural queries and `sentence-transformers/all-MiniLM-L6-v2` on CPU. It does not justify making graph availability a prerequisite for reviews or choosing a cloud model for better accuracy. No graph integration was added to Review Agent or its deployed runtime.

CRG is the preferred first candidate because its Git-based incremental indexing fits repeated PR reviews. Codebase Memory MCP offers native packaging and parse-coverage reporting, but its watcher invokes the full indexing pipeline. This is a source comparison; CBM throughput was not benchmarked. Sources: [CRG incremental owner at b586687](https://github.com/tirth8205/code-review-graph/blob/b58668751ab0c7670c078cf7cbd4d1f5b8e54f81/code_review_graph/incremental.py), [CBM watcher at aa44c28](https://github.com/DeusData/codebase-memory-mcp/blob/aa44c28ea5ea82a5f811f0bace4f7857a68cac80/src/watcher/watcher.c).

## Real PR and method

The main case is [Django PR 21724](https://github.com/django/django/pull/21724), which corrects uniqueness validation when a database default is a nonconstant expression. Two implementation owners matter: `Model._perform_unique_checks` and `UniqueConstraint.validate`. The related `DatabaseDefault`, model validation entry point and regression tests provide six retrieval targets. Source inspection established these targets before model results were available.

CRG 2.3.8 was pinned to `b58668751ab0c7670c078cf7cbd4d1f5b8e54f81`. The graph was built at Django `812c08bd4e9da7b74ab9ee0db83da58a6da48d19`, then incrementally updated to its child merge commit `febefb175e03352e5aeb2ed827024bacab96cf16`. The PR changes seven files, six of them Python. No Django code or tests were executed; this evaluates retrieval around a real change, not defect detection by a reviewer.

Each configuration used a separate copy of the same updated graph and CRG's native `hybrid_search`, with ten results requested. A hit requires the exact target symbol in the top five. Six description queries and six known-identifier queries were fixed before execution. The baseline is CRG without embeddings, not a tuned lexical retrieval system. Queries, ranks and measurements are retained in [results.json](results.json).

The host was an Apple M4 Pro with 48 GiB RAM, macOS 26.5.2. Both embedding models used CPU only, four Torch compute threads and one interop thread. Each model ran once; timing is an observation, not a statistical performance estimate. Model weights were already cached for the Django runs. CRG used its file-based community fallback because `igraph` was unavailable, so these measurements do not cover Leiden community detection.

## Measurements

The initial graph build parsed 3,001 files in **51.10 seconds**. Updating the six changed Python files took **10.71 seconds**, including full postprocessing. The updated graph supplied **41,952 non-file symbols** for embedding.

| Configuration | Initial embedding time | Description hits in top 5 | Identifier hits in top 5 | Median description query | Peak process RAM |
| --- | ---: | ---: | ---: | ---: | ---: |
| CRG without embeddings | None | 0/6 | 6/6 | 0.079 s | 447 MiB |
| MiniLM-L6-v2 | 80.61 s | 4/6 | 6/6 | 1.427 s | 1,270 MiB |
| BGE-small-en-v1.5 | 210.67 s | 3/6 | 6/6 | 1.377 s | 1,168 MiB |

MiniLM is the better starting candidate for this case: it required less initial embedding time and returned one more exact target in the top five. Six description queries are too few to establish a general model ranking. Both models missed `_perform_unique_checks` in their first ten description results. MiniLM placed `DatabaseDefault` sixth; BGE placed the full-validation entry point seventh and one regression test sixth. Known identifiers remained useful with every configuration.

The graph database occupied 692 MiB without vectors and 780 MiB with either 384-dimensional model. Model files are additional storage. An unchanged embedding pass wrote zero vectors and took 1.33 seconds with MiniLM or 0.97 seconds with BGE. Changing one symbol's metadata wrote exactly one vector, taking 0.0067 or 0.0133 seconds respectively. That controlled update proves hash-based reuse; it is not a measurement of embedding every change in the real PR.

CRG embeds symbol metadata, including names, parameters, return types, parent context and up to 400 docstring characters. It does not embed function bodies. A body-only change can therefore leave the vector unchanged. Graph freshness and source reads remain necessary regardless of the embedding model. Native semantic search reads and scores every stored vector and sorts the scores locally, including when the embedding provider is remote. Cloud embeddings do not remove that growth cost. Source: [embedding text, reuse and search owners](https://github.com/tirth8205/code-review-graph/blob/b58668751ab0c7670c078cf7cbd4d1f5b8e54f81/code_review_graph/embeddings.py#L1094).

An earlier exploratory run on Review Agent's 2,997 symbols took 7.57 seconds for MiniLM and 15.83 seconds for BGE. Both scored 0/6 description hits and 5/6 identifier hits. Its sentence-initial capitals triggered CRG's class-name query boost, which confounds comparison with the lower-case Django queries. Those results are retained separately in the local artifacts and are not pooled into the Django score. The native BGE path also omits the model card's suggested retrieval-query prefix; no custom prefix tuning was tested.

## Structural usefulness and failure modes

CRG found the source-confirmed `full_clean → validate_unique → _perform_unique_checks` call chain through two caller queries. A `DatabaseDefault` reference query found both changed implementation owners among seven results in about 1.3 ms. These relationships can help a reviewer expand beyond the diff without an embedding model.

The limitations were visible in the same case. `callers_of UniqueConstraint.validate` returned four unresolved bare-name matches involving unrelated validation methods. CRG marks them `target_resolution=unresolved`; an integration must preserve that marker. Its `tests_for` queries returned 36 and 62 results for the two owners but omitted the new regression tests. Test relationships cannot establish complete behavioral coverage. Source: [query resolution and test traversal](https://github.com/tirth8205/code-review-graph/blob/b58668751ab0c7670c078cf7cbd4d1f5b8e54f81/code_review_graph/query.py#L390).

## Cloud cost

At the published standard rates checked on 7 September, 10 million input tokens cost **$0.20** for [OpenAI text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small) or **$1.50** for [Gemini embedding-001](https://ai.google.dev/gemini-api/docs/pricing#gemini-embedding). These are embedding API charges; hosting, storage, retries, queries and review-model use are additional. Batch API discounts require that API, not simply passing a list of texts in an ordinary embedding request.

The actual Django symbol metadata contained 6,295,874 characters. Counting each text locally with `tiktoken` 0.14.0 and `cl100k_base` produced **1,217,904 OpenAI tokens**, an estimated **$0.0244** for one initial text-embedding-3-small pass. This is a local estimate, not an API billing receipt. Gemini uses a different tokenizer, so the OpenAI count is not presented as its billable count. No source was uploaded to an embedding API and no paid cloud request was made.

With a retained compatible cache, later embedding cost follows changed metadata plus query tokens. Rebuilding the graph alone need not re-embed unchanged metadata; losing the vector cache or changing provider/model identity can require a full pass. At this scale, cloud token cost is small enough to test later, but cloud accuracy, network latency and production rate limits remain unmeasured.

## Integration decision

Proceed only with an optional pilot. Use the existing authorized source context as the owner of review evidence. A graph should return bounded candidate paths and relationships, followed by reads of the exact authorized commit. CRG indexes a working tree, so a pilot must provide an immutable checkout and key derived state by repository, commit and indexer configuration. It must preserve authorization, stale-head checks, bounded output and unresolved-edge markers. Index failure or staleness should fall back to existing source tools and disclose unavailable graph context.

The pilot should expose only the required read operations. CRG also has install hooks, memory writes and refactoring capabilities; exposing its entire MCP surface is unnecessary for this use. Keep derived graph storage disposable and PostgreSQL application state canonical. Removing the optional graph must leave normal review and publication behavior available.

Before making it a default, compare actual Review Agent runs with and without graph context on several real PRs, including safe changes. Measure missed defects, false positives, review time, token use and index resource cost on the deployment hardware. This retrieval experiment establishes feasibility and specific limits; it does not establish improved review accuracy or production capacity.

Model revisions: [MiniLM 1110a24](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/1110a243fdf4706b3f48f1d95db1a4f5529b4d41), [BGE 5c38ec7](https://huggingface.co/BAAI/bge-small-en-v1.5/tree/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a). Local scripts, complete ranked responses, build logs and databases are retained under `~/.codex/artifacts/review-agent-crg-evaluation-20260907/`. Temporary benchmark registry entries were removed; no repository hooks were installed.
