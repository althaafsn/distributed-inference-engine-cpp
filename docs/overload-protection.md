# Overload protection and admission control

## What changed

The gateway now supports an optional global in-flight limit:

```sh
ADMISSION_MAX_INFLIGHT=7
```

- `0` is the default and keeps the previous behavior.
- When the limit is reached, the gateway returns HTTP `503` with `Retry-After: 1` and a JSON body.
- The limit covers `/infer` and `/infer_tensor`.
- The gateway `/health` and `/stats` endpoints are never rejected. Worker `/health` endpoints are separate from gateway admission.
- `/stats` reports:

```json
"admission": {
  "max_in_flight": 7,
  "in_flight": 0,
  "accepted": 286,
  "rejected": 214
}
```

`accepted` counts requests admitted by the gateway. It includes requests that are later rejected with HTTP 400 for malformed input. The benchmark table below uses “accepted” for HTTP 200 responses; all benchmark inputs are valid, so the two counts match there.

The gateway checks admission before parsing or routing the inference request. A rejected request does not reach a worker. The counter uses a compare-and-swap loop, so it never over-admits and does not reject below the limit because of a temporary increment.

Negative, non-numeric, or out-of-range values for `ADMISSION_MAX_INFLIGHT` are ignored with a warning; admission stays disabled. This prevents a configuration typo from rejecting every request.

## Why

The benchmark showed a throughput plateau and p95 collapse at high concurrency. Without admission, clients queue for seconds or tens of seconds. The service remains “successful” only because every request eventually completes; the latency is not usable.

Admission control makes the tradeoff explicit:

- protect accepted requests,
- reject excess with a retryable status,
- keep the queue bounded.

This is a static limit, not an autoscaler.

## Local experiment

Hardware:

- AMD Ryzen 7 5800H, 16 logical CPUs.
- 2 local worker processes.
- 2 ORT intra-op threads per worker.
- ResNet50-v2-7.
- 500 unique images, cold cache (`CACHE_CAPACITY=1`).
- Binary float32 transport.
- Open-loop offered rate, 10-second duration, 128 connection limit.
- The load generator runs on the same host and shares its CPU.

Commands:

```sh
/tmp/opencode/bench-venv/bin/python benchmark-aws/run_autotune.py \
  --model models/resnet50-v2-7.onnx \
  --data benchmark-aws/data/imagenet500 \
  --output /tmp/opencode/final-ad7-50-r1 \
  --worker-bin /tmp/opencode/cmake-build-hardening/worker_node \
  --gateway-bin /tmp/opencode/cmake-build-hardening/gateway \
  --workers 2 --admission-max 7 \
  --levels 1 --rate 50 --duration 10 --rate-concurrency 128 \
  --mode unique --cache 1 --threads 2 --force
```

All runs in the static local matrix used the same binaries:

- gateway SHA-256: `16168245be980b241966eaabce6cd2832b328e7e329d0061c020b55b52798fbc`
- worker SHA-256: `603f3453730e4b8af1275eee8454db0a3ab1c5b3b8d28ecf99bb0439e1adf794`

## Results

### Passing load: 25 requests/s

| Condition | Accepted | 503 | Accepted p95 | Accepted p99 | Accepted throughput |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline, no admission | 250 / 250 | 0 | 605.5 ms | 775.3 ms | 24.4 /s |
| Admission 7 | 240 / 250 | 10 | 326.2 ms | 365.7 ms | 23.8 /s |

At this rate the limit is close to the natural in-flight count. The admission run shed 10 short bursts (4%). The baseline completed every request, but its tail was slower in this run. Earlier runs at the same rate had different tails, so the 25 requests/s comparison is not a clean admission effect; it is dominated by run-to-run variance.

### Twice the operating point: 50 requests/s

| Condition | Accepted | 503 | Accepted throughput | Accepted p95 | Accepted p99 | Wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline rep 1 | 500 / 500 | 0 | 27.1 /s | 8,357 ms | 8,800 ms | 18.4 s |
| Baseline rep 2 | 500 / 500 | 0 | 28.9 /s | 7,264 ms | 7,541 ms | 17.3 s |
| Baseline rep 3 | 500 / 500 | 0 | 32.5 /s | 5,560 ms | 5,805 ms | 15.4 s |
| Admission 7 rep 1 | 286 / 500 | 214 | 28.1 /s | 376.3 ms | 388.8 ms | 10.2 s |
| Admission 7 rep 2 | 260 / 500 | 240 | 25.6 /s | 376.3 ms | 399.0 ms | 10.1 s |
| Admission 7 rep 3 | 272 / 500 | 228 | 26.8 /s | 372.0 ms | 386.8 ms | 10.2 s |

