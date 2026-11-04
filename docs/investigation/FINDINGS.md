# Investigation record

Status: **complete for cycles 1–12.** The continuous cycle ledger is [EXPERIMENTS.md](EXPERIMENTS.md); the current candidate queue is exhausted within the authorization and budget scope. Cycle 7 closed the safe-operation gaps found after the image benchmark. Cycle 9 validated admission control on a separate multi-host AWS topology. Cycle 10 added adaptive admission, Prometheus metrics, and a retry-aware client. Cycle 11 added a two-gateway HA proxy and failover test. Cycle 12 validated an internal AWS ALB with two gateways and measured a 16.6 s health-check failover window. Independent verification of the admission change is recorded in the PR review; the AWS run is a bounded benchmark, not a production deployment.

## Context and scope

- Date: 2026-11-02. Investigator: AI agent working for the repository owner.
- Repository: `distributed-inference-engine-cpp` (fork of `AbhiramDodda/distributed-inference-engine-cpp`).
- Initial revision: `9c7ab13` (`main`). The timing instrumentation, benchmark workflow, and backend-selection commits were already in history; only `docs/investigation/` was untracked. The worktree was not cleaned, reset, or committed.
- Initial `git status --short --branch`: `## main...origin/main`, `?? docs/investigation/`. No staged or modified tracked files before this investigation.
- Environment: Ubuntu, Linux 7.0.0-34-generic, 16 logical CPUs (AMD Ryzen 7 5800H, 8 cores with SMT), 61 GiB RAM. Tools present: `python3` 3.14.4, `cmake` 4.2.3, `ctest`, `g++` 15.2.0, `aws` CLI, `perf`. Docker is present. ONNX Runtime 1.23.2 runtime is installed system-wide, but C++ headers were absent; local probes used the pinned ORT 1.23.2 release tarball in `/tmp/opencode`, not a system installation. The project's `external/` gitlinks were empty.
- Scope: one evidence-supported performance improvement, correctness checks, a fair before/after comparison where feasible, and a portfolio/resume handoff. No production, distributed-scaling, GPU, or accuracy claims. No commits, pushes, or publication.
- Safety: preserve all existing work; do not stop unrelated processes (port 8000 is used by an unrelated `job-outreach` process, so probes used 18xxx ports). AWS spending ceiling is $10 total; the cost ledger records the current spend.

## Request path and measurement boundaries

`load.py` client -> gateway `POST :8000/infer` -> input-hash consistent routing -> circuit breaker -> pooled keep-alive `httplib::Client` -> worker `POST /infer` -> worker cache lookup -> batch queue -> `BatchProcessor` deadline/size flush -> `WorkerNode::processBatch` (copy inputs) -> `InferenceEngine::batchPredict` (flatten, pad, ORT `Session::Run`, split outputs) -> JSON response -> gateway -> client.

Timers:

- `queue_wait_us`: from enqueue to the start of the batch callback. Excludes mutex acquisition before enqueue, input copying in the batch processor, backend work, and response handling.
- `batch_execution_us`: wall time of the whole `engine_->batchPredict` call, repeated for every request in the batch. Includes backend mutex, input flatten/pad, ORT `Session::Run`, and output splitting. It is not pure model compute and must not be summed across requests.
- `inference_time_us`: legacy field. On a cache hit it is the measured cache lookup; on a miss it is `batch_execution_us` divided by batch size.
- End-to-end client latency includes HTTP connection handling, gateway routing/logging, worker HTTP, queueing, backend, and response serialization. It is not directly comparable to `batch_execution_us`.

Source and artifact identities: model `models/resnet50-v2-7.onnx`, SHA-256 `79102261eb6e5fd7af5d27f41316293e388c5cb691e5d25bfb035c4f64fefe31`; pinned benchmark base revision `c4d8a08b72df810ffad0dce6a993cecae22494bb`; ORT release 1.23.2 tarball verified against SHA-256 `1fa4dcaef22f6f7d5cd81b28c2800414350c10116f5fdd46a2160082551c5f9b`; cpp-httplib v0.14.3 and nlohmann/json v3.11.3 pinned by the run guide.

