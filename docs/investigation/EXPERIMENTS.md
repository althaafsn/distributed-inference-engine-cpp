# Continuous optimization experiment ledger

This is the resumable record for [CONTINUOUS-OPTIMIZATION.md](CONTINUOUS-OPTIMIZATION.md). Raw logs stay in the Git-ignored `benchmark-aws/runs/` directory. Sanitized summaries may be published under `benchmarks/results/`.

## Cycle 0 — trustworthy starting point

- HEAD `9c7ab13`, branch `main`, remote `origin/main`.
- The timing instrumentation, benchmark runner, and backend-selection work were already committed. The only pre-existing untracked path was `docs/investigation/`.
- Preserved the user's uncommitted thread-tuning work. Did not reset, clean, stash, or delete it.
- Local tools: Python 3.14.4, CMake 4.2.3, g++ 15.2.0, AWS CLI, `perf`, Docker. The system has the ONNX Runtime 1.23.2 runtime but no C++ headers; local real-worker probes used the pinned ORT 1.23.2 release in `/tmp/opencode`.
- Initial checks: `python3 -m unittest discover -s tests -v` → 31 passed, 1 skipped. Later final: 33 passed, 2 intended skips; all 4 opt-in real-worker tests passed when the model and worker were supplied.
- Model SHA-256 `79102261eb6e5fd7af5d27f41316293e388c5cb691e5d25bfb035c4f64fefe31`.
- Pinned benchmark base `c4d8a08b72df810ffad0dce6a993cecae22494bb`.

## Cycle 1 — ORT intra-op thread capacity (accepted)

| Field | Record |
| --- | --- |
| Hypothesis | Four hardcoded ORT intra-op threads oversubscribe a two-vCPU host and inflate whole-batch backend time. |
| Baseline | Pinned upstream source with four threads; prior instrumented AWS run `infer-8bb17685b18e63a4`. End-to-end baseline runs `infer-d4522a282d0dcc42`, `infer-ca8db4050033ae08`. |
| Change | `src/inference_engine.cpp`: default `min(4, hardware_concurrency)` with a floor of 1, `ORT_INTRA_THREADS` override, startup log line. |
| Workload | `benchmark-aws/load.py`, 60 repeated and 60 unique requests at concurrency 1/2/4; 360 requests per run; `c6i.large`, us-east-1. |
| Acceptance rule | Lower unique-input p50 and higher throughput without failures, output mismatch, cache-hit regression, or cleanup/cost failure. |
| Correctness | Default suite 33 passed / 2 intended skips; 4/4 opt-in real-worker tests; 2-vs-4-thread output parity within `1e-4`; 360/360 valid responses per AWS run. |
| Local A/B | `benchmark-aws/runs/local-investigation-2026-11-02/`: two-CPU affinity, 30 unique requests per level. p50 229/384/635 ms → 149/234/404 ms; throughput 4.32/5.19/6.16 → 7.38/9.04/10.08 req/s. |
| AWS A/B | Baseline mean p50 237.9/424.3/701.5 ms → changed 149.9/231.7/403.9 ms at c1/c2/c4. Throughput 4.11/4.65/5.56 → 7.44/9.11/10.26 req/s. Backend p50 196.0/371.6/659.8 → 86.2/168.9/338.5 ms. Repeated workload unchanged. No failures. |
| Decision | Accepted. Supported by matched end-to-end runs, a local paired probe, a negative-control workload, and output parity. |
| Artifacts | `benchmarks/results/2026-11-02-aws-thread-tuning.json`, `docs/aws-thread-tuning.md`, raw runs `infer-d4522a282d0dcc42`, `infer-1f39a2ac8519f743`, `infer-0601374edef6f33d`, `infer-ca8db4050033ae08`. |
| Cost | Four new runs: $0.003958 + $0.003755 + $0.003501 + $0.003958 = $0.015172. Seven-run project estimate $0.024787. Cleanup independently verified. |
| Limits | Single host, synthetic padded inputs, closed-loop client sharing CPU, 60 samples/level. Backend baseline is from a prior session. No p99 claim. |

## Candidate queue after cycle 1