### Four times the operating point: 100 requests/s

| Condition | Accepted | 503 | Accepted throughput | Accepted p95 | Accepted p99 | Wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 1,000 / 1,000 | 0 | 28.2 /s | 24,371 ms | 25,603 ms | 35.5 s |
| Admission 7 | 296 / 1,000 | 704 | 29.1 /s | 387.3 ms | 402.5 ms | 10.2 s |

All admission-run failures were controlled `503` responses. There were no other 5xx responses and no client timeouts.

At 50 and 100 requests/s, the baseline accepted every request but took 15–35 seconds to drain a 10-second workload. Its p95 was 5.6–24.4 seconds. That is queue collapse, even though the client saw HTTP 200 for every request.

## Interpretation

- Admission control does not increase capacity. Accepted throughput stays near 24–32 requests/s in every condition.
- It prevents accepted requests from waiting behind an unbounded queue.
- At 2× the operating point, the limit of 7 sheds 42.8–48.0% of requests and keeps accepted p95 at 372–376 ms.
- At 4× the operating point, it sheds 70.4% and keeps accepted p95 at 387 ms. The baseline p95 at the same offered load is 24.4 seconds.
- The shed requests are explicit `503` responses with `Retry-After`, not silent timeouts.
- A real client must honor `Retry-After` and retry with jitter. The static experiment did not retry, so the 503 fraction is a property of the offered concurrency, not a client policy. The optional retry mode is measured separately below.
- At 25 requests/s, the limit is close to the natural in-flight count, so short bursts can still be shed. That run is not evidence of a latency improvement.

## AWS multi-host validation

Topology: one `c6i.large` gateway, two `c6i.large` workers, and one `c6i.xlarge` load generator in `us-east-1`. Routing was `least_loaded`; the workers used 2 ORT threads each. The workload was ResNet50-v2-7 with 500 unique images, `CACHE_CAPACITY=1`, binary transport, 10 seconds per run, and 128 load connections. Each rate has three repeats. The gateway log for every condition reports `Admission max in-flight: disabled` for baseline or `7` for admission.

| Offered load | Baseline accepted | Baseline p95 | Baseline client timeouts | Admission accepted | Admission 503s | Admission p95 | Admission p99 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 25 req/s | 250 / 250 | 1,511–1,581 ms | 0 | 234 / 250 | 16 (6.4%) | 324–331 ms | 339–342 ms |
| 50 req/s | 500 / 500 | 12,431–21,549 ms | 0 | 234–237 / 500 | 263–266 (52.6–53.2%) | 339–340 ms | 343–346 ms |
| 100 req/s | 866–878 / 1,000 | 28,829–29,209 ms | 122–134 | 235 / 1,000 | 765 (76.5%) | 343–344 ms | 355–360 ms |

- Accepted throughput was about 23.0 requests/s in every admission run. Baseline successful throughput was 22.6 requests/s at 25, 15.6–22.1 requests/s at 50, and 21.6 requests/s at 100. Admission does not add capacity; it prevents queue waste and client timeouts.
- At 100 req/s the gateway accepted all 1,000 baseline requests, had 72–82 still in flight when metrics were captured, and rejected none. The load client timed out on 122–134 requests at its 30-second limit. Admission returned 765 controlled `503`s, had zero requests in flight at capture, and produced no timeouts.
- The first baseline 50 req/s run recorded five gateway-to-worker retries (`failed: no response`) and 505 worker completions for 500 client requests. Its 21.5 s p95 and 32.0 s wall include that transient; the other two baseline runs were 12.4–12.7 s p95 and 22.6–22.8 s wall. The conclusion does not depend on the high end.
- Both workers reached about 100% of their 2 vCPUs in the busiest 10-second window in every condition. Gateway CPU stayed below about 15%. The workers are the bottleneck; admission bounds the queue rather than idling the workers.
- All admission failures were controlled `503` responses. There were no other 5xx responses.
- The run used 4 instances for 17 minutes 40 seconds. Estimated cost is **$0.1326** (exact estimate $0.132578). Cleanup verified that all instances were terminated and all volumes and security groups were removed.
- This is one topology in one region. It does not prove multi-AZ, autoscaling, production traffic, or a client retry policy. The load generator does not retry after `503`.

