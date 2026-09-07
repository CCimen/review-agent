---
sidebar_label: Optional code graph
slug: /code-graph
title: Optional code graph pilot
description: Help the reviewer locate callers, shared definitions, and tests at the exact review commit.
status: current
last_verified: 2026-09-07
---

# Optional code graph pilot

The code graph helps a reviewer find relevant code beyond the changed files.
It returns candidate paths, symbols, and line ranges for source inspection.
It is off by default and is included in the v0.4.0-rc.1 prerelease. Install the
matching image and review profile when enabling the pilot.

Structural queries find known symbols, callers, callees, references, and possible
tests without an embedding model. Optional OpenAI `text-embedding-3-small`
embeddings support description searches when the symbol name is unknown.
The reviewer must read the candidate source before using it in a finding.
Unresolved name matches do not prove calls, and test links do not prove coverage.
Ambiguous names return candidate symbols that the reviewer can use to narrow a query.

## Enable a small pilot

The supplied deployment overlay supports Compose. It has not been qualified for
OpenShift or multi-instance graph service operation. Keep one graph service per
cache volume. Normal review services do not depend on its health or startup.

1. Choose repositories already authorized by the GitHub App and enabled for
   Review Agent. Put their numeric GitHub repository IDs in the deployment
   environment. This setting grants no additional GitHub access:

   ```dotenv
   REVIEW_AGENT_CODE_GRAPH_REPOSITORY_IDS=123456,789012
   REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS=none
   REVIEW_AGENT_CODE_GRAPH_CACHE_BYTES=10737418240
   ```

2. For description searches, set `REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS=openai`
   and add `REVIEW_AGENT_OPENAI_API_KEY` in Dokploy's Environment settings (or
   the deployment's private `.env` file). The overlay passes the key only to the
   GitHub gateway. Keep the actual value out of tracked files, chat, and logs.
   Leave it empty in structural mode.

3. Include the overlay when applying your normal deployment procedure:

   ```bash
   docker compose -f compose.yaml -f compose.code-graph.yaml config --quiet
   docker compose -f compose.yaml -f compose.code-graph.yaml up -d --no-build
   ```

   Follow the [upgrade procedure](DEPLOYMENT.md#upgrade-and-roll-back-production)
   when installing a new image and profile. The commands above do not replace
   draining reviews, backup, profile installation, or migration checks.

4. Request an authorized `/review` on a selected repository. Its begin response
   starts preparation in the background. The first review can finish before the
   graph is ready; subsequent reviews can reuse it. Inspect graph service logs
   and the returned `code_graph` status during the pilot.

Repository content cannot enable graphs, choose a provider, or grant permission
to upload metadata. These are operator settings. There is no embedding API key
in the reviewer prompt, graph service, parser environment, or graph volume.

## Freshness and reuse

Preparation follows review requests, rather than a timer. Each retained graph
belongs to a GitHub repository ID, exact head SHA, and indexer configuration.
Branches at the same commit share a snapshot. Different commits retain separate
graphs while cache space permits, so simultaneous PRs cannot select each other's
code. Every query rechecks the live worker lease and repository authorization.

A new commit starts from a compatible graph of the same repository. File hashes
identify changed and deleted paths for incremental parsing. CRG retains vectors
whose symbol metadata and provider identity are unchanged and removes orphaned
vectors. Changing the embedding model/configuration or losing the cache requires
fresh vectors. A body-only edit may need graph updates without new embeddings:
the embedding text contains symbol names, signatures, parent context, and short
docstrings, not function bodies. The gateway bounds each text to 2,048 characters.

If embeddings fail, a later review can retry them once against the retained graph
without downloading or parsing the same commit again. The graph is copied to
staging so a failed write cannot damage the retained index. Repeated preparation calls
within that review do not retry. There is no background retry loop.

OpenAI uses 1,536-dimensional vectors. Known-symbol and relationship queries stay
local; only description searches call the embedding API. All vectors are stored
and searched locally. Cloud embeddings reduce local model memory, but do not
remove the cost of scanning vectors during semantic search.

## Resource and failure behavior

The graph service has its own cache volume, a 4 GiB memory limit, two CPU cores,
and no external network. The existing gateway downloads exact GitHub archives
with installation tokens and proxies the fixed OpenAI embedding operation when
enabled. It retains ownership of credentials and authorization. PostgreSQL
remains the application database; CRG's local graph database is a disposable
derived artifact.

The service runs one index build or query at a time. Busy, cold, failed, revoked,
or incompatible indexes do not supply graph context. Normal source tools and
review publication continue through their existing checks. The model must not
poll or shorten its review because a graph is unavailable.

Bounds include a 64 MiB compressed archive, 256 MiB expanded files, 10,000 files
and 20,000 archive entries, 100,000 graph nodes, a 2 GiB graph database, and a
five-minute maximum for the indexer process. Files above the existing 2 MB source-read
bound, binary files, symlinks, and repository-supplied CRG parser configuration
are skipped and counted. Unsafe paths, duplicate source entries, special files,
or exceeded bounds reject the snapshot. Parser errors are counted in graph
context; a graph is never evidence of complete repository coverage.

The cache defaults to 10 GiB and evicts older snapshots before building a new one.
Allow at least another 4.25 GiB of free working space for bounded staging and
journal files. The cache limit is configurable; the parser and response limits
are resource guards. They do not limit which PRs the normal reviewer can review.

To disable the pilot, empty the repository ID setting or remove the overlay and
recreate the affected services through the normal deployment procedure. Keep the
cache volume if reuse is useful. It contains derived repository metadata and
vectors; removing it does not remove reviews, findings, or feedback.

## What the evidence supports

On one real Django PR, structural queries found a relevant validation call chain
and references to both affected implementation owners. MiniLM on CPU and full-size
OpenAI embeddings each found four of six description targets in the first five
results. OpenAI used less process memory but took longer per query. At the
published $0.02 per million input tokens, Django's 1.22 million metadata tokens
cost approximately $0.0244 per initial embedding pass, excluding later queries
and retries. See the [evaluation and method](https://github.com/CCimen/review-agent/blob/main/docs/assessments/2026-09-07/code-graph-evaluation.md).

This demonstrates code discovery, not improved review accuracy. Before enabling
the graph broadly, compare actual reviews with and without it on several PRs,
including safe changes. Measure missed defects, false positives, review time,
token use, and resource use on the deployment hardware.