| Priority | Candidate | Evidence | Status |
| ---: | --- | --- | --- |
| — | HTTP keep-alive / Nagle delayed-ACK | Cache-hit p50 ~83 ms through the unmodified gateway vs 0.4–0.7 ms direct; enabling `TCP_NODELAY` reduced AWS cache-hit p50 to 1.16–3.54 ms. | **Accepted in cycle 2** |
| — | Input copy/padding performance | Flatten/pad measured 8/18/39/989 µs for batch 1/2/4/32, negligible against 86–345 ms backend time. The same investigation found and fixed an oversized-input batch-alignment defect. | **Rejected for performance; correctness fixed in cycle 3** |
| — | Batching policy / deadline | 20 ms → 10 ms lowered unique p50 2–9% and queue wait ~50% at c1–c4; 5 ms lost batch size at c32. | **Accepted in cycle 4** |
| — | Gateway per-request logging | Local A/B: logging 141/868/896 vs no-log 147/880/918 req/s; differences were within noise (≤2.5%). | **Rejected as low value** |
| — | Gateway JSON parse/dump | Local A/B: cache-hit p50 1.57/2.09/4.18 → 1.11/1.50/2.70 ms; AWS cache-hit throughput 327.7/884.7/953.1 → 367.9/1,246.8/1,264.4 req/s. | **Accepted in cycle 5** |
| 1 | Cache/routing | Repeated workload is ~100% hits; direct lookups are sub-millisecond. No demonstrated bottleneck. | No action |
| 2 | Measurement quality | Closed-loop client, shared CPU, 60 samples/level, p99 unstable, baseline sessions differ. | Improve with paired runs; do not infer p99 |
| 3 | Multi-host scaling | Not authorized and not measurable on one host. | Out of scope; document only |

## Cycle 2 — HTTP keep-alive / Nagle (accepted)

| Field | Record |
| --- | --- |
| Hypothesis | `CPPHTTPLIB_TCP_NODELAY` defaults to false. With keep-alive, the worker response, gateway response, and gateway request each can stall on Nagle/delayed-ACK, adding ~40 ms per affected hop. |
| Baseline | Accepted thread-tuned source with two ORT threads; AWS runs `infer-1f39a2ac8519f743` and `infer-0601374edef6f33d` (patch `9b4c0303…`). |
| Change | Set `TCP_NODELAY` on the worker HTTP server, the gateway HTTP server, and the gateway's worker client. Added `GATEWAY_PORT` for loopback tests without changing the default port. |
| Alternatives | Keep the default and document the latency; change only the gateway client; replace keep-alive. Each is smaller but leaves at least one hop affected. |
| Expected effect | Cache-hit p50 should fall from ~98 ms end-to-end to near the direct-worker range; unique-request end-to-end should improve by roughly the removed HTTP stall while backend time is unchanged. |
| Adverse effects | More, smaller TCP segments; negligible on loopback and small JSON responses. No correctness effect expected. |
| Workload | Same `load.py` 360-request workload; local A/B with cache-hit and unique levels; AWS A/B against the committed thread-tuned baseline. |
| Acceptance rule | Lower cache-hit p50 without throughput regression, no failed requests, unchanged outputs and request association, and verified cleanup. |
| Correctness | Default suite 33 passed / 3 intended skips; all 5 opt-in real-worker/gateway tests passed; miss and hit outputs equal; request IDs match; source-delivery checks include `src/gateway.cpp`. |
| Local A/B | `benchmark-aws/runs/local-cycle2-2026-11-02/`: two-CPU affinity. Cache-hit p50 83.2/84.0/83.9 ms → 1.6/2.1/4.3 ms; unique p50 149.1/235.2/408.4 ms → 108.0/193.6/365.4 ms. Backend unchanged. 240/240 requests succeeded. |
| AWS A/B | Cache-hit p50 98.26/98.13/97.94 ms → 1.16/2.15/3.54 ms at c1/c2/c4; system throughput 13.81/27.72/55.26 → 311.49/894.77/955.64 req/s. Unique p50 149.85/231.69/403.91 → 108.02/190.42/360.00 ms; throughput 7.44/9.11/10.26 → 9.22/10.48/11.10 req/s. Backend p50 unchanged within 1.2%. No failures. |
| Decision | Accepted. The cache-hit failure mode is removed, unique requests improve, backend compute is unchanged, and correctness checks pass. |
| Artifacts | `benchmarks/results/2026-11-02-aws-http-nodelay.json`, `docs/aws-http-nodelay.md`, raw runs `infer-ff9ac124eb8e79ea`, `infer-2063d463870454ba`. |
| Cost | Two new runs: $0.003273 + $0.003450 = $0.006723. Nine-run project estimate $0.031510. Cleanup independently verified. |
| Limits | Single host, synthetic padded inputs, closed-loop client sharing CPU; high cache-hit throughput is client-bound. Baseline and changed AWS sessions differ. No p99 claim. |
| Status | Accepted and documented. |