## Checks and experiments

| Command or activity | Environment | Result, count and evidence path | Limitation |
| --- | --- | --- | --- |
| `python3 -m unittest discover -s tests -v` | local | Initial run: 31 passed, 1 skipped. Final run: 33 passed, 3 skipped; the skips are the opt-in real-worker/gateway classes (`BENCH_WORKER_BIN`/`BENCH_GATEWAY_BIN`/`BENCH_MODEL` not set in that run). | The default suite does not exercise the real model. |
| `bash -n benchmark-aws/remote.sh` | local | Syntax OK. | Syntax only. |
| Retained instrumented AWS run analysis (`benchmark-aws/runs/infer-8bb17685b18e63a4/results/requests.jsonl`) | prior AWS `c6i.large`, 2 vCPU, 4 ORT intra-op threads | Unique inputs: batch 1 backend p50 197 ms, batch 2 372 ms (186 ms/request), batch 4 660 ms (165 ms/request); queue p50 ~20 ms. Repeated inputs: cache-hit end-to-end p50 ~98 ms despite sub-millisecond direct cache lookups. | 60 requests per level, closed loop, client shares CPU. Batch time is whole-batch. |
| Local HTTP probe, direct worker vs gateway, persistent keep-alive | local, 16 CPU | Direct worker cache hit p50 0.4–0.7 ms; through the unmodified gateway p50 ~83 ms with values quantized near 1, 42, and 84 ms. | Probe, not the final benchmark. |
| Local gateway/worker `set_tcp_nodelay(true)` probe | local, 16 CPU | Worker server + gateway server + gateway client nodelay: end-to-end cache-hit p50 0.8 ms vs 82.9 ms unmodified. | Separate HTTP-layer issue; not selected as the single change. |
| Local ORT intra-op thread probe under `taskset -c 0,1` (2 logical CPUs, like a 2-vCPU host) | local, pinned ORT 1.23.2, 10 unique requests per setting | Backend p50: 1 thread 91 ms, 2 threads 85 ms, 3 threads 146 ms, 4 threads 164–197 ms. End-to-end through gateway: 4 threads p50 233 ms, 2 threads p50 150 ms. | CPU affinity emulation, not AWS; AMD SMT, not Xeon 8375C. ORT's default (`0`) escapes process affinity and is not representative. |
| Local matched A/B, baseline HEAD (4 threads) vs changed source with `ORT_INTRA_THREADS=2`, same gateway/model/workload, `taskset -c 0,1` | local, 30 unique requests per concurrency level | Baseline: c1 p50 229 ms / 4.32 req/s, c2 384 ms / 5.19 req/s, c4 635 ms / 6.16 req/s; backend p50 180/331/572 ms. Changed: c1 149 ms / 7.38 req/s, c2 234 ms / 9.04 req/s, c4 404 ms / 10.08 req/s; backend p50 88/173/341 ms. 180/180 requests succeeded; batch sizes matched. Evidence: `benchmark-aws/runs/local-investigation-2026-11-02/`. | Affinity emulation and forced override; not the AWS result. |
| AWS matched A/B, two runs per variant, alternating order (baseline, changed, changed, baseline) | `c6i.large`, us-east-1, 60 requests/level, 360 requests/run | Unique p50 baseline mean 237.9/424.3/701.5 ms vs changed 149.9/231.7/403.9 ms at c1/c2/c4; throughput 4.11/4.65/5.56 vs 7.44/9.11/10.26 req/s. Repeated p50 stayed ~98 ms and throughput ~13.5/27.6/55.3 req/s. No failures. Changed worker log: `ORT intra-op threads: 2 (hardware_concurrency=2)`. Backend p50 (4-thread prior instrumented run vs changed): 196.0/371.6/659.8 ms vs 86.2/168.9/338.5 ms. Evidence: `benchmark-aws/runs/infer-d4522a282d0dcc42`, `infer-1f39a2ac8519f743`, `infer-0601374edef6f33d`, `infer-ca8db4050033ae08`; sanitized aggregate `benchmarks/results/2026-11-02-aws-thread-tuning.json`. | 60 samples/level; client shares CPU; baseline backend session differs; single host; p99 unstable. |

