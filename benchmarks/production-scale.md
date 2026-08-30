---
sidebar_label: Scale evidence
slug: /production-scale
title: Production scale measurements
description: Measured pull-request, queue, publisher, webhook, and multi-repository behavior for the v0.1 release.
status: current
last_verified: 2026-08-30
---

# Production scale measurements

## Decision

The v0.1.0 candidate needs no new queue service, cache, quota, or index. Two
measured PostgreSQL query changes were worthwhile:

- materialize the leased-repository set once while choosing a review job;
- let concurrent publishers skip review runs already locked by another
  publisher.

Both changes preserve the existing durable queue, per-repository fairness,
lease fencing, and PostgreSQL transaction boundaries.

## Environment and method

- Client: CPython 3.14.0 on macOS arm64, Psycopg 3.3.4 and Psycopg Pool 3.3.1.
- Database: the pinned PostgreSQL 17.10 Linux arm64 image and production
  migrations in a fresh container.
- Queue latency: 20 sequential claims at 100, 1,000, and 10,000 ready jobs;
  bulk setup time is excluded.
- Concurrency: production pools, admission, claim functions, and transaction
  boundaries; no database behavior is mocked.
- Resources: process peak RSS, PostgreSQL buffer counters and held lock modes,
  plus production pool sizes and peak waiters.

These are local capacity measurements, not a promise about model-provider
throughput or network latency.

The repository includes the exact
[measurement driver](https://github.com/CCimen/review-agent/blob/main/benchmarks/run_production_scale.py)
and its immutable
[v0.1 receipt](https://github.com/CCimen/review-agent/blob/main/benchmarks/production-scale-receipt.json).
Run it only against a disposable database whose name ends in `_benchmark`; it
resets the `review_agent` schema:

```bash
python benchmarks/run_production_scale.py \
  --database-url postgresql://user:password@host/review_agent_benchmark \
  --reset-schema
```

## Results

### Large pull request

The changed-file reader completed GitHub's maximum 3,000-file inventory in 30
paginated requests. It used 38.37 ms wall time, 38.31 ms CPU time, 2.56 MiB peak
Python allocations, and a 60.28 MiB process RSS high-water mark. Coverage
remained complete; the reader did not retain all raw response bodies.

### Review queue

| Ready jobs | Median | p95 | Maximum | Pool waiters |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 2.81 ms | 4.38 ms | 4.89 ms | 0 |
| 1,000 | 46.59 ms | 56.10 ms | 61.03 ms | 0 |
| 10,000 | 26.98 ms | 33.16 ms | 33.24 ms | 0 |

Each case made 20 sequential production claims. The query materializes the
leased-repository set once instead of rescanning it for each candidate. Peak
process RSS was 62.28 MiB; the receipt also preserves buffer deltas and held
lock modes for diagnosis. No claim failed or deadlocked.

### Publishers and App intake

The publisher query skips review runs already locked by another replica:

| Publisher replicas | Distinct claims | Total wall time |
| ---: | ---: | ---: |
| 1 | 1 / 1 | 25.72 ms |
| 2 | 2 / 2 | 20.86 ms |
| 10 | 10 / 10 | 65.28 ms |

All claims were unique. Existing tests still prove that two publishers cannot
own the same publication generation.

The production GitHub App admission path durably registered 100 deliveries
through eight clients in 82.58 ms (about 1,211 deliveries/second) with all 100
receipts present. The four-connection admission pool peaked at six waiters and
did not reject a receipt.

### One hundred repositories

Ten worker threads claimed one ready review from each of 100 repositories in
290.32 ms. All 100 job IDs were unique. The production pool remained bounded at
11 connections and peaked at seven waiters; PostgreSQL's row-lock skipping
prevented a blocked claim chain. Peak process RSS was 63.16 MiB.

This proves queue and claim behavior for a representative 100-repository mix.
It does not mean that one deployment should start 100 model calls without first
checking provider, CPU, memory, PID, and database budgets.

## Operating consequence

Keep the shipped defaults until observed queue age justifies more capacity.
Size PostgreSQL with the pool formula in the operations guide, increase worker
concurrency gradually, and add replicas for failure isolation. Re-run these
measurements before changing the queue schema or introducing external queue
infrastructure.