## Cycle 3 — input shape alignment (accepted correctness fix)

| Field | Record |
| --- | --- |
| Hypothesis | `InferenceEngine::batchPredict` appends every input element and pads only short inputs. An oversized input therefore occupies more than one model-sized slot and shifts every later sample in the batch, producing wrong outputs while still returning the expected shape. Input preparation itself may be too small to optimize. |
| Baseline | Accepted cycle-2 source (thread-tuned, TCP_NODELAY). |
| Preparation measurement | A standalone C++ probe measured flatten/pad at 8/18/39/989 µs for batch 1/2/4/32, against 86–345 ms whole-batch backend time. The performance candidate was rejected. |
| Reproduction | A direct `batchPredict({oversized, small})` call produced a small-sample output that differed from its single-request reference by 10.1369. The new `tests/test_batch_shapes.cpp` failed with “sample after an oversized input was misaligned” before the fix. |
| Change | Cap each sample at the model’s per-sample element count, pad short samples, and truncate oversized samples. Add guards for an empty output tensor and an output batch dimension that differs from the input batch. |
| Verification | `test_batch_shapes` passes with the real model; the default suite remains green; all opt-in worker/gateway tests pass. No AWS run was needed because the defect is host-independent and the benchmark workload uses short inputs. |
| Decision | Accepted as a correctness fix. It is not a performance claim. |
| Cost | $0; local only. |
| Artifacts | `tests/test_batch_shapes.cpp`, `src/inference_engine.cpp`, `CMakeLists.txt`; local output `/tmp/opencode/build/test_batch_shapes.log`. |

## Cycle 4 — batch deadline 20 ms to 10 ms (accepted)

| Field | Record |
| --- | --- |
| Hypothesis | The fixed 20 ms batching deadline is an avoidable low-load queueing tax. A 10 ms deadline should lower queue wait and unique-request latency while still filling the measured 2- and 4-request batches. |
| Baseline | Accepted cycle-3 source with the 20 ms default; AWS runs `infer-ff9ac124eb8e79ea`, `infer-2063d463870454ba`. |
| Local sweep | 20/10/5/2 ms at c1/c2/c4: 10 ms p50 97.3/183.3/356.0 ms vs 20 ms 107.0/193.2/363.5 ms; throughput 10.23/10.86/11.14 vs 9.30/10.33/10.91 req/s; batch sizes unchanged. High-concurrency: 5 ms lost batch size at c32 (mean 10.1 vs 20), while 10 ms kept batch size 20 and throughput within noise. |
| Change | Default `BATCH_DEADLINE_MS` 10 ms, environment override, effective deadline logged, and an opt-in default/override test. |
| Workload | Same 360-request `c6i.large` A/B; two runs per variant. |
| Acceptance rule | Lower unique p50 without a material throughput regression, unchanged cache-hit behavior, no failures, and verified cleanup. |
| AWS A/B | Unique p50 108.0/190.4/360.0 → 98.5/181.6/351.5 ms at c1/c2/c4; throughput 9.22/10.48/11.10 → 10.09/10.93/11.36 req/s. Queue-wait p50 ~20.06 → ~10.06 ms. Repeated p50 1.16/2.15/3.54 → 1.15/2.17/3.69 ms (noise). Backend unchanged. No failures. |
| Correctness | Default suite 33 passed / 3 intended skips; opt-in test verifies `Batch Timeout: 10ms` and `BATCH_DEADLINE_MS=20`; all five opt-in worker/gateway tests pass; `ctest` batch tests pass. |
| Decision | Accepted as a bounded latency tuning change. The gain is smaller than cycles 1–2 and is a tradeoff, not a universal optimum. |
| Artifacts | `benchmarks/results/2026-11-02-aws-batch-deadline.json`, `docs/aws-batch-deadline.md`, raw runs `infer-f6dfdf8dd788978c`, `infer-d1070c813d774208`; local sweeps under `benchmark-aws/runs/local-cycle4-*`. |
| Cost | Two new runs: $0.003197 + $0.003222 = $0.006419. Eleven-run project estimate $0.037929. Cleanup verified. |
| Limits | Single host, synthetic padded inputs, closed-loop client sharing CPU; baseline/changed sessions differ; high-concurrency evidence is local only. |

