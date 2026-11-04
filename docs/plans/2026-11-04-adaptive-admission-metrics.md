# Adaptive admission and Prometheus metrics

## Objective

Close the control-plane gap left by static admission control:

- expose Prometheus-compatible metrics from the gateway,
- make the admission limit adapt to observed accepted latency,
- add a retry/backoff client so `503` handling can be measured,
- compare static and adaptive admission under local overload.

## Non-goals

- No distributed admission across multiple gateways.
- No external Prometheus server, Grafana, or alerting stack.
- No TLS, authentication, or per-client quotas.
- No AWS run unless the local result is strong and a separate bounded run is approved.
- No model or worker-batching change.

## Verified facts

- The gateway has a static `ADMISSION_MAX_INFLIGHT`; `0` disables admission.
- Admission uses a compare-and-swap counter and records accepted/rejected counts in `/stats`.
- `/stats` is the only observability endpoint.
- The load generator treats `503` as a failure and does not retry.
- The local and AWS results show the static limit protects p95 but is manually selected.
- Current Python suite: 79 tests with real binaries; CTest 4/4; CI green.

## Design

### Prometheus metrics

Add `include/gateway_metrics.h` with a small, fixed-cardinality registry:

- `gateway_requests_total{endpoint,status}` counter.
- `gateway_request_duration_seconds` histogram per endpoint.
- `gateway_admission_accepted_total`, `gateway_admission_rejected_total` counters.
- `gateway_admission_in_flight`, `gateway_admission_limit`, `gateway_admission_configured_max` gauges.
- `gateway_admission_adaptive`, `gateway_admission_target_p95_ms`, `gateway_admission_observed_p95_ms` gauges.
- `gateway_admission_limit_changes_total` counter.
- `gateway_worker_in_flight{worker}` and `gateway_worker_circuit_open{worker}` gauges.

`/metrics` returns Prometheus text format and is never rejected. `/stats` keeps the JSON shape and adds adaptive fields.

### Adaptive policy

Add `include/adaptive_admission.h` with a pure policy function:

- Enable with `ADMISSION_ADAPTIVE=1`.
- `ADMISSION_MAX_INFLIGHT` is the upper bound; `ADMISSION_MIN_INFLIGHT` defaults to 1.
- `ADMISSION_TARGET_P95_MS` defaults to 400.
- `ADMISSION_INTERVAL_MS` defaults to 1000.
- The gateway records successful `/infer` and `/infer_tensor` latencies in a bounded window.
- Once per interval, one request elects itself to update the limit:
  - fewer than 20 samples: hold,
  - p95 above 110% of target: decrease by 20% with a floor of `ADMISSION_MIN_INFLIGHT`,
  - p95 below 75% of target: increase by 1 with a ceiling of `ADMISSION_MAX_INFLIGHT`,
  - otherwise hold (deadband between 75% and 110% of target).
- Clear the latency window after a change so stale samples do not dominate the next decision.
- Static mode remains the default; `ADMISSION_ADAPTIVE=0` preserves current behavior.

### Retry-aware client

Extend `benchmark-aws/autotune/loadgen.py`:

- `--retry-attempts` (default 0), `--retry-backoff-ms` (default 100), `--retry-max-backoff-ms` (default 1000), `--retry-jitter` (default 0.2).
- Retry only HTTP `503`, honor `Retry-After` when present, then use exponential backoff plus jitter.
- Record `attempts`, `retried_requests`, `retry_successes`, and `retry_exhausted`.
- End-to-end latency covers the first attempt through the final result.
- Do not retry `400`, `500`, timeouts, or connection errors by default.

### Local experiment

- 2 workers, ResNet50, 500 unique images, cache 1, binary transport.
- 50 and 100 offered requests/s, 10 seconds, 128 connections.
- Compare static 7 versus adaptive (min 2, max 16, target 400 ms).
- Run one no-retry and one retry condition at 100 req/s.
- Report accepted p95/p99, shed rate, final goodput, retry counts, and the limit trace from the gateway log.

## Affected files

- `include/gateway_metrics.h` (new)
- `include/adaptive_admission.h` (new)
- `src/gateway.cpp`
- `CMakeLists.txt`
- `tests/test_adaptive_admission.cpp` (new)
- `tests/test_gateway_metrics.py` (new)
- `tests/test_loadgen_retry.py` (new)
- `benchmark-aws/autotune/loadgen.py`
- `benchmark-aws/run_autotune.py`
- `benchmark-aws/controller.py`, `benchmark-aws/verify_source.py` (source allowlist)
- `tests/test_source_delivery.py`
- `.github/workflows/ci.yml`
- report and ledger after the experiment

## Risks and controls

- **Oscillation:** use hysteresis, a 20% decrease, a 1-step increase, and a minimum sample count. Record the limit trace.
- **Stale latency:** clear the window after each limit change.
- **Retry storms:** cap attempts, honor `Retry-After`, add jitter, and retry only `503`.
- **Metrics cardinality:** fixed endpoint/status/worker labels only; no request IDs.
- **Default regression:** adaptive mode is opt-in; static-mode tests must stay unchanged.
- **Timing tests:** the policy is a pure function; integration tests check formatting and counters, not exact controller convergence.

## Acceptance criteria

1. Static admission behavior is unchanged with `ADMISSION_ADAPTIVE=0`.
2. `/metrics` returns valid Prometheus text with request, latency, admission, and worker series.
3. Adaptive policy tests cover increase, decrease, hold, floor, ceiling, and too-few-samples cases.
4. Retry tests prove `503` retry success, `Retry-After` handling, exhaustion, and no retry on non-503.
5. The local benchmark produces a limit trace and compares static versus adaptive.
6. Full Python suite, CTest, Ruff, source-delivery checks, and CI pass.
7. Report and experiment ledger include the result and limits; no resume edit.

## Verification

```sh
cmake --build /tmp/opencode/cmake-build-hardening --parallel 4
ctest --test-dir /tmp/opencode/cmake-build-hardening --output-on-failure
BENCH_GATEWAY_BIN=/tmp/opencode/cmake-build-hardening/gateway \
  python3 -m unittest tests.test_gateway_metrics tests.test_admission_control -v
python3 -m unittest tests.test_loadgen_retry -v
python3 -m unittest discover -s tests -v
ruff check benchmark-aws tests
```
