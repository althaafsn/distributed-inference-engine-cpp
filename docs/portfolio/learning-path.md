# Learning path

Eight sessions, about 60–90 minutes each. Do them in order. Two sessions per week is enough.

The goal is not to memorize the code. The goal is to explain each engineering decision and its evidence without notes.

## Before you start

Build the binaries and generate the image fixture once:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
python3 benchmark-aws/autotune/prepare_data.py \
  --output benchmark-aws/data/imagenet500 --count 500 --force
```

The fixture is downloaded from a pinned ImageNet subset. It is ignored by Git. If it already exists, skip it.

## Session 1 — Run the system and map the request path

**Goal:** know which process does what.

**Read:** `README.md` (Architecture), `src/gateway.cpp` (`main`, routing setup), `src/worker_node.cpp` (HTTP server and batch processor).

**Run:**

```sh
python3 benchmark-aws/run_autotune.py \
  --model models/resnet50-v2-7.onnx \
  --data benchmark-aws/data/imagenet500 \
  --output /tmp/opencode/learn-01 \
  --worker-bin build/worker_node --gateway-bin build/gateway \
  --workers 2 --levels 1 --requests 4 --mode unique --cache 1 --threads 2 --force
```

**Prove:** open `/tmp/opencode/learn-01/level-1/summary.json`. Write down the request count, successes, p50, and p95.

**Answer:**

1. Which process loads the ONNX model?
2. What does the gateway do before it forwards a request?
3. What does the gateway not do?

**Done when:** you can draw the three-process path from memory.

## Session 2 — Batching and tail latency

**Goal:** explain why a larger batch raises throughput and hurts tail latency.

**Read:** `include/batch_processor.h`, `src/worker_node.cpp` (batch deadline and maximum batch size), `docs/timing-instrumentation.md`.

**Run:** the same command as Session 1, twice, with `--deadline 1` and `--deadline 10`. Keep the output directories separate.

**Prove:** build a small table: deadline, p95, mean batch size, accepted throughput. Use `queue_wait_us` and `batch_execution_us` from `requests.jsonl`.

**Answer:**

1. Why does a 10 ms deadline produce larger batches than a 1 ms deadline?
2. Why is the default 10 ms and not 100 ms?
3. What happens to a request that waits in the batch queue?

**Done when:** you can explain the tradeoff with your own numbers.

## Session 3 — Cache and routing

**Goal:** explain cache locality and what changes when a worker dies.

**Read:** `include/lru_cache.h`, `include/consistent_hash.h`, `src/consistent_hash.cpp`, and the routing code in `src/gateway.cpp`.

**Run:** two short runs: one with `--mode repeated --cache 1000`, one with `--mode unique --cache 1`. Then set `ROUTING_POLICY=hash` before a third run and compare it with the default least-loaded policy.

**Prove:** record cache hits and p95 for each run. Explain the gap.

**Answer:**

1. Why does consistent hashing improve cache locality?
2. What is the cost when the chosen worker fails?
3. When is least-loaded routing better than hashing?

**Done when:** you can explain why the 8-worker campaign used least-loaded routing.

## Session 4 — Admission control

**Goal:** explain why a service should reject work it cannot finish in time.

**Read:** `src/gateway.cpp` (`admitRequest` and `AdmissionGuard`), `include/adaptive_admission.h`, `docs/overload-protection.md` (local tables).

**Run:** two 10-second runs at 50 requests/s, one with `--admission-max 0`, one with `--admission-max 7`. Use 128 connections and a cold cache.

**Prove:** record accepted, 503 count, shed rate, p95, and wall time for each. Compare with the published table.

**Answer:**

1. Why does the baseline return HTTP 200 for every request but still fail?
2. Why is the admission limit a compare-and-swap loop?
3. Why is shedding 43–48% the correct behavior at 2× the operating point?

**Done when:** you can defend the 503 contract without calling it an error.

## Session 5 — Metrics and retry

**Goal:** connect the gateway counters to the client behavior.

**Read:** `include/gateway_metrics.h`, `benchmark-aws/autotune/loadgen.py` (`RetryConfig`, `parse_retry_after`, `retry_delay_seconds`), `src/gateway.cpp` (`/metrics`, `/stats`).

**Run:** a 10-second run at 50 requests/s with `--admission-max 7 --retry-attempts 3 --retry-backoff-ms 100 --retry-max-backoff-ms 1000 --retry-jitter 0.2`.

**Prove:** read `summary.json` and record `retry_successes`, `retry_exhausted`, p95, and the 503 count. Note that retry p95 includes the wait between attempts.

**Answer:**

1. What does `Retry-After` tell the client?
2. Why add jitter to the backoff?
3. Why not retry forever?

**Done when:** you can explain why retry improves success rate but cannot create capacity.

## Session 6 — Local HA and failover

**Goal:** explain the difference between a fast proxy failover and a slow ALB failover.

**Read:** `benchmark-aws/ha_proxy.py`, `benchmark-aws/run_ha.py`, `docs/overload-protection.md` (Gateway HA section).

**Run:**

```sh
python3 benchmark-aws/run_ha.py \
  --model models/resnet50-v2-7.onnx \
  --data benchmark-aws/data/imagenet500 \
  --output /tmp/opencode/learn-06 \
  --worker-bin build/worker_node --gateway-bin build/gateway \
  --rate 50 --duration 10 --concurrency 128 \
  --admission-max 4 --threads 2 --kill-after 2 --force
```

**Prove:** record the failover window, client-visible errors, and whether the killed gateway returned to rotation.

**Answer:**

1. Why did the proxy fail over in about 0.2 s?
2. What does the proxy do on a connection error?
3. Why does the per-gateway admission total drop during failover?

**Done when:** you can contrast the 0.2 s local result with the 16.6 s ALB result.

## Session 7 — The AWS ALB harness

**Goal:** trace the AWS run without spending money.

**Read:** `benchmark-aws/ha_controller.py`, `benchmark-aws/ha_preflight.py`, `benchmarks/results/2026-11-04-aws-alb-ha.json`, `docs/aws-cost-ledger.md`.

**Run:** only the local dry run:

```sh
python3 benchmark-aws/ha_preflight.py --dry-run
```

Then recompute the successful-run cost from the artifact with a short Python script. Compare it with `$0.074941`.

**Prove:** write the run order from memory: preflight, groups, instances, payload, services, ALB, normal, kill, failover, recovery, cleanup. Mark where state is saved.

**Answer:**

1. Why did the ALB take 16.576 s to report the target unhealthy?
2. Why were there both 502 and 504 responses?
3. Why did cleanup still terminate instances if ALB deletion failed?

**Done when:** you can explain the controller without running it.

## Session 8 — Interview drill

**Goal:** say the story cleanly in two minutes and survive follow-up questions.

**Read:** `docs/portfolio/case-study.md` and `docs/portfolio/resume-entry.md`.

**Run:** record yourself on your phone. Two minutes: problem, what you built, result, failure, limit, next step.

**Prove:** answer all 12 questions in the resume entry without notes. Mark the three you cannot answer, and reread those code paths.

**Answer:**

1. What is the strongest number in the project, and what is the context?
2. What is the most interesting failure, and what did you learn?
3. What did you personally do, and where did AI help?

**Done when:** you can give the two-minute version and answer the three weakest questions without notes.

## After the eight sessions

- Add the project to your resume with the primary entry.
- Link the case study, not the raw repository, unless you have confirmed the upstream license.
- Revisit one session before each interview. Do not add features just before applying.