## Cycle 5 — gateway response passthrough (accepted)

| Field | Record |
| --- | --- |
| Hypothesis | The gateway parses each successful ~10 KB worker response into JSON and immediately dumps it, although it does not transform the response. Removing that round trip should reduce gateway CPU and improve cache-hit throughput on the shared two-vCPU host. |
| Baseline | Accepted cycle-4 source with JSON parse/dump; AWS runs `infer-f6dfdf8dd788978c`, `infer-d1070c813d774208`. |
| Local A/B | Cache-hit p50 1.57/2.09/4.18 → 1.11/1.50/2.70 ms; throughput 141/868/896 → 147/1,211/1,229 req/s. Unique p50 and backend unchanged. 240/240 requests succeeded. |
| Change | `tryNode`/`routeRequest` return the raw successful body; the handler sets it directly. Non-200/error/retry behavior is unchanged. |
| Tradeoff | The gateway no longer validates the worker's 200 JSON body. The worker is trusted; the benchmark client still checks response shape and request association. |
| AWS A/B | Cache-hit p50 1.15/2.17/3.69 → 0.85/1.51/2.80 ms; throughput 327.7/884.7/953.1 → 367.9/1,246.8/1,264.4 req/s. Unique p50 −0.2 to −0.7%; throughput +0.1 to +0.8%. No failures. |
| Correctness | Gateway test compares direct worker output with forwarded output; default suite 33 passed / 3 intended skips; all six opt-in tests pass; `ctest` batch tests pass. |
| Decision | Accepted as a focused gateway CPU optimization. The cache-miss path is unchanged; the gain is confined to the response path. |
| Artifacts | `benchmarks/results/2026-11-02-aws-gateway-passthrough.json`, `docs/aws-gateway-passthrough.md`, raw runs `infer-a5f5bc6de001b9ae`, `infer-f8f48cbffd6ffd98`; local A/B under `benchmark-aws/runs/local-cycle5-final-2026-11-02/`. |
| Cost | Two new runs: $0.003222 + $0.003552 = $0.006774. Thirteen-run project estimate $0.044703. Cleanup verified. |
| Limits | Single host, synthetic padded inputs, closed-loop client sharing CPU; high cache-hit throughput is client-bound; baseline and changed sessions differ. |

## Cycle 6 — multi-host image capacity and routing (accepted for the tested scope)

