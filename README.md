> **Project provenance:** This is a fork of [AbhiramDodda/distributed-inference-engine-cpp](https://github.com/AbhiramDodda/distributed-inference-engine-cpp). The upstream repository did not specify a license when this fork was prepared. The original implementation and its measurements are credited to the upstream author.
>
> This fork adds a temporary AWS benchmark workflow, an image auto-tuning harness, local cost and cleanup records, queue and batch timing instrumentation, capacity-aware ONNX Runtime thread selection, `TCP_NODELAY` keep-alive tuning, a 10 ms batch deadline, gateway response passthrough, binary tensor transport, selectable least-loaded routing, and a static batch-size cap. See [AWS baseline](docs/aws-baseline.md), [instrumented results](docs/instrumented-aws-run.md), the [thread-tuning A/B](docs/aws-thread-tuning.md), the [HTTP nodelay A/B](docs/aws-http-nodelay.md), the [batch-deadline A/B](docs/aws-batch-deadline.md), the [gateway passthrough A/B](docs/aws-gateway-passthrough.md), the [image auto-tuning benchmark](docs/autotune-benchmark.md), and the [run guide](benchmark-aws/README.md). The upstream performance results below describe the original author's experiments; they are not results from our AWS runs.

# Distributed Inference Engine

A high-performance distributed inference system for ONNX models featuring consistent hashing, LRU caching, dynamic batching, and circuit breakers.

### Links to blogs
- [Part 1](https://medium.com/@abhiramdodda/building-a-production-grade-distributed-inference-system-in-c-part-1-11b933d9216f)
- [Part 2](https://medium.com/@abhiramdodda/building-a-production-grade-distributed-inference-system-in-c-part-2-50d0d3ba290d)


## Features

- **Consistent Hashing**: Routes each request by a hash of its input data (via virtual nodes) so identical inputs always reach the same worker, maximizing cache locality across the cluster
- **LRU Cache**: Caches inference results to reduce computation for repeated requests
- **Adaptive Batching**: SLO-aware dynamic batching — the batch grows only while the oldest queued request stays under a latency budget, trading between throughput (large batches) and tail latency (small batches) automatically as load changes
- **Circuit Breakers**: Monitors worker health and automatically routes around failed nodes
- **Load Balancing**: Distributes requests across multiple worker nodes
- **CUDA Support**: Optional GPU acceleration with automatic CPU fallback

## Image auto-tuning benchmark

The 2026-11-02 campaign used a 500-image ImageNet subset, five pinned ONNX models, private AWS `c6i.large` workers, and a separate `c6i.xlarge` load generator. All capacity runs use a cold cache and real model outputs.

- 8-worker ResNet50 full-SLO hold: **45 requests/s**, p95 **104 ms**, p99 **106 ms**, 0 errors, worst worker mean CPU **56%** (maximum sample 67%).
- 8-worker latency/error capacity: **86 requests/s**. The 84 requests/s hold meets latency and errors but uses about 95% CPU, above the 80% limit.
- Least-loaded routing raises 8-worker latency/error capacity from **31.4 to 86.1 requests/s** (2.7×) compared with consistent hashing.
- ShuffleNetV2-10: **201 requests/s** on 2 workers at **72.2% top-1**. ResNet50-v2-7: **21.9 requests/s** at **78.6% top-1** on the same data.
- Worker kill at 40 requests/s: **0 failed requests**, 0 top-1 conflicts, 0.0 output difference.
- Estimated image campaign cost **$3.31**; total project cost **$3.44 of $10**. No active benchmark resources remain.

These results are not a production-readiness claim. TLS, authentication, rate limiting, observability, model versioning, and multi-AZ replacement remain open. See the [full report](docs/autotune-benchmark.md) and the [sanitized summary](benchmarks/results/2026-11-02-autotune-summary.json).

## Production hardening

The worker and gateway now handle `SIGTERM` and `SIGINT` with a bounded shutdown, fail queued batch work instead of leaving handlers blocked, return HTTP 400 for malformed JSON inference requests, and have a loopback worker-failure test. CI builds the real C++ targets with pinned ONNX Runtime and runs the real-model loopback tests. The shutdown and failover evidence is loopback-only. TLS, authentication, rate limiting, model versioning, and multi-AZ replacement remain open. Prometheus scraping and dashboards are not deployed. See [production hardening](docs/production-hardening.md).

## Overload protection

The gateway supports an opt-in global in-flight limit, `ADMISSION_MAX_INFLIGHT`. At the limit it returns HTTP `503` with `Retry-After: 1` and exposes accepted, rejected, and current in-flight counters on `/stats`. In local 2-worker tests, a limit of 7 kept accepted p95 at 372–376 ms at 50 offered requests/s, where the baseline p95 was 5.6–8.4 s. On a 2-worker AWS `c6i.large` topology, admission kept accepted p95 at 339–344 ms at 50 and 100 offered requests/s; the baseline p95 was 12–29 s, and the 100 req/s baseline also produced 122–134 client timeouts. The gateway also exposes Prometheus metrics at `/metrics` and supports adaptive admission (`ADMISSION_ADAPTIVE=1`). In a local 10-second comparison, adaptive converged near the 400 ms target but did not beat the manually calibrated static limit, so static remains the default. This is benchmark evidence, not a production availability claim. See [overload protection](docs/overload-protection.md).

## Architecture

```
Client -> Gateway (port 8000) -> Worker Nodes (ports 8001, 8002, 8003)
                                       |
                                       +-> ONNX Runtime (CPU/CUDA)
                                       +-> LRU Cache
                                       +-> Batch Processor
```

## System Requirements

### Required Dependencies

- C++17 compatible compiler (GCC 7+ or Clang 5+)
- CMake 3.15 or higher
- ONNX Runtime (CPU or CUDA version)
- pthread library

### Optional Dependencies

- CUDA Toolkit (for GPU acceleration)
- Python 3.6+ with requests library (for benchmarking)

### Arch Linux Installation

```bash
# Install system packages
sudo pacman -S base-devel cmake git onnxruntime

# For CUDA support (optional)
sudo pacman -S cuda onnxruntime-cuda
```

### Ubuntu/Debian Installation

```bash
# Install build tools
sudo apt-get update
sudo apt-get install build-essential cmake git

# Install ONNX Runtime (see https://onnxruntime.ai for installation)
```

## Build Instructions

### 1. Setup Dependencies

Run the setup script to download required libraries:

```bash
./setup.sh
```

This downloads:
- cpp-httplib (HTTP server library)
- nlohmann-json (JSON parsing library)

### 2. Build the Project

```bash
mkdir build
cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
make -j$(nproc)
cd ..
```

Build artifacts:
- `build/worker_node` - Inference worker process
- `build/gateway` - Request gateway/router

## Running the System

### 1. Prepare ONNX Model

Ensure you have an ONNX model file. Set the path:

```bash
export MODEL_PATH=/path/to/your/model.onnx
```

### 2. Start Worker Nodes

Start three worker nodes on different ports:

```bash
./build/worker_node 8001 worker_1 $MODEL_PATH &
./build/worker_node 8002 worker_2 $MODEL_PATH &
./build/worker_node 8003 worker_3 $MODEL_PATH &
```

Worker configuration:
- Cache capacity: 1000 entries
- Max batch size: 32 requests
- Batch latency budget: 10ms by default (max time a request waits to be batched;
  override with `BATCH_DEADLINE_MS`)
- HTTP server threads: 64 (kept above max batch size so batches can fill)

### 3. Start Gateway

Start the gateway to route requests:

```bash
./build/gateway localhost:8001 localhost:8002 localhost:8003
```

Gateway configuration:
- Listen port: 8000
- HTTP server threads: 256 (handlers block on outbound worker calls)
- Failure threshold: 5 failures before circuit opens
- Success threshold: 2 successes to close circuit
- Circuit timeout: 30 seconds

## API Endpoints

### Gateway Endpoints

#### POST /infer

Perform inference on input data.

Request:
```json
{
  "request_id": "unique_request_id",
  "input_data": [1.0, 2.0, 3.0, ...]
}
```

Response:
```json
{
  "request_id": "unique_request_id",
  "output_data": [-0.999, 0.452, ...],
  "node_id": "worker_1",
  "cached": false,
  "inference_time_us": 1250
}
```

The engine pads a shorter `input_data` to the model's input shape and truncates
a longer one to that shape. Each request therefore occupies exactly one
model-sized slot in a batch; an oversized request cannot shift the samples after
it.

#### GET /stats

Get gateway statistics and circuit breaker states.

Response:
```json
{
  "total_workers": 3,
  "circuit_breakers": [
    {
      "node": "localhost:8001",
      "state": "CLOSED",
      "failures": 0,
      "successes": 0
    }
  ]
}
```

### Worker Endpoints

#### POST /infer

Direct inference request (bypass gateway).

#### GET /health

Get worker health and performance metrics.

Response:
```json
{
  "healthy": true,
  "node_id": "worker_1",
  "total_requests": 1000,
  "cache_hits": 950,
  "cache_size": 50,
  "cache_hit_rate": 0.95,
  "batch_processor": {
    "total_batches": 100,
    "avg_batch_size": 10.5,
    "timeout_batches": 5,
    "full_batches": 95
  }
}
```

## Testing and Diagnostics

### Run Diagnostic Script

```bash
chmod +x diagnose.sh
./diagnose.sh
```

Checks:
- Process status
- Port availability
- Worker health
- Gateway connectivity
- Direct inference test
- End-to-end inference test

### Manual Testing

Test worker directly:
```bash
curl -X POST http://localhost:8001/infer \
  -H "Content-Type: application/json" \
  -d '{"request_id": "test1", "input_data": [1.0, 2.0, 3.0]}'
```

Test through gateway:
```bash
curl -X POST http://localhost:8000/infer \
  -H "Content-Type: application/json" \
  -d '{"request_id": "test1", "input_data": [1.0, 2.0, 3.0]}'
```

Check statistics:
```bash
curl http://localhost:8000/stats
curl http://localhost:8001/health
```

## Benchmarking

### Install Python Dependencies

```bash
pip install requests
```

### Run Standard Benchmark

```bash
python3 benchmark.py --requests 1000 --threads 10
```

### Run Cache Effectiveness Test

```bash
python3 benchmark.py --cache-test --requests 100
```

### Run Tail-Latency Load Sweep

Sweeps concurrency with **unique inputs** (cache-miss / real-inference path) and
reports throughput and the latency tail (p50/p90/p99/p99.9) plus batch occupancy at
each level, then locates the saturation knee. Point `--target` at a single worker to
isolate the batcher:

```bash
# single worker (isolates the batcher)
python3 tail_latency_sweep.py \
  --target http://localhost:8001 --workers http://localhost:8001 \
  --concurrency 8 16 32 48 64 96 128 --requests-per-level 1500

# full cluster via gateway
python3 tail_latency_sweep.py --concurrency 16 32 64 96 128 --requests-per-level 2000
```

### Benchmark Options

```
--gateway URL       Gateway URL (default: http://localhost:8000)
--requests N        Total number of requests (default: 1000)
--threads N         Number of concurrent threads (default: 10)
--workers URL...    Worker URLs for statistics
--cache-test        Run cache effectiveness test
--no-stats          Skip system statistics output
```

## Recent Improvements

- **TCP_NODELAY on keep-alive responses**: the worker server, gateway server, and
  gateway's worker client now disable Nagle. On the 2-vCPU benchmark host,
  cache-hit p50 fell from about 98 ms to 1.2–3.5 ms and closed-loop system
  throughput rose from 13.8–55.3 req/s to 311–956 req/s; unique-input p50 fell
  11–28%. Whole-batch backend time was unchanged. The high cache-hit throughput
  is client-bound at the upper levels, and this is a single-host synthetic-input
  result. See the [HTTP nodelay report](docs/aws-http-nodelay.md).
- **10 ms batch deadline**: the default batching window is 10 ms instead of
  20 ms, with `BATCH_DEADLINE_MS` as an override. On the 2-vCPU benchmark host,
  unique-input p50 fell 2–9% and throughput rose 2–9% at concurrency 1–4, while
  the measured batch sizes and cache-hit workload stayed unchanged. This is a
  latency/throughput tuning choice, not a universal optimum. See the
  [batch-deadline report](docs/aws-batch-deadline.md).
- **Gateway response passthrough**: the gateway now forwards a successful
  worker response body unchanged instead of parsing and re-dumping it. On the
  2-vCPU benchmark host, cache-hit p50 fell 24–30% and closed-loop cache-hit
  throughput rose 12–41%; unique-input and backend behavior were unchanged.
  This is a single-host synthetic-input result. See the
  [gateway passthrough report](docs/aws-gateway-passthrough.md).
- **Capacity-aware ORT threads**: the worker no longer hardcodes four ONNX Runtime
  intra-op threads. It now uses `min(4, hardware_concurrency)` with a floor of one,
  so the two-vCPU benchmark host runs two threads instead of four, and logs the
  effective count. In a matched `c6i.large` A/B, unique-input p50 fell from
  237.9 ms to 149.9 ms at concurrency 1 and throughput rose 81%; whole-batch
  backend p50 fell 56%. The repeated/cache-hit workload was unchanged. This is a
  single-host synthetic-input result, not a production or multi-host claim. See
  the [thread-tuning report](docs/aws-thread-tuning.md).
- **SLO-aware adaptive batching**: the batch processor now anchors its flush
  deadline to the *oldest queued request's arrival time* and flushes when the batch
  is full or that request would exceed a fixed latency budget (10ms by default,
  override with `BATCH_DEADLINE_MS`). Small batches
  at low load (latency mode), full batches under pressure (throughput mode) — the
  window adapts to offered load instead of a fixed timer.
- **Right-sized HTTP server thread pools**: cpp-httplib defaults its server pool to
  `hardware_concurrency - 1` (~15). Because every `/infer` handler blocks in the
  batcher, that default capped in-flight requests *below* `max_batch_size`, made
  full batches structurally unreachable, and stalled the tail in 5s keep-alive
  quanta once concurrency exceeded the pool. Raising the pools (worker 64, gateway
  256) let batches actually fill and lifted single-worker throughput ~3.8×
  (see Performance Results). Found via the tail-latency study below.
- **Input-based routing for cache locality**: the gateway now routes on a hash of
  the request's `input_data` (FNV-1a over the float bits) instead of `request_id`.
  Identical inputs always reach the same worker, so each distinct result is computed
  and cached once cluster-wide instead of being duplicated across workers.
- **Thread-safe worker connections**: each worker is backed by a pool of keep-alive
  HTTP clients. Concurrent gateway requests borrow their own connection (RAII),
  removing a data race on the previously shared, non-thread-safe `httplib::Client`.
- **Honest cache-hit metrics**: `inference_time_us` on a cache hit is now the actual
  measured lookup time (single-digit microseconds) rather than a hardcoded constant.
- **Stronger cache hashing**: `VectorHash` hashes the entire input vector rather than
  sampling three elements, eliminating avoidable bucket collisions (and the O(n)
  full-vector comparisons they triggered).
- **Circuit breaker synchronization cleanup**: all breaker state is guarded by a
  single mutex with consistent locking in every accessor, removing redundant and
  inconsistently-used atomics.

## Performance Results

Measured with the gateway and all three workers running locally on a single host
(CPU inference via ONNX Runtime). Because the load generator (`benchmark.py`) shares
the machine with the servers, absolute throughput is bounded by the Python client
rather than the inference engine — see the note below.

### 1,000,000 requests / 500 threads

Throughput
- Total requests: 1,000,000
- Success rate: 100.00% (0 failures)
- Total time: 2359.18s
- Requests/sec: 423.88

Latency
- Mean: 1168.51ms
- Median: 1162.19ms
- P90: 1867.70ms
- P95: 2033.39ms
- P99: 2318.40ms

### 10,000 requests / 50 threads

Throughput
- Success rate: 100.00%
- Requests/sec: 262.77

Latency
- Mean: 187.32ms
- Median: 191.08ms
- P90: 226.22ms
- P99: 357.86ms

### Cache & Load Distribution (input-based routing)

Requests are routed by a hash of the input data, so identical inputs always map to
the same worker. Across the benchmark's 10 distinct inputs, cache entries are
partitioned with no duplication (1M / 500-thread run):

| Worker   | Requests | Cache entries | Hit rate |
|----------|----------|---------------|----------|
| worker_1 | 300,000  | 3             | 100.00%  |
| worker_2 | 400,000  | 4             | 99.99%   |
| worker_3 | 300,000  | 3             | 99.99%   |

The 10 distinct results are stored once cluster-wide (3 + 4 + 3) rather than being
replicated across every worker.

> **Note:** the ~424 req/s ceiling is client-bound, not server-bound. With 500 client
> threads at ~424 req/s, Little's Law gives ~1.18s mean latency, which matches the
> measured mean — each thread spends its time in the queue it creates. The C++ path
> serves cache hits in single-digit microseconds (`total_batches` stays in single
> digits, so only the first occurrence of each input runs real inference). Measuring
> true server throughput requires a keep-alive/async load client.

### Adaptive Batching & Tail-Latency Study

Measured with `tail_latency_sweep.py`, which sends a **unique input on every
request** so each one misses the cache and exercises the real inference + batching
path (unlike `benchmark.py`, which reuses inputs and mostly measures cache hits).
Load is concentrated on a **single worker** to isolate the batcher — through the
3-worker gateway the load generator saturates before any one worker's batch queue
fills. Same host, CUDA provider, ResNet50-v2-7.

The tail study surfaced the real bottleneck: the HTTP server's default thread pool
(`hardware_concurrency - 1` ≈ 15) capped in-flight requests below `max_batch_size`,
so batches never filled and throughput plateaued at ~130 req/s. After raising the
worker pool to 64:

| Concurrency | Throughput | p50 (ms) | p99 (ms) | Avg batch | Full batches |
|-------------|-----------:|---------:|---------:|----------:|-------------:|
| 8           |    119/s   |     75   |     91   |    8.0    |      0%      |
| 16          |    134/s   |    120   |    166   |    8.2    |      0%      |
| 32          |    256/s   |    121   |    176   |   15.8    |      0%      |
| 48          |    367/s   |    127   |    180   |   23.8    |     49%      |
| **64**      |  **500/s** |  **121** |  **173** | **31.2**  |   **92%**   |
| 96          |    189/s   |    126   |   5387   |   30.6    |     92%      |
| 128         |    189/s   |    131   |   5429   |   31.2    |     96%      |

**Findings:**

- **Adaptive batching engages under load.** Average batch size grows 8 → 31 and the
  full-batch fraction goes 0% → 92% as concurrency rises, while p50 stays flat at
  ~120ms — the batch amortizes fixed inference overhead. Peak throughput **~500
  req/s**, a **~3.8×** gain over the pre-fix ~130 req/s ceiling.
- **The knee is the server thread pool.** Throughput peaks exactly at concurrency
  64 (= the new pool size). Past it, extra connections stall on cpp-httplib's 5s
  keep-alive timeout and the tail jumps to ~5s (p99 5387ms) while throughput drops.
  The pool sets both the concurrency ceiling and the maximum fillable batch.
- **Little's Law governs the post-knee tail.** With throughput pinned, latency grows
  linearly with concurrency (L = λW). The tail past the knee is queueing, not
  compute.

End-to-end through the gateway (worker pool 64, gateway pool 256), the earlier
under-load collapse is gone: throughput scales **130 → 500 req/s** peaking at
concurrency 96 with **p99 324ms and zero failures**. Both single-worker and cluster
peaks land at ~500 req/s because the single-process Python client is itself the
ceiling at that point — the server-side proof of batching is the batch-occupancy
series above, which is measured on the worker.

## Configuration

### Tuning Worker Performance

Edit `worker_node.cpp` to adjust:
- Cache capacity: `LRUCache cache_(1000)`
- Max batch size: `32`
- Batch latency budget: `std::chrono::milliseconds(10)` by default; override with
  `BATCH_DEADLINE_MS`
- HTTP server threads: `kServerThreads` (keep ≥ max batch size)

ORT intra-op threads are selected in `inference_engine.cpp` as
`min(4, hardware_concurrency)` with a floor of one. Set `ORT_INTRA_THREADS` to
override the default for an experiment or a specific deployment.

### Tuning Circuit Breakers

The gateway listens on port 8000 by default. Set `GATEWAY_PORT` to override it;
the loopback integration test uses this to avoid a fixed-port conflict.

Edit `gateway.cpp` to adjust:
- Failure threshold (line 19): `5`
- Success threshold (line 20): `2`
- Timeout (line 21): `std::chrono::seconds(30)`

### Tuning Consistent Hashing

Edit `gateway.cpp` constructor to adjust virtual nodes per physical node (default: 150).

## Troubleshooting

### All Requests Failing

Check if workers are running:
```bash
ps aux | grep worker_node
```

Check if ports are listening:
```bash
ss -tuln | grep -E "8000|8001|8002|8003"
```

Restart gateway to reset circuit breakers:
```bash
pkill gateway
./build/gateway localhost:8001 localhost:8002 localhost:8003
```

### Circuit Breakers Opening

Circuit breakers open after 5 consecutive failures. Common causes:
- Worker crashed or not responding
- Model file not found or corrupted
- Network connectivity issues

Wait 30 seconds for automatic reset, or restart the gateway.

### Low Cache Hit Rate

Ensure request inputs are identical for cache hits. The cache hashes the full input vector (FNV-1a over the float bits), and the gateway routes by that same hash, so identical inputs consistently reach the same worker's cache.

### CUDA Not Loading

If CUDA provider fails to load, the system automatically falls back to CPU. Check console output for:
```
CUDA failed to load: <error message>
Falling back to CPU Provider...
```

Verify CUDA installation:
```bash
nvidia-smi
```

## Project Structure

```
distributed_inference_engine/
├── src/
│   ├── circuit_breaker.cpp      # Circuit breaker implementation
│   ├── consistent_hash.cpp      # Consistent hashing
│   ├── gateway.cpp              # Gateway server
│   ├── inference_engine.cpp     # ONNX Runtime wrapper
│   └── worker_node.cpp          # Worker node server
├── include/
│   ├── batch_processor.h        # Dynamic batching (header-only)
│   ├── circuit_breaker.h
│   ├── consistent_hash.h
│   ├── inference_engine.h
│   ├── lru_cache.h             # LRU cache (header-only)
│   └── gateway.h
├── external/                    # Third-party dependencies
│   ├── cpp-httplib/
│   └── json/
├── build/                       # Build output
├── setup.sh                     # Dependency setup script
├── diagnose.sh                  # System diagnostic script
├── benchmark.py                 # Performance benchmark
├── tail_latency_sweep.py        # Tail-latency load sweep (unique-input path)
├── CMakeLists.txt              # Build configuration
└── README.md
```
