# Five-minute demo script

## Before the demo

Build and prepare the fixture:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
python3 benchmark-aws/autotune/prepare_data.py \
  --output benchmark-aws/data/imagenet500 --count 500 --force
```

Check that ports 8000, 8001, and 8002 are free. Close other workers or gateways first.

Set the paths:

```sh
export MODEL=$PWD/models/resnet50-v2-7.onnx
export DATA=$PWD/benchmark-aws/data/imagenet500
```

## 0:00–0:40 — Problem and architecture

Show `README.md`, Architecture section.

Say:

> This is a C++ distributed inference engine. A gateway routes each request to one of two ONNX Runtime workers, which cache results and batch inference calls. Under load, the queue grew without bound: every request returned 200, but p95 reached 8 seconds. I added admission control, metrics, and a failover harness, then tested it locally and on AWS.

## 0:40–2:20 — Baseline collapse

Run:

```sh
python3 benchmark-aws/run_autotune.py \
  --model "$MODEL" --data "$DATA" \
  --output /tmp/opencode/demo-base \
  --worker-bin build/worker_node --gateway-bin build/gateway \
  --workers 2 --admission-max 0 \
  --levels 1 --rate 50 --duration 5 --rate-concurrency 128 \
  --mode unique --cache 1 --threads 2 --force
```

Show the result:

```sh
python3 - <<'PY'
import json
d = json.load(open("/tmp/opencode/demo-base/level-1/summary.json"))
print({k: d[k] for k in ("requests", "successes", "failures", "latency_ms", "successful_requests_per_second", "wall_seconds")})
PY
```

Say:

> Fifty requests per second for five seconds. Every request succeeds, but p95 is several seconds and the wall time is longer than the workload. That is queue collapse: HTTP 200 does not mean the latency is usable.

## 2:20–3:50 — Admission control

Run the same command with `--admission-max 7` and a new output directory:

```sh
python3 benchmark-aws/run_autotune.py \
  --model "$MODEL" --data "$DATA" \
  --output /tmp/opencode/demo-ad7 \
  --worker-bin build/worker_node --gateway-bin build/gateway \
  --workers 2 --admission-max 7 \
  --levels 1 --rate 50 --duration 5 --rate-concurrency 128 \
  --mode unique --cache 1 --threads 2 --force
```

Show the result with the same script, using `/tmp/opencode/demo-ad7`.

Say:

> The gateway now rejects excess work with 503 and Retry-After. Accepted p95 is about 375 milliseconds, and the run finishes in the workload window. About 43–48% of requests are shed. That is the contract: protect the accepted requests, make the rejection explicit, and let the client retry.

## 3:50–4:30 — Failover

Show the table in `docs/overload-protection.md`, or the artifact:

```sh
python3 - <<'PY'
import json
d = json.load(open("benchmarks/results/2026-11-04-aws-alb-ha.json"))
print(json.dumps({
    "failover": d["failover"],
    "status_counts": d["phases"]["failover"]["status_counts"],
}, indent=2))
PY
```

Say:

> Locally, a health-checked proxy fails over in about 0.2 seconds with no client-visible errors. On AWS, the ALB checks every 5 seconds and needs two unhealthy checks. TargetHealth reported the dead target unhealthy after 16.6 seconds. Clients saw 180 502s and 39 504s during the transition. I kept that result and documented the fix: bounded retry, draining, or faster detection.

## 4:30–5:00 — Limits and next step

Say:

> This is one run, one AZ, with no TLS or global admission. It is benchmark evidence, not a production claim. The next step is bounded client retry for 502/504, then a shared admission limit and multi-AZ placement.

## If something fails

- **Port already in use:** stop the old `worker_node` or `gateway` process and rerun.
- **Model or data missing:** rerun the build or `prepare_data.py` commands.
- **Low p95 difference:** the local host shares CPU with the load generator. Use a quiet machine and keep the 10-second runs.
- **No `summary.json`:** the run failed before the load phase. Read the log in the output directory.

## Do not show

- Raw AWS account IDs, instance IDs, IPs, or ALB DNS names.
- Claims of production readiness, multi-AZ, or zero downtime.
