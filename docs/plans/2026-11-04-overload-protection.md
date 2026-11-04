# Overload protection and SLO control plane

## Objective

Add an opt-in gateway admission limit that protects accepted inference requests during overload, rejects excess load with HTTP 503 and `Retry-After`, exposes admission counters, and measures the behavior with the existing local harness.

## Non-goals

- No public deployment, TLS, authentication, or per-user quotas.
- No distributed rate limiter.
- No autoscaler.
- No model change.
- No AWS spend. The first evaluation is local and reproducible.

## Verified facts

- The gateway is synchronous: each handler blocks on a worker HTTP call.
- The gateway already tracks per-worker in-flight counts for routing.
- The worker health endpoint exposes batch and cache counters.
- The benchmark shows a throughput plateau and p95 collapse at high concurrency.
- The existing local runner starts one worker. It needs a small extension to start several workers for a meaningful admission test.

## Design

### Admission rule

- New environment variable: `ADMISSION_MAX_INFLIGHT`.
- Default `0` keeps current behavior (disabled).
- When greater than zero, the gateway atomically admits a request only when the global in-flight count is below the limit.
- The limit covers both `/infer` and `/infer_tensor`.
- Health and stats endpoints are never rejected.
- Rejection response:
  - HTTP `503`
  - `Retry-After: 1`
  - JSON body with `error` and `retry_after_ms`
- Accepted requests decrement the counter with an RAII guard on every return and exception path.

### Counters

`GET /stats` adds:

```json
"admission": {
  "max_in_flight": 8,
  "in_flight": 3,
  "accepted": 1200,
  "rejected": 400
}
```

The counters are cumulative since gateway start.

### Evaluation

Extend `run_autotune.py`:

- `--workers N`
- `--admission-max N`

Each level starts fresh workers and gateway.

Local test:

- 2 workers, ResNet50, binary transport, cache capacity 1, unique images.
- Baseline: admission disabled.
- Protected: admission limit selected from a calibration run.
- Levels: passing load, 2x passing load, 4x passing load.
- Report:
  - accepted-request p50/p95/p99,
  - 503 count and rate,
  - uncontrolled 5xx count,
  - successful throughput,
  - worker CPU and batch metrics.

## Risks and limits

- Closed-loop clients do not back off after 503; the 503 rate is a property of the offered concurrency, not a real client retry policy.
- Local 16-core hardware is not the AWS `c6i.large` target. Results compare policies, not absolute production capacity.
- 503 is a controlled rejection, not a successful request. Report it separately from uncontrolled 5xx errors.
- The admission limit is static. A real control plane would derive it from model, worker count, and SLO.

## Acceptance criteria

1. With admission disabled, behavior and existing tests are unchanged.
2. With `ADMISSION_MAX_INFLIGHT=1`, two concurrent requests produce one 200 and one 503 with `Retry-After`, and the rejected request never reaches a worker.
3. `/stats` reports the configured limit, current in-flight, accepted, and rejected counts.
4. A local overload run shows accepted p95 below the latency limit while the system sheds excess load, with zero uncontrolled 5xx.
5. The full Python suite, CTest, lint, and source-delivery checks pass.
6. The benchmark report includes the offered load, shed rate, accepted latency, and limitations.

## Verification commands

```sh
BENCH_WORKER_BIN=/tmp/opencode/cmake-build-hardening/worker_node \
BENCH_GATEWAY_BIN=/tmp/opencode/cmake-build-hardening/gateway \
BENCH_MODEL=$PWD/models/resnet50-v2-7.onnx \
python3 -m unittest tests.test_admission_control -v

BENCH_WORKER_BIN=... BENCH_GATEWAY_BIN=... BENCH_MODEL=... \
python3 -m unittest discover -s tests -v

ctest --test-dir /tmp/opencode/cmake-build-hardening --output-on-failure
```

## Files

- Modify `src/gateway.cpp`
- Modify `benchmark-aws/run_autotune.py`
- Create `tests/test_admission_control.py`
- Create `docs/overload-protection.md`
- Update `README.md` and `docs/investigation/EXPERIMENTS.md`
