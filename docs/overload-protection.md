# Overload protection and admission control

## What changed

The gateway now supports an optional global in-flight limit:

```sh
ADMISSION_MAX_INFLIGHT=7
```

- `0` is the default and keeps the previous behavior.
- When the limit is reached, the gateway returns HTTP `503` with `Retry-After: 1` and a JSON body.
- The limit covers `/infer` and `/infer_tensor`.
- The gateway has no `/health` endpoint. `/stats` is never rejected, and worker `/health` endpoints are separate from gateway admission.
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

All reported runs used the same binaries:

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
- A real client must honor `Retry-After` and retry with jitter. The load generator does not retry, so the 503 fraction is a property of the offered concurrency, not a client policy.
- At 25 requests/s, the limit is close to the natural in-flight count, so short bursts can still be shed. That run is not evidence of a latency improvement.

## Limits

- Local host, not AWS `c6i.large`.
- Two workers, not 1/2/4/8.
- Three 10-second runs at 50 requests/s; one run each at 25 and 100 requests/s.
- Worker CPU was not collected. The harness records client CPU only.
- The load generator is open-loop but does not implement retry/backoff.
- The client, gateway, and workers share one host CPU. Client-side queueing can contribute to baseline latency at 128 connections.
- The admission limit is static and manually selected. A production control plane would derive it from model, worker count, and SLO.
- No TLS, authentication, per-user quota, metrics export, or public deployment.
- Raw runs remain under `/tmp/opencode/final-*`; they can be lost. The sanitized JSON records the derived values and binary hashes.
- The benchmark is not a production availability claim.

## Artifacts

- Sanitized summary: `benchmarks/results/2026-11-04-overload-protection.json`.
- Tests: `tests/test_admission_control.py` covers `/infer`, `/infer_tensor`, negative and out-of-range configuration, and release after a malformed request.
- Raw runs: `/tmp/opencode/final-*` (ignored, temporary).

## Rollback

Remove `ADMISSION_MAX_INFLIGHT` or set it to `0`. The previous gateway behavior returns.