## Hypotheses

| Candidate cause | Supporting and opposing evidence | Status | Next discriminating check | Cost and risk |
| --- | --- | --- | --- | --- |
| ORT intra-op thread oversubscription on a 2-vCPU host | `InferenceEngine` hardcodes 4 intra-op threads. Prior AWS host had 2 vCPUs. Local 2-CPU probe: 4 threads 164–197 ms backend vs 2 threads 85 ms; 4-thread end-to-end 233 ms vs 2-thread 150 ms. Opposing: local CPU is AMD and affinity-limited; cloud behavior may differ. | **Supported, selected** | Matched AWS baseline (4 threads) vs changed source (default `min(4, hardware_concurrency)` = 2) on `c6i.large`, same workload. | Low code risk; verify output parity. |
| HTTP keep-alive Nagle / delayed-ACK stalls | Cache hits are ~98 ms end-to-end through the gateway but 0.4–0.7 ms direct. Latencies are quantized near 1/42/84 ms. `CPPHTTPLIB_TCP_NODELAY` defaults false; three socket paths (worker server, gateway server, gateway client) each add a stall. Enabling nodelay on all three reduced cache-hit p50 to 0.8 ms. | **Reproduced, not selected** | Would need a separate change in `src/gateway.cpp` and a separate A/B. | Low risk but targets HTTP configuration; would confound a single-change comparison. Recorded as a follow-up. |
| Input copy/padding/allocation | `handleInfer` copies JSON to `vector<float>`, the batch processor copies into `requests`, the worker copies into `inputs`, and `batchPredict` flattens/pads into `batch_input`; synthetic 3-float inputs are padded to 150,528 floats. Opposing: total copies are small relative to ~100–200 ms backend time; backend timing already includes the last flatten/pad. | Plausible, lower impact | Microbenchmark preparation alone, or remove copies and measure. | Low risk, likely small gain. |
| Batching policy (10 ms default, max 32) | Cycle 4 sweep: 20 ms queue wait vs 10/5/2 ms. At c1/c2/c4, 10 ms lowered p50 and raised throughput with the same batch sizes; at c16/c32, 10 ms kept large batches and throughput better than 5 ms. | **Selected in cycle 4** | AWS A/B with 10 ms against the accepted 20 ms baseline. | Low code risk; latency/throughput tradeoff documented. |
| Gateway/HTTP overhead independent of Nagle | Gateway logs every request with `std::endl`; route path does JSON parse/dump and a sync POST. Opposing: nodelay probe removed most of the cache-hit latency without touching logging; direct worker is fast. | Partly explained by Nagle | Profile gateway with nodelay enabled; measure logging overhead separately. | Low. |
| Cache behavior | Repeated workload is ~100% hits after the first request; direct worker cache hits are sub-millisecond. Cache is not the bottleneck. | Refuted as bottleneck | None needed. | — |
| Measurement artifacts | Closed-loop client, 60 requests/level, shared CPU, p99 unstable. Cache-hit latency included client/server keep-alive quantization. | Confirmed as a limitation | Report distributions and sample counts; avoid p99 claims. | Affects interpretation, not the selected backend change. |
| Model compute itself | ORT `Session::Run` dominates `batch_execution_us`; thread count changes it by ~2× on a 2-CPU budget. | Supported as part of thread hypothesis | Cloud A/B with timing fields. | — |

## Correctness, reliability and security findings