Sanitized artifact: `benchmarks/results/2026-11-04-aws-admission-validation.json`. Raw run: `benchmark-aws/runs/infer-8449738a5d8c568d` (ignored, local).

## Adaptive admission and metrics

The gateway exposes Prometheus metrics at `/metrics` and supports an opt-in adaptive in-flight limit.

### Metrics

`/metrics` is never rejected, even when admission is full. It includes:

- `gateway_requests_total{endpoint,status}` and a per-endpoint latency histogram.
- `gateway_admission_in_flight`, `gateway_admission_limit`, `gateway_admission_configured_max`, `gateway_admission_min`, and `gateway_admission_adaptive`.
- `gateway_admission_target_p95_ms`, `gateway_admission_observed_p95_ms`, and `gateway_admission_observed_samples`.
- `gateway_admission_accepted_total`, `gateway_admission_rejected_total`, and `gateway_admission_limit_changes_total`.
- `gateway_worker_in_flight{worker}` and `gateway_worker_circuit_open{worker}`.

### Policy

Enable adaptive mode with:

```sh
ADMISSION_ADAPTIVE=1
ADMISSION_MAX_INFLIGHT=12
ADMISSION_MIN_INFLIGHT=2
ADMISSION_TARGET_P95_MS=400
ADMISSION_INTERVAL_MS=500
```

- The limit starts at `ADMISSION_MAX_INFLIGHT`.
- Once per interval, one request elects itself to update the limit from the last 256 successful requests.
- Fewer than 20 samples: hold.
- p95 above 110% of target: decrease by 20%, never below the configured minimum.
- p95 below 75% of target: increase by 1, never above the configured maximum.
- Otherwise hold. This deadband reduces oscillation around the target.
- The latency window is cleared after a change so stale samples do not dominate the next decision. `observed_samples` is the sample count used by the last decision, not the current window size.
- Static mode is unchanged; `ADMISSION_ADAPTIVE=0` keeps the previous behavior.

### Local experiment

Two local workers, ResNet50, 500 unique images, cache 1, 10-second runs, 128 connections. Static 7 was compared with adaptive max 12, min 2, target 400 ms, interval 500 ms. All 12 conditions used gateway SHA-256 `b32f2afc445fbf274a66cfb790a4298a34790f79a806d7b035508552a0c98609` and worker SHA-256 `603f3453730e4b8af1275eee8454db0a3ab1c5b3b8d28ecf99bb0439e1adf794`.

| Offered load | Static 7 | Adaptive | Adaptive final limit |
| ---: | --- | --- | ---: |
| 25 req/s | 250 / 250 accepted; 0 503s; p95 158 ms | 250 / 250 accepted; 0 503s; p95 362 ms | 12 |
| 50 req/s | 264 / 500 median accepted (263–278); 236 503s (222–237); p95 397 ms (394–399) | 297 / 500 median accepted (260–300); 203 503s (200–240); p95 534 ms (471–552) | 6–7 |
| 100 req/s | 277 / 1,000 accepted; 723 503s; p95 401 ms | 266 / 1,000 accepted; 734 503s; p95 557 ms | 7 |

- Adaptive converges near the target: final observed p95 was 360 ms at 25, 351–385 ms at 50, and 398 ms at 100.
- The whole-run p95 is still worse than static 7. At 25 requests/s the controller stays at the ceiling and accepts the 400 ms budget, so p95 is 362 ms versus 158 ms for static 7. At 50 it accepts more requests (297 versus 264) but p95 is 534 ms versus 397 ms. At 100 it accepts slightly fewer requests (266 versus 277) and p95 is 557 ms versus 401 ms.
- The 10-second window includes the convergence ramp from the initial limit of 12 and large run-to-run variance. Static 7 was already manually calibrated for this topology.
- This is a mixed result: adaptive admission works mechanically and converges near the target, but it did not beat the calibrated static limit in these short runs. The metrics surface and retry client are the durable deliverables; the policy needs a longer evaluation window, a lower initial limit, or a faster decrease before it can replace static tuning.

