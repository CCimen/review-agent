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

## See whether reviews use it

A query that the graph service answers writes one INFO line from the reviewer
process, logger `hermes_plugins.review_agent_tools.tools.related_code`:

```text
Code graph query run_id=412 pattern=callers_of status=ready results=3
```

`status` is the service's answer (`ready`, `building`, `queued`, `busy`,
`disabled`, `embeddings_unavailable` or `unavailable`) and `results` the number of
locations returned. The query target is written by the model and is not
logged. When the service cannot be reached, its answer is invalid or for
another commit, or the result is larger than a tool result may be, the line is
`Code graph query run_id=412 status=unavailable results=0`, without `pattern`.

These lines do not count every attempt. Nothing is written when `hermes-review`
has no graph URL because the overlay is not loaded (the tool answers
`disabled`), when the tool rejects the pattern or target, when the review has
ended or lost its worker lease, or when reading the pull request through the
GitHub gateway fails. Counting the lines per `run_id` in the `hermes-review`
logs shows how many queries reached the service and what it answered.

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

   `review-github-gateway` reads the first two values when it starts. Once any
   settings revision has been saved in the admin console, even one saved to
   change another setting, the gateway takes both from that revision and
   ignores the two variables
   ([environment defaults and admin controls](ADMIN_PANEL.md#environment-defaults-and-admin-controls)).
   From then on, change **Include repository code relationships** and **Code
   graph embeddings** in **Settings** and restart `review-github-gateway`.

   This applies to every repository authorized by the GitHub App and enabled
   for Review Agent, including repositories enabled later. Repository
   disablement, installation revocation, profile, and worker-lease checks still
   apply to graph access.

2. For semantic queries, set `REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS=openai`
   and add `REVIEW_AGENT_OPENAI_API_KEY` in Dokploy's Environment settings (or
   the deployment's private `.env` file). The overlay passes the key only to the
   GitHub gateway. Keep the value out of tracked files, chat, and logs. Leave it
   empty with `none`. With `openai`, symbol metadata is sent to OpenAI whenever
   a graph is prepared, not only when a semantic query runs
   ([Freshness and reuse](#freshness-and-reuse)).

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
   changes need no new template: change the setting that applies (step 1) and
   start `review-github-gateway` again.

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
the embedding configuration or losing the cache requires fresh vectors.

The embedding API is called on two occasions, both through the GitHub gateway.
Preparing a graph sends the embedding text of every symbol that has no current
vector: all of them the first time a repository is indexed, new and changed
ones afterwards. This happens whether or not a review later asks a semantic
query. Each `semantic` query sends its query text. The other patterns send
nothing, and vectors are stored and searched locally.

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

`review-github-gateway` reads the setting only when it starts, so turn it off
where the gateway reads it:

- If a settings revision has been saved in the admin console, clear **Include
  repository code relationships** in **Settings**, save, and restart
  `review-github-gateway`. The saved revision overrides
  `REVIEW_AGENT_CODE_GRAPH_ENABLED`, so changing only the variable leaves the
  graph on.
- Otherwise set `REVIEW_AGENT_CODE_GRAPH_ENABLED=false` and recreate
  `review-github-gateway` so the container receives the new value.

From then on the gateway tells the graph service that the graph is disabled and
refuses its archive and embedding requests. Queries answer `disabled`, and no
source is downloaded and no metadata is sent to OpenAI.

The `review-code-graph` container keeps running until it is stopped. Leaving
the overlay out of a later `docker compose up` does not stop a container that
already exists unless the command includes `--remove-orphans`. To retire the
service, stop it with the overlay still named:

```bash
docker compose -f compose.yaml -f compose.code-graph.yaml stop review-code-graph
```

Then deploy without the overlay, which also removes the graph URL from
`hermes-review`. Keep the cache volume if reuse is useful. It contains derived
repository metadata and vectors; removing it does not remove reviews, findings,
or feedback.
