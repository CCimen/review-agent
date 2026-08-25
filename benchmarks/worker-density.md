# Worker density optimization report

## Decision

**Status:** accepted pending the final commit gate

One bounded worker process can provide 32 logical review slots with about 29 MiB
of steady idle RSS, one retained PostgreSQL connection, and 1.2–1.4 empty-queue
transactions per second. The previous 32-process shape used about 922 MiB, 32
connections, and 35.4 transactions per second.

## Objective and guardrails

Reduce aggregate worker RSS, PostgreSQL connections, and polling transactions as
cross-repository capacity grows. Preserve review results, claim priority, the
one-live-review-per-repository rule, lease generations, heartbeats, retry and
recovery behavior, graceful shutdown, and durable PostgreSQL state. New queue
infrastructure, caches, runtime experiments, and behavior changes are out of
scope.

## Runtime and workload

- Image runtime: CPython 3.13.5, Linux arm64, release build, normal GIL, no
  enabled JIT or free-threading, glibc 2.41, six effective CPU cores.
- Database: PostgreSQL 17.10 using the schema-v5 production migrations.
- Dependencies: psycopg 3.3.4 and psycopg-pool 3.3.1.
- Images: `review-agent:contract-fix` for the baseline and
  `review-agent:perf-candidate`, built from this candidate. Both use the pinned
  Hermes v2026.8.3 base and installed managed profile.
- Workload: an empty durable queue, two-second polling, and 1, 8, or 32 logical
  worker slots. Setup and image build were excluded. RSS was sampled after
  startup; transactions were measured over 10- or 20-second steady intervals.

This workload isolates scheduler overhead from model latency and provider
variability. PostgreSQL integration tests cover active concurrency. The report
makes no synthetic model-throughput claim.

## Baseline and profile evidence

| Logical slots | Processes | Steady RSS | DB connections | Empty-queue transactions |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 1 | 36.85–37.07 MiB | 1 | 26 / 20 s (1.3/s) |
| 8 | 8 | about 238 MiB | 8 | 182 / 20 s (9.1/s) |
| 32 | 32 | about 922 MiB | 32 | 708 / 20 s (35.4/s) |

A 12.047-second `cProfile` run recorded 297,441 calls. About 11.8 seconds were
waiting on locks, pool shutdown, or stop events. Six claims used about 19 ms in
total, including about 11 ms in `claim_next_job`. CPU-heavy Python did not limit
the workload. Process duplication and independent polling grew with the process
count.

## Change

One dispatcher claims while an execution slot is free and submits the
review to a bounded `ThreadPoolExecutor`. There is no independent task backlog.
The process shares its immutable Hermes client, imports, polling loop, and
PostgreSQL pool. The worker pool is bounded to `concurrency + 1` connections and
the same number of waiters. `REVIEW_AGENT_WORKER_CONCURRENCY` defaults to four
and can be reduced to one or combined with replicas for stronger isolation.

## Results

| Logical slots | Candidate processes | Steady RSS | DB connections | Empty-queue transactions |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 1 | 28.8 MiB | 1 | 12 / 10 s (1.2/s) |
| 8 | 1 | 29.5 MiB | 1 | 12 / 10 s (1.2/s) |
| 32 | 1 | 28.84–29.23 MiB | 1 | 12, 12, 14 / 10 s (1.2–1.4/s) |

At 32 logical slots, steady idle RSS decreased by about 893 MiB (96.8%), retained
connections decreased by 31 (96.9%), and measured empty-queue transaction rate
decreased by about 34.1/s (96.3%). Candidate CPU samples settled between 0.00%
and 0.15%. Baseline aggregate CPU sampling cannot support a valid relative CPU
claim. Candidate startup sampled about 65 MiB before RSS
settled near 29 MiB.

The transaction intervals include the measurement queries. The baseline used
one interval per scale. The 32-slot candidate used three successive intervals.
The density result exceeds measurement noise; this report does not measure tail
latency.

## Correctness and risk

- Focused worker tests prove the execution bound, no claim beyond free slots,
  positive configuration, and unchanged `--once` behavior.
- PostgreSQL integration tests start four cross-repository reviews through the
  real pool at the same time and preserve terminal state transitions.
- Existing tests retain the one-live-lease-per-repository, lease generation,
  heartbeat, retry, recovery, transient database failure, and shutdown rules.
- Threads are appropriate because model calls and PostgreSQL waits dominate;
  they do not claim a pure-Python CPU speedup.
- More reviews share one process failure boundary. Operators can lower
  concurrency or add replicas when isolation matters more than density.

Set `REVIEW_AGENT_WORKER_CONCURRENCY=1` to restore serial execution, or revert
the code. The change leaves the schema, stored data, API, and review output
unchanged.

## Remaining evidence-backed work

The candidate image is 2,817,548,619 bytes, about 60.3 MiB larger than its Hermes
base. Audit image-layer contents in the next container slice. An image change
would invalidate this candidate's runtime comparison. No Python micro-optimization,
cache, alternate queue, free-threaded build, JIT, or Python 3.14 migration is
justified by the profile.