- No crash or request/response pairing failure was reproduced. The final default suite had 33 passes and 3 opt-in skips; all six opt-in real-worker/gateway tests passed when the binaries and model were supplied.
- **Oversized-input batch alignment (reproduced and fixed in cycle 3):** `batchPredict` appended every input element and padded only short inputs. An oversized first input shifted the next sample, changing its output by 10.1369 while the response shape stayed valid. The engine now caps each sample at the model’s per-sample size, truncates the excess, and checks the output batch dimension. `tests/test_batch_shapes.cpp` covers the regression with the real model.
- The selected change alters only the ORT intra-op thread count. It can change floating-point reduction order; output parity must be checked with fixed inputs and a stated tolerance.
- The HTTP delayed-ACK finding is a latency issue, not a correctness issue. It was implemented and accepted as cycle 2; see [EXPERIMENTS.md](EXPERIMENTS.md) and [the HTTP nodelay report](../aws-http-nodelay.md).
- No security issue was reproduced. Probes bound to loopback and 18xxx ports; no service was exposed. No credentials or account identifiers were printed.
- AWS cleanup from the prior runs was verified previously; no new AWS resources existed at the start of this session.

## Selected approach and analysis

- **Recommended change:** replace the hardcoded `SetIntraOpNumThreads(4)` with a capacity-aware default: use `min(4, hardware_concurrency)` with a floor of 1, and allow an explicit `ORT_INTRA_THREADS` override for testing/operations. Log the effective count. On the benchmark `c6i.large` (2 vCPU), the default becomes 2 threads instead of 4.
- **Why:** the instrumented AWS run shows the backend call dominates unique-request latency; the local 2-CPU experiment shows 4 threads is ~2× slower than 2 threads, consistent with oversubscription. This is a one-line behavioral change with a clear rollback and no protocol or API change.
- **Simpler alternative / no-change:** keep 4 threads. It preserves current behavior but leaves the measured 2-vCPU oversubscription in place. TCP_NODELAY was kept as a separate cycle-2 change because it targets HTTP configuration, not the backend; it is now accepted with its own matched A/B.
- **Expected effect:** approximately 2× lower whole-batch backend time on a 2-vCPU host, and ~30–40% lower unique-request end-to-end latency if HTTP overhead is unchanged. Cache-hit latency should be unchanged (it does not enter ORT).
- **Before/after data, variability, correctness, errors, confounders, and conclusion:** AWS unique-input p50 improved 37.0%/45.4%/42.4% at concurrency 1/2/4; throughput improved 80.9%/95.7%/84.5%. Backend p50 improved 56.0%/54.5%/48.7% against the prior 4-thread instrumented run. Repeated/cache-hit p50 and throughput were unchanged, as expected. Two runs per variant were stable (changed c1 p50 149.850/149.858 ms; c4 398.715/409.098 ms), and alternating order did not reverse the effect. All 360 requests per run succeeded. Confounders: the client shares the CPU; the backend baseline is a different session; each level has 60 samples; p99 is unstable. Conclusion: the hypothesis is supported for this single-host synthetic workload; no production or multi-host claim is made.
- **Correctness tests and comparison design:** unit/integration checks of the effective thread count and output parity between 2 and 4 threads; then matched AWS `c6i.large` baseline (pinned source, 4 threads) vs changed source (default 2 threads), same model, runtime, workload, cache state, warm-up, request counts and concurrency. Report per-level p50/p95, throughput, errors, and run-to-run variation. Do not claim p99 from 60 samples.
- **Remaining budget:** seven runs were estimated at $0.024787 of the $10 ceiling; see the cost ledger for the current total before tax, transfer, or billing adjustments. Cost Explorer reported $0.000000 for the period at the time of writing, but billing data can be delayed, so the ledger estimate is the conservative figure. Each new run retains logs locally and verifies cleanup before another paid operation.
- **Intended files:** `src/inference_engine.cpp` (thread selection and log), `benchmark-aws/controller.py` and `benchmark-aws/verify_source.py` (delivery allowlist), `tests/test_instrumented_worker.py` (opt-in thread/output parity test), `tests/test_source_delivery.py` (allowlist regression), `benchmark-aws/README.md` and `docs/timing-instrumentation.md` (delivery documentation), `docs/investigation/FINDINGS.md` (evidence), plus the final README/results update and resume.
- **Rollback:** remove the capacity-aware branch and restore `SetIntraOpNumThreads(4)`; no data or API migration.