| Field | Record |
| --- | --- |
| Hypothesis | The single-host cache-hit results do not measure inference capacity. On an isolated multi-host topology with real images and a cold cache, least-loaded routing should remove the hot-worker bottleneck that consistent hashing creates for a fixed image set, and the 80% CPU limit should cap the sustained rate below the latency-only knee. |
| Baseline | Accepted cycle-5 source plus the image benchmark harness; 1/2/4/8 `c6i.large` workers, one `c6i.large` gateway, one `c6i.xlarge` load generator; 500-image ImageNet subset; ResNet50-v2-7; raw float32 transport. |
| Change | Add `ROUTING_POLICY=hash\|least_loaded\|bounded_load` with per-node in-flight counters, remove per-request gateway logging, add per-level service restart and fresh-cache health checks, and cap the effective batch size to the model's static batch dimension. |
| Workload | Cold cache (`CACHE_CAPACITY=1`), unique 500-image levels at concurrency 1/4/16/64/128 (and 256 for 4/8 workers), 500 requests per level; 5-minute holds; 60-second worker-kill test at 40 requests/s. |
| Accuracy | 500 labeled images per model: ShuffleNetV2-10 72.2/90.6, MobileNetV2-7 71.8/90.8, ResNet18-v2-7 71.6/90.6, ResNet34-v2-7 76.2/92.2, ResNet50-v2-7 78.6/94.2 (top-1/top-5). |
| Latency/error capacity | ResNet50 least-loaded 1/2/4/8 workers: 13.54/21.92/45.66/86.07 requests/s. 8-worker hash baseline: 31.44 requests/s; least-loaded is 2.7× higher and cuts p95 at concurrency 16 from 1,074 ms to 194 ms. |
| Full-SLO sustained | 1/2/4/8 workers at 7/12/24/45 requests/s for 300 s each: 0 errors, p95 102–104 ms, 0 cache hits, worst worker mean CPU 55.8–67.5%, worst p95 CPU 60–69%, maximum sample 77%. The 8-worker 45 requests/s run is 13,500 requests with p95 104.0 ms and p99 105.8 ms. |
| CPU boundary | At 8 workers, 50 requests/s is marginal (worst mean 67.0%, p95 80%, max 82%); 60 requests/s fails (worst mean 86.3%, p95 95%); 84 requests/s passes latency/errors but uses 94.8% CPU. Do not report the 84 requests/s run as a full-SLO result. |
| Failure and consistency | Worker kill at 40 requests/s: 2,400 requests, 0 failures, p95 114.0 ms; 19 of 20 labels were served by more than one worker; 0 top-1 conflicts; maximum output difference 0.0. |
| Transport | Binary vs Python JSON at 2 workers: 21.41 vs 12.98 requests/s at concurrency 4; 23.05 vs 11.11 at concurrency 16. The JSON client reached 101% CPU, so the gap includes client serialization. |
| Correctness | 53 default tests passed / 10 intended skips; 10/10 benchmark load tests; routing integration test passes with two fake workers; static batch-1 model test passes; source-delivery tests include the new headers and engine backend. |
| Decision | Accepted for the tested workload. Least-loaded routing and the static batch cap are real improvements. The CPU limit, not latency, sets the sustained capacity. Production readiness remains blocked by TLS/auth, rate limiting, graceful shutdown, observability, and model versioning. |
| Artifacts | `docs/autotune-benchmark.md`, `benchmarks/results/2026-11-02-autotune-summary.json`; raw runs listed in the report. |
| Cost | New campaign estimate $3.267; grand total $3.312 of the $10 budget. Cleanup verified: 0 active instances, 0 tagged security groups, 0 tagged volumes. |
| Limits | 500-image subset, coarse closed-loop sweep, 5-minute holds, one region, Python JSON client, no spare worker in the failover test, no process-pool or gateway-batching implementation. |

## Cycle 7 — production hardening (accepted)

| Field | Record |
| --- | --- |
| Hypothesis | The remaining material gaps are operational, not throughput: queued handlers can hang on shutdown, malformed JSON becomes 500s and can trip circuit breakers, failover has no regression test, and CI does not build the real C++ targets. |
| Baseline | Cycle-6 source at `cba852c`; existing gateway, cache, batching, and ORT backend. |
| Change | Bounded batch-processor stop; graceful SIGINT/SIGTERM for worker and gateway; JSON request validation with 400; loopback failover test; real C++ CI job with pinned ONNX Runtime. Idle keep-alive timeout lowered to 2 seconds so shutdown does not wait for the library default of 5 seconds. |
| Shutdown | New worker and gateway tests send SIGTERM and require exit code 0 within 5 seconds. The batch test requires queued work to fail, new work to be rejected, and `stop()` to be idempotent. |
| Validation | Unit test covers missing/empty/oversized IDs, missing/empty/non-numeric/non-finite inputs. Real HTTP test covers gateway and worker 400 responses; valid request remains 200; circuit-breaker failures stay 0. |
| Failover | Two local workers and one gateway; kill the worker that owns a hashed input; same input returns 200 from the healthy worker and records a breaker failure. |
| CI | New `cpp` job initializes pinned submodules, verifies the ORT 1.23.2 tarball SHA-256, builds real targets, runs `ctest`, and runs the real worker/gateway tests. |
| Verification | Full Python suite: 70 passed, 7 intended skips. `ctest`: 4/4 passed. Real worker/gateway tests: 11 passed. |
| Decision | Accepted for the tested loopback and CI scope. This closes the safe-operation gaps; it does not make the service production-ready. |
| Artifacts | `docs/production-hardening.md`, `tests/test_batch_timings.cpp`, `tests/test_graceful_shutdown.py`, `tests/test_api_validation.py`, `tests/test_gateway_failover.py`, `.github/workflows/ci.yml`. |
| Cost | Local and CI only; $0. |
| Limits | Loopback only; no long in-flight drain test; one worker-loss injection; no TLS/auth/rate limiting; CI result requires a pushed branch run. |