### Retry-aware client

At 100 requests/s with `--retry-attempts 3`, `Retry-After` and exponential backoff plus jitter:

| Admission | Final successes | Attempts | Retried requests | Retry successes | Exhausted | p95 | Wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Static 7 | 399 / 1,000 | 3,280 | 842 | 241 | 601 | 3,323 ms | 13.2 s |
| Adaptive | 320 / 1,000 | 3,437 | 883 | 203 | 680 | 3,329 ms | 13.2 s |

Retry mode raises final goodput from 266–277 to 320–399 requests, but p95 rises to about 3.3 seconds because the metric includes the wait between attempts. It is a client-side goodput/latency tradeoff, not a free improvement.

Sanitized artifact: `benchmarks/results/2026-11-04-adaptive-admission.json`.

## Gateway HA and failover

The HA test used two gateway processes against the same two workers, with a local `aiohttp` proxy in front. The proxy polls each gateway `/health` every second, round-robins healthy gateways, and retries the other gateway once on a connection error.

Topology: 2 workers, 2 gateways, 1 proxy, 50 offered requests/s, 10-second phases, 128 connections, 3 complete normal/failover/recovery runs. Per-gateway admission limit 4, so the configured total is 8. All runs used gateway SHA-256 `818630c99f457c79a879af5ddb032171a8bb1f84b853459575f13519957be688` and worker SHA-256 `603f3453730e4b8af1275eee8454db0a3ab1c5b3b8d28ecf99bb0439e1adf794`.

| Phase | Successes | p95 | p99 | Accepted throughput | 503s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Normal | 343 median (329–352) | 257 ms (243–258) | 271 ms | 33.8 /s | 157 (148–171) |
| Failover | 322 median (320–343) | 224 ms (213–250) | 241 ms | 31.9 /s | 178 (157–180) |
| Recovery | 357 median (318–358) | 241 ms (230–280) | 266 ms | 35.2 /s | 143 (142–182) |

- Each failover run killed gateway 1 after 2 seconds. The proxy marked it down, completed the load through gateway 2, and recorded exactly one proxy failover. The observed failover window was 0.205–0.257 s. Gateway 1 had accepted 32–37 requests and rejected 12–17 before it was killed. Each run had one expected upstream `ClientConnectorError`, which the proxy retried successfully; there were no client-visible connection failures and no proxy-generated both-down 503. After restart, gateway 1 returned to rotation.
- A normal run with 7 per gateway (configured total 14) accepted 330/500 but p95 rose to 500 ms and p99 to 529 ms. Four per gateway is the better fit for two workers.
- Admission is local to each gateway. During failover the survivor keeps its limit of 4, so the configured total drops from 8 to 4. In this workload the workers still allowed similar goodput, but that is a capacity tradeoff, not a global admission guarantee.
- The proxy is a local test harness, not a production load balancer. It shares the host CPU and does not implement TLS, connection draining, or multi-host failover.

Sanitized artifact: `benchmarks/results/2026-11-04-gateway-ha.json`.

### AWS internal-ALB result

Topology: 2 `c6i.large` gateways, 2 `c6i.large` workers, 1 `c6i.xlarge` load generator, and an internal Application Load Balancer. 50 offered requests/s, 128 connections, per-gateway admission limit 4 (total 8). Normal and recovery phases were 10 seconds; failover was 20 seconds with gateway 1 terminated after 2 seconds.

| Phase | Successes | 503 | 502 | 504 | p95 | p99 | Accepted throughput |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Normal | 232 / 500 | 268 | 0 | 0 | 349 ms | 369 ms | 22.6 /s |
| Failover | 455 / 1,000 | 326 | 180 | 39 | 340 ms | 348 ms | 18.7 /s |
| Recovery | 235 / 500 | 265 | 0 | 0 | 350 ms | 374 ms | 22.8 /s |

Latency percentiles cover successful HTTP 200 responses only; 502/504/503 responses are counted separately.