## Evidence and limits

- **Implemented:** `src/inference_engine.cpp` now selects `min(4, hardware_concurrency)` intra-op threads (floor 1), supports `ORT_INTRA_THREADS`, and logs the effective count. The AWS delivery allowlist now includes that file; the controller and verifier check for the thread selection, and the source-delivery tests cover it.
- **Tested, with exact outcomes:** `python3 -m unittest discover -s tests -v` → 33 tests passed, 3 skipped (the opt-in real-worker/gateway classes). All six opt-in tests passed with the changed binaries and model, including batch-deadline default/override, output parity between 2 and 4 threads within `1e-4`, and the gateway cache-hit regression guard. `ctest` in a temporary build ran `batch_timings` and `batch_shapes`; both passed, and `test_batch_shapes` passed with the real model. Local matched A/B above: 180/180 requests succeeded; c1/c2/c4 p50 improved 35%/39%/36%; throughput improved 71%/74%/63%; backend p50 roughly halved.
- **Reviewed and by whom:** labeled self-review performed against the actual diff, tests, raw AWS summaries, cleanup records, cost ledger, and rendered resume. Independent review was not performed for that cycle; the later admission change received an independent verifier review. Self-review found no critical or important defect; the remaining items are the stated limitations.
- **Not tested or blocked:** no current blocker. Cycle 2 (HTTP nodelay) is accepted and documented in [EXPERIMENTS.md](EXPERIMENTS.md). Model accuracy was not assessed because the inputs are synthetic and the model has no ground-truth labels in this workload. The resume's independent visual review is the owner's responsibility.
- **Risks and next steps:** the performance result is single-host and synthetic; do not generalize to production or multi-host. The backend baseline is from a different session than the changed runs. Cycle 5 candidates are ranked in [EXPERIMENTS.md](EXPERIMENTS.md); the next bounded investigation is gateway logging/JSON overhead. Run `python3 docs/investigation/audit.py` and review the diff before any commit.

## Cycle 2 follow-up — HTTP TCP_NODELAY (accepted)

The next cycle isolated the keep-alive delayed-ACK stall and enabled `TCP_NODELAY` on the worker server, gateway server, and gateway worker client. Against the accepted thread-tuned baseline, AWS cache-hit p50 fell from 98.26/98.13/97.94 ms to 1.16/2.15/3.54 ms at concurrency 1/2/4; system throughput rose from 13.81/27.72/55.26 to 311.49/894.77/955.64 req/s. Unique-input p50 fell 27.9%/17.8%/10.9%, and whole-batch backend p50 was unchanged within 1.2%. All 360 requests per run succeeded. Two changed runs cost $0.003273 + $0.003450. Details: [EXPERIMENTS.md](EXPERIMENTS.md), [aws-http-nodelay.md](../aws-http-nodelay.md), and `benchmarks/results/2026-11-02-aws-http-nodelay.json`.

## Cycle 3–4 follow-ups

Cycle 3 measured input flatten/pad at 8/18/39/989 µs for batch 1/2/4/32, so the copy/padding performance candidate was rejected. The same investigation reproduced an oversized-input alignment defect (a following sample changed by 10.1369) and fixed it by capping each sample at the model’s per-sample size; `tests/test_batch_shapes.cpp` covers the regression.