## Cycle 8 — overload admission control (accepted for the tested local scope)

| Field | Record |
| --- | --- |
| Hypothesis | Under sustained offered load above capacity, an unbounded gateway queue turns excess load into multi-second accepted latency. A global in-flight limit should protect accepted requests and reject excess work with a retryable 503. |
| Baseline | Cycle-7 source at `b0bf9b3`; gateway had per-worker in-flight routing but no global admission limit. |
| Change | Add `ADMISSION_MAX_INFLIGHT` (default 0, disabled); use a compare-and-swap admission counter; ignore negative or out-of-range configuration with a warning; return HTTP 503 with `Retry-After: 1`; expose admitted, rejected, and in-flight counters in `/stats`; extend the local runner to start multiple workers, pass the admission limit, record binary hashes, and use one rate-mode run. |
| Workload | Local AMD Ryzen 7 5800H, 16 logical CPUs; 2 workers, 2 ORT threads each; ResNet50-v2-7; 500 unique images; cache capacity 1; binary transport; open-loop 25, 50, and 100 requests/s for 10 s; 128 connection limit. |
| Verification | Admission tests cover `/infer`, `/infer_tensor`, negative and out-of-range configuration, and release after a malformed request; the rejected request never reaches the worker and the counters remain consistent. Full Python suite 75 tests (0 skips with aiohttp; 7 optional skips without); CTest 4/4. |
| Results at 25 req/s | Baseline: 250/250 accepted, p95 605.5 ms. Admission 7: 240/250 accepted, 10 controlled 503s, p95 326.2 ms. The limit is close to the natural in-flight count here; the comparison is dominated by run-to-run variance. |
| Results at 50 req/s | Baseline, three runs: 500/500 accepted; wall 18.4/17.3/15.4 s; p95 8,357/7,264/5,560 ms. Admission 7, three runs: 286/260/272 accepted; 214/240/228 controlled 503s; p95 376.3/376.3/372.0 ms; p99 388.8/399.0/386.8 ms; accepted throughput 28.1/25.6/26.8 req/s. |
| Results at 100 req/s | Baseline: 1,000/1,000 accepted; wall 35.5 s; p95 24,371 ms. Admission 7: 296/1,000 accepted; 704 controlled 503s; p95 387.3 ms; p99 402.5 ms. |
| Decision | Accepted for the tested local scope. The limit protects accepted latency at 2× and 4× the operating point; it does not increase capacity. Controlled 503s are reported separately from uncontrolled 5xx; the load generator does not retry. |
| Artifacts | `docs/overload-protection.md`, `benchmarks/results/2026-11-04-overload-protection.json` (contains gateway and worker SHA-256), `tests/test_admission_control.py`. |
| Cost | Local only; $0. |
| Limits | Static limit; local hardware; two workers; three 10-s runs at 50 req/s and one each at 25 and 100; client shares CPU; worker CPU not collected; no gateway `/health`; no client backoff; no TLS/auth/per-user quotas; no AWS or production claim. |

## Stop condition and future work

Cycle 6 closes the multi-host capacity item for the tested workload. The remaining constraints are production concerns, not single-variable performance candidates: TLS and authentication, rate limiting and backpressure, graceful shutdown, metrics export, model versioning, multi-AZ replacement, and a long-duration soak.

Future work that would justify another cycle: a process-pool or multi-process-per-host comparison; gateway-side batching; a CPU-aware autoscaler; a longer hold with memory and file-descriptor tracking; a multi-AZ failover with spare capacity; and a compiled JSON client to isolate server-side transport cost. Each requires a new baseline, a one-variable change, and its own correctness and cost plan.

## Checkpoint rules

- After each accepted change, update this ledger, the README/results, the cost ledger, and the resume only with verified work.
- Keep the previously accepted source as the next baseline. Do not label the pinned upstream as the current baseline.
- Do not commit or push without checking the exact diff, tests, raw artifacts, secrets, and AWS cleanup.