- The ALB TargetHealth API first reported the terminated gateway unhealthy after **16.576 seconds**. Request IDs show new 502/504 responses starting about 5.3 seconds after the load marker and no new dead-target routing after about 14.3 seconds. The 16.576-second value is a health-state observation, not the full client-visible routing window; the exact ALB transition is inferred because CloudTrail and CloudWatch are unavailable.
- The ALB returned **180 × 502** and **39 × 504** during the transition. The 503s are gateway admission rejections, not ALB failures.
- The instance was terminated about 5 seconds after the load marker, not exactly 2 seconds: the harness slept 2 seconds, then took the pre-kill stats snapshot before terminating the instance.
- The target group uses the default 300-second deregistration delay. The planned 5-second delay was not configured; failover relies on health checks, not deregistration.
- Both targets were healthy during normal and recovery. The replacement gateway was healthy before the recovery phase.
- Normal and recovery shed about 53% because four in-flight requests per gateway (eight total) were below the 50 req/s offered load; accepted throughput was about 23 requests/s.
- The local aiohttp proxy used one-second health checks and connection-error retry, so it had no client-visible failover errors. The ALB health-check interval is five seconds and requires two unhealthy checks, so its failover window is much longer. A production design needs deregistration/draining, client retry for 502/504, or a load balancer with faster detection.
- The run used one subnet/AZ for all instances. The ALB spans two AZs, but this is not a multi-AZ failover test.
- Cost was **$0.074941**; cleanup verified the ALB, listener, target group, instances, volumes, and security groups were removed. Two earlier setup attempts failed on IAM and a CLI flag; their cost is recorded in the ledger.

Sanitized artifact: `benchmarks/results/2026-11-04-aws-alb-ha.json`.

## Limits

- Local 16-core host and two AWS topologies: a single-gateway admission run and a two-gateway internal-ALB run.
- Two workers; no 1/4/8-worker AWS comparison.
- AWS: three 10-second runs per rate. Local: one run each at 25 and 100 requests/s, three at 50.
- Worker CPU was collected on AWS; local runs record client CPU only.
- The static local and AWS experiments did not retry; the optional retry mode is reported separately and uses fixed backoff/jitter.
- Local runs share one host CPU. AWS separates the load, gateway, and workers.
- The AWS load generator uses a 30-second client timeout; a real client may use a different timeout.
- The admission limit of 7 was carried from the local 2-worker test and was not tuned on AWS.
- The adaptive and retry experiment is local only; no AWS adaptive or retry run was performed.
- The gateway HA test ran locally with an aiohttp proxy and on AWS with an internal ALB. Neither is a production load balancer.
- The AWS ALB run used one run, one subnet/AZ for all targets, no client retry for 502/504, and a health-state observation of 16.6 s. The client-visible dead-target routing began ~5.3 s after the load marker and stopped ~14.3 s.
- Adaptive policy constants (20 samples, 20% decrease, +1 increase, 110%/75% deadband) were chosen from first principles and not tuned across topologies.
- Retry p95 includes the wait between attempts; it is not comparable to first-attempt latency.
- Static mode uses a manually selected limit; adaptive mode is opt-in and was tested locally. A production control plane would also account for multiple gateways and per-client quotas.
- No Prometheus server, dashboards, TLS, authentication, per-user quota, or public deployment.
- Raw local runs remain under `/tmp/opencode/final-*` and `/tmp/opencode/adaptive-*`; the AWS raw run is under `benchmark-aws/runs/infer-8449738a5d8c568d`. These are ignored and can be lost. The sanitized JSONs record the derived values and binary or source hashes where available.
- The benchmark is not a production availability claim.

## Artifacts

- Local sanitized summary: `benchmarks/results/2026-11-04-overload-protection.json`.
- AWS admission sanitized summary: `benchmarks/results/2026-11-04-aws-admission-validation.json`.
- AWS ALB sanitized summary: `benchmarks/results/2026-11-04-aws-alb-ha.json`.
- Tests: `tests/test_admission_control.py` covers `/infer`, `/infer_tensor`, negative and out-of-range configuration, and release after a malformed request.
- Raw local runs: `/tmp/opencode/final-*` and `/tmp/opencode/adaptive-*` (ignored, temporary).
- Raw AWS admission run: `benchmark-aws/runs/infer-8449738a5d8c568d` (ignored, local).
- Raw AWS ALB run: `benchmark-aws/runs/infer-b24dc251e5bbc128` (ignored, local).

## Rollback

Remove `ADMISSION_MAX_INFLIGHT` or set it to `0`. The previous gateway behavior returns.