Cycle 4 reduced the default batch deadline from 20 ms to 10 ms with a `BATCH_DEADLINE_MS` override. AWS unique p50 fell from 108.0/190.4/360.0 ms to 98.5/181.6/351.5 ms at c1/c2/c4, throughput rose from 9.22/10.48/11.10 to 10.09/10.93/11.36 req/s, and queue-wait p50 fell from about 20.06 ms to 10.06 ms. The cache-hit workload and backend time were unchanged. Details: [EXPERIMENTS.md](EXPERIMENTS.md), [aws-batch-deadline.md](../aws-batch-deadline.md), and `benchmarks/results/2026-11-02-aws-batch-deadline.json`.

Cycle 5 measured the gateway’s JSON parse/dump of each ~10 KB worker response. Forwarding the successful body unchanged cut AWS cache-hit p50 from 1.15/2.17/3.69 ms to 0.85/1.51/2.80 ms and raised cache-hit throughput from 327.7/884.7/953.1 to 367.9/1,246.8/1,264.4 req/s at c1/c2/c4; unique p50 changed by −0.2 to −0.7% and backend time was unchanged. The gateway now trusts the worker’s 200 body rather than re-validating it, which is documented as a reliability tradeoff. Details: [EXPERIMENTS.md](EXPERIMENTS.md), [aws-gateway-passthrough.md](../aws-gateway-passthrough.md), and `benchmarks/results/2026-11-02-aws-gateway-passthrough.json`.

## Production hardening follow-up (cycle 7)

Cycle 7 addressed operational gaps that the image benchmark exposed:

- `BatchProcessor::stop()` now rejects new work and fails every promise still queued when stop begins, so handlers cannot remain blocked after shutdown. A request already dequeued may complete.
- Worker and gateway block `SIGINT`/`SIGTERM`, run the HTTP listener on a joined thread, stop it, and exit within the tested 5-second bound. The idle keep-alive timeout is 2 seconds.
- `POST /infer` validates the JSON object, `request_id`, and numeric finite `input_data` before routing. Invalid client input returns HTTP 400; valid requests and the 500 internal-error path are unchanged.
- A loopback test kills the worker that owns a hashed input and verifies retry on the healthy worker plus a circuit-breaker failure record.
- CI has a `cpp` job that builds the real worker/gateway with pinned ONNX Runtime and runs `ctest` and the real-model loopback tests.

Evidence: 70 Python tests passed with 7 intended skips; 4/4 CTest cases passed; 11 real worker/gateway tests passed. These are loopback and CI results, not a deployed-network or production-availability claim. See `docs/production-hardening.md`.

## Overload admission follow-up (cycle 8)

- Added opt-in `ADMISSION_MAX_INFLIGHT` at the gateway. It uses a compare-and-swap counter, returns controlled HTTP 503 with `Retry-After: 1` when the global in-flight limit is reached, and exposes admitted/rejected/in-flight counters in `/stats`.
- Negative or out-of-range configuration is ignored with a warning, so a typo disables admission instead of rejecting every request.
- The admission tests cover `/infer`, `/infer_tensor`, negative and out-of-range configuration, prove the rejected request never reaches the worker, and check release after a malformed request.
- Local 2-worker, 50 req/s open-loop runs: baseline accepted all requests but p95 was 5.6–8.4 s; admission limit 7 accepted 260–286 of 500 with p95 372.0–376.3 ms and p99 386.8–399.0 ms. The rest were controlled 503s. At 100 req/s, the baseline p95 was 24.4 s; admission accepted 296 of 1,000 with p95 387.3 ms and 704 controlled 503s.
- This protects accepted latency; it does not add capacity. The result is local, uses static limits, and has no client retry policy. See `docs/overload-protection.md`.

## AWS admission validation (cycle 9)

