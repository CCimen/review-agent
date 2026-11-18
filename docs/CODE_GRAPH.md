---
sidebar_label: Optional code graph
slug: /code-graph
title: Optional code graph
description: Help the reviewer locate callers, shared definitions, and tests at the exact review commit.
status: current
last_verified: 2026-11-17
---

# Optional code graph

The code graph helps the reviewer find relevant code beyond the changed files.
It is off by default. It is built with the upstream
[code-review-graph](https://github.com/tirth8205/code-review-graph) library
(CRG) and runs as a separate Compose service.

## What a query returns

The reviewer's `review_agent_related_code` tool accepts one of six patterns:

| Pattern | Finds |
| --- | --- |
| `symbol` | Symbols whose names match the target (keyword search) |
| `semantic` | Symbols matching a description, when the name is unknown |
| `callers_of`, `callees_of`, `references_to` | Structural relationships of a symbol |
| `tests_for` | Possible tests for a symbol |

Each query returns at most ten locations (path, line range, symbol, kind), in
the order the upstream library returns them. Review Agent adds no reranking
stage. Semantic queries use the library's hybrid keyword-and-embedding search
with OpenAI `text-embedding-3-small`, and are available only when embeddings
are enabled and ready; otherwise the tool reports `embeddings_unavailable`. The
other patterns need no embedding model.

Results are hints. The reviewer must read the candidate source at the review
head before using it as evidence. Unresolved name matches do not prove calls,
and test links do not prove coverage. An ambiguous name returns candidate
symbols that the reviewer can query to narrow the target.

## Enable the graph

The supplied overlay supports Compose. It has not been qualified for
multi-instance graph service operation. Keep one graph service per cache
volume. Normal review services do not depend on its health or startup.

1. Enable the deployment capability in Dokploy's Environment settings (or the
   deployment's private `.env` file). It defaults to `false`, including when the
   overlay is loaded:

   ```dotenv
   REVIEW_AGENT_CODE_GRAPH_ENABLED=true
   REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS=none
   REVIEW_AGENT_CODE_GRAPH_CACHE_BYTES=10737418240
   ```

   This applies to every repository authorized by the GitHub App and enabled
   for Review Agent, including repositories enabled later. Repository
   disablement, installation revocation, profile, and worker-lease checks still
   apply to graph access.

2. For semantic queries, set `REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS=openai`
   and add `REVIEW_AGENT_OPENAI_API_KEY` in Dokploy's Environment settings (or
   the deployment's private `.env` file). The overlay passes the key only to the
   GitHub gateway. Keep the value out of tracked files, chat, and logs. Leave it
   empty with `none`.

3. Include the overlay when applying your normal deployment procedure:

   ```bash
   docker compose -f compose.yaml -f compose.code-graph.yaml config --quiet
   docker compose -f compose.yaml -f compose.code-graph.yaml up -d --no-build
   ```

   Follow the [upgrade procedure](DEPLOYMENT.md#upgrade-and-roll-back-production)
   when installing a new image and profile. The commands above do not replace
   draining reviews, backup, profile installation, or migration checks.

   For Dokploy's raw Compose editor, merge the overlay into the existing Compose
   definition once. Given a local copy of that definition as `compose.yaml`,
   generate the merged template with:

   ```bash
   docker compose -f compose.yaml -f compose.code-graph.yaml config \
     --no-interpolate --no-path-resolution --no-normalize > compose.dokploy.yaml
   ```

   Save that template in the raw Compose editor, preserving the deployment's
   existing ingress, mounts, networks, and volumes. Environment references remain
   references; supply their values through Dokploy Environment. Later on/off
   changes need only the environment switch and normal service recreation.

4. Request an authorized `/review` on an enabled repository. Its begin response
   starts preparation in the background. The first review can finish before the
   graph is ready; later reviews can reuse it. Inspect graph service logs and the
   returned `code_graph` status.

Repository content cannot enable graphs, choose a provider, or grant permission
to upload metadata. These are operator settings. The embedding API key is not in
the reviewer prompt, graph service, parser environment, or graph volume.

## Freshness and reuse

Preparation follows review requests, not a timer, so enabling the feature does
not index every repository at once. Each retained graph belongs to a GitHub
repository ID, exact head SHA, and indexer configuration. Branches at the same
commit share a snapshot. Different commits retain separate graphs while cache
space permits, so simultaneous pull requests cannot select each other's code.
Every query rechecks the live worker lease and repository authorization.

A new commit starts from a compatible graph of the same repository; file hashes
select the changed and deleted paths to parse again.

With OpenAI enabled, each code symbol receives its own 1,536-dimension vector.
The embedding text holds the symbol name, signature, parent context, and short
docstring (not the body), cut to 2,048 characters, so a body-only edit needs no
new embedding. CRG keeps unchanged vectors and removes orphaned ones; changing
the embedding configuration or losing the cache requires fresh vectors. Only
semantic queries call the embedding API; vectors are stored and searched
locally.

The structural graph is saved before embeddings start, and each embedding batch
is committed as it completes. If the review ends, authorization is revoked, or
embedding stops, a later authorized review retries only missing or changed
vectors. A failed embedding attempt is not retried within the same review, and
there is no background retry loop. Retained cache data does not authorize
further source reads, embedding requests, or graph queries.

## Data flow and resources

The graph service has its own cache volume, a 4 GiB memory limit, two CPU
cores, and no external network. The existing GitHub gateway downloads exact
GitHub archives with installation tokens and proxies the fixed OpenAI embedding
operation when enabled; it keeps ownership of credentials and authorization.
PostgreSQL remains the application database; CRG's local graph database is a
disposable derived artifact.

The service runs one index build or query at a time and can queue four
preparation requests, coalescing requests for the same repository and commit.
Queued work is authorized again when its turn starts; if its review has already
ended, it is discarded before downloading source or requesting embeddings. A
full queue returns `busy`; pending preparations return `queued`. The queue
clears on shutdown; saved graphs and committed vectors survive a restart.

Busy, queued, cold, failed, revoked, or incompatible indexes, and semantic
queries without ready embeddings, supply no graph context. Normal source tools
and publication continue. The model must not poll or shorten its review because
a graph is unavailable.

| Bound | Value |
| --- | --- |
| Compressed archive | 64 MiB |
| Expanded files | 256 MiB |
| Files / archive entries | 10,000 / 20,000 |
| Graph nodes | 100,000 |
| Graph database | 2 GiB |
| Indexer process | 5 minutes |
| Cache (`REVIEW_AGENT_CODE_GRAPH_CACHE_BYTES`) | 10 GiB default; older snapshots are evicted first |
| Free working space beyond the cache | 4.25 GiB |

Files above the 2 MB source-read bound, binary files, symlinks, and
repository-supplied CRG parser configuration are skipped and counted. Unsafe
paths, duplicate source entries, special files, or exceeded bounds reject the
snapshot. Parser errors are counted in graph context; a graph is never evidence
of complete repository coverage. These bounds apply to the graph only, not to
the pull requests the reviewer can review.

## Disable the graph

Set `REVIEW_AGENT_CODE_GRAPH_ENABLED=false` and recreate the affected services
through the normal deployment procedure. Removing the overlay also stops the
graph service. Keep the cache volume if reuse is useful. It contains derived
repository metadata and vectors; removing it does not remove reviews, findings,
or feedback.