- Ran the paired matrix on AWS: 1 `c6i.large` gateway, 2 `c6i.large` workers, 1 `c6i.xlarge` load generator in `us-east-1`, `least_loaded`, 25/50/100 req/s, 10 s, 3 repeats.
- At 25 req/s: baseline p95 1,511–1,581 ms; admission 7 p95 324–331 ms with 16 controlled 503s (6.4%).
- At 50 req/s: baseline p95 12,431–21,549 ms; the high end includes five gateway-worker retries, and the other two runs were 12.4–12.7 s. Admission 7 p95 339–340 ms with 263–266 controlled 503s (52.6–53.2%).
- At 100 req/s: baseline p95 28,829–29,209 ms and 122–134 client timeouts; admission 7 p95 343–344 ms with 765 controlled 503s (76.5%) and no timeouts.
- Both workers reached about 100% of their 2 vCPUs; gateway CPU stayed below about 15%. Admission bounds the queue, not capacity. Cost $0.1326; cleanup verified.
- See `docs/overload-protection.md` and `benchmarks/results/2026-11-04-aws-admission-validation.json`.

## Adaptive admission and metrics (cycle 10)

- Added a Prometheus `/metrics` endpoint, an adaptive admission controller, and a retry-aware load client. Static mode remains the default.
- At 25 req/s: static 7 accepted 250/250 with p95 158.2 ms; adaptive accepted 250/250 with p95 362.1 ms and stayed at the ceiling of 12.
- At 50 req/s: static 7 accepted 264/500 median with p95 397.3 ms; adaptive accepted 297/500 median with p95 534.5 ms and final limit 6–7. Adaptive final observed p95 was 351–385 ms; the whole-run p95 includes the convergence ramp.
- At 100 req/s: static 7 accepted 277/1,000 with p95 400.9 ms; adaptive accepted 266/1,000 with p95 556.6 ms and final limit 7; final observed p95 was 398 ms.
- Retry at 100 req/s raised final successes from 266–277 to 320–399, but p95 rose to about 3.3 s because it includes the wait between attempts.
- Adaptive works mechanically and converges near the target, but it did not beat the calibrated static 7 in these 10-second runs. Static 7 remains the default; the metrics surface and retry client are the durable deliverables. Cost $0, local only. See `docs/overload-protection.md` and `benchmarks/results/2026-11-04-adaptive-admission.json`.

## Gateway HA and failover (cycle 11)

- Added a gateway `/health` endpoint, a local aiohttp proxy with health checks and connection-error failover, and a two-gateway orchestration script.
- Three normal/failover/recovery runs at 50 req/s: normal 343/500 median successes (329–352), p95 257 ms (243–258); failover 322/500 (320–343), p95 224 ms (213–250), one proxy failover per run with one expected upstream connection error retried successfully, no client-visible connection failures, observed failover window 0.205–0.257 s; recovery 357/500 (318–358), p95 241 ms (230–280).
- Four per gateway (total 8) gave p95 257 ms; seven per gateway (total 14) gave p95 500 ms. Per-gateway admission limits sum, so the total must fit the two workers.
- The proxy removes the gateway single point of failure in this local test. Admission is still local: the surviving gateway keeps its limit, so total admission capacity drops during failover. A production design needs a shared/global limiter or load-balancer-aware limits.
- Cost $0; local only. See `docs/overload-protection.md` and `benchmarks/results/2026-11-04-gateway-ha.json`.

## AWS internal-ALB HA (cycle 12)

- Ran 2 `c6i.large` gateways, 2 `c6i.large` workers, 1 `c6i.xlarge` load generator, and 1 internal ALB in `us-east-1` at 50 req/s with per-gateway admission 4 (total 8).
- Normal: 232/500 successes, 268 controlled 503s, p95 349 ms, 22.6 req/s. Failover: 455/1,000 successes, 326 controlled 503s, 180 ALB 502s, 39 ALB 504s, p95 340 ms, 18.7 req/s. Recovery: 235/500 successes, 265 controlled 503s, p95 350 ms, 22.8 req/s.
- The ALB marked the terminated gateway unhealthy after 16.576 s; during that window it kept sending requests to the dead target and returned 502/504. The local aiohttp proxy used 1 s checks and connection retry and had no client-visible failover errors.
- The ALB failover works but is slower than the local proxy. A production design needs deregistration/draining, client retry for 502/504, or faster detection. All targets were in one subnet/AZ; admission is per gateway.
- Cost: $0.074941 successful run plus $0.046329 for two setup attempts; cleanup verified. See `docs/overload-protection.md` and `benchmarks/results/2026-11-04-aws-alb-ha.json`.

## Portfolio and repository handoff

- **Upstream work versus the owner's specific contribution:** upstream authored the gateway, cache, batching, and ORT backend. This investigation adds capacity-aware ORT threads, `TCP_NODELAY` on keep-alive paths, input-shape alignment, and a 10 ms batch deadline, plus their tests and evidence; inherited benchmark and instrumentation work is attributed separately.
- **Claim, supporting evidence and measurement limitations:** on a single `c6i.large` with synthetic padded inputs and closed-loop load, ORT 4→2 threads cut unique-input p50 37–45% and raised throughput 81–96%; `TCP_NODELAY` cut cache-hit p50 from about 98 ms to 1.2–3.5 ms and raised unique throughput another 8–24%; gateway response passthrough cut cache-hit p50 another 24–30% and raised cache-hit throughput 12–41%; a 10 ms deadline cut unique p50 another 2–9% and queue wait about 50%. An oversized-input batch-alignment defect was reproduced and fixed. Evidence: `benchmarks/results/2026-11-02-aws-thread-tuning.json`, `2026-11-02-aws-http-nodelay.json`, `2026-11-02-aws-batch-deadline.json`, `2026-11-02-aws-gateway-passthrough.json`, the linked reports, and raw local run directories.
- **Proposed concise README or results update:** added Recent Improvements bullets and linked reports for [thread tuning](../aws-thread-tuning.md), [HTTP nodelay](../aws-http-nodelay.md), [batch deadline](../aws-batch-deadline.md), and [gateway passthrough](../aws-gateway-passthrough.md); updated the cost ledger, delivery docs, and test instructions.
- **Draft resume bullets, limited to verified work:** the resume keeps the two largest performance wins: capacity-aware ORT threads (37–45% unique p50 reduction, 81–96% throughput increase) and the combined HTTP keep-alive/passthrough work (cache-hit p50 about 98 ms → 0.85–2.8 ms, cache-hit throughput another 12–41%), plus tests for thread parity and batch alignment. The smaller 10 ms deadline result is documented in the project report but was not added as a fifth project bullet, to avoid inflating the one-page resume. Do not claim production or multi-host results.
- **Initial and final Git status; pre-existing user changes preserved:** initial `## main...origin/main`, `?? docs/investigation/`; the five changes are committed as `eb7ba33` (thread tuning), `0e6bf8c` (TCP_NODELAY), `b35501d` (input alignment), `d2a49ed` (10 ms deadline), and `e058d9f` (response passthrough), and pushed to `origin/main`. `git ls-remote origin refs/heads/main` reported `e058d9f146d63e1aa81aae616749fb63ae2cb62f` at the last code checkpoint; a final documentation commit follows. The worktree is clean at the final checkpoint. No reset, clean, stash, force push, or history rewrite was used. All pre-existing instrumentation and investigation files were preserved.
- **Purpose of each proposed tracked file and generated artifacts retained locally:** source/tests implement and verify the changes; the runner/docs deliver and explain them; the README and reports publish verified results; the cost ledger records paid runs. Raw run directories remain under ignored `benchmark-aws/runs/`; local probes are under `benchmark-aws/runs/local-*`; no raw logs or credentials are tracked.
- **Checks on the complete diff and untracked files; proposed commit only if appropriate:** `git diff --check` passed; the default Python suite passed 33/33 with 3 intended skips; all six opt-in worker/gateway tests passed; `ctest` passed `batch_timings` and `batch_shapes`; ruff check/format passed; AWS cleanup was independently verified.
- **Commit, push, or publication authorization and actual result, if any:** the user requested that the GitHub repository be kept updated. Five focused commits were pushed to `origin/main`; `git ls-remote origin refs/heads/main` was used to verify the remote reference. No resume or personal file was pushed.
