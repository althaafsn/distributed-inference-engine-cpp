# Distributed inference with admission control and HA failover

**C++17 · ONNX Runtime · AWS EC2 · Application Load Balancer · Python benchmark harness**

## Summary

I extended a forked C++ distributed inference engine with overload protection, observability, and a reproducible AWS high-availability benchmark. The work turned a queue-collapse failure into an explicit load-shedding contract: at 50 requests/s, accepted p95 fell from 5.6–8.4 s to 372–376 ms by rejecting excess requests with HTTP 503 and `Retry-After`. On AWS, a two-gateway internal ALB completed normal, failover, and recovery, but exposed a 16.6 s target-health window with 180 HTTP 502 and 39 HTTP 504 responses. I measured that gap instead of hiding it, and documented the fix options.

**Provenance:** this is a fork of [AbhiramDodda/distributed-inference-engine-cpp](https://github.com/AbhiramDodda/distributed-inference-engine-cpp). The original engine and its published results are the upstream author's work. I set the goals, chose the experiments, and reviewed the results for the extensions described here; implementation and runs were AI-assisted.

## Problem

The engine routes each request to a worker by input hash, caches results, and batches inference calls. Under 2–4× its operating point, it kept accepting work and returned HTTP 200 for every request, but the queue grew without bound: p95 reached 5.6–24.4 s, and a 10-second workload took 15–35 seconds to drain. That is a failure even when every response is "successful".

The second question was failover. A single gateway is a single point of failure. The project had no evidence that a second gateway, a load balancer, or a restart could keep the service usable.

## What I built

- **Admission control.** An opt-in per-gateway in-flight limit. A compare-and-swap loop admits or rejects atomically, so the gateway never over-admits. Rejections are HTTP 503 with `Retry-After: 1`; `/health` and `/stats` are never rejected.
- **Adaptive admission.** An AIMD controller in `include/adaptive_admission.h` adjusts the limit from a bounded p95 window. It is opt-in; static admission remains the default because adaptive did not beat a tuned static limit in the local test.
- **Observability and retry.** Prometheus `/metrics` and JSON `/stats` expose accepted, rejected, in-flight, limit, and worker state. The load generator supports bounded retry with exponential backoff, jitter, and `Retry-After`.
- **Local HA.** A health-checked `aiohttp` proxy in front of two gateways, with a failover/recovery harness that kills one gateway and verifies traffic completes through the survivor.
- **AWS HA.** An internal-ALB controller that provisions two gateways, two workers, and a load generator; creates the target group, listener, and health checks; runs normal/failover/recovery; replaces the killed gateway; and verifies cleanup. A preflight checks subnets, leftover resources, ELB permissions, and cost.
- **Production hardening.** Bounded `SIGTERM`/`SIGINT` shutdown, queued-work failure, JSON request validation, and a loopback circuit-breaker failover test.
- **Tests and CI.** The suite grew to 120 Python tests, including real-binary loopback tests, plus 5 C++ tests (CTest) in CI.

## Results

| Experiment | Before | After |
| --- | --- | --- |
| Local, 2 workers, 50 req/s | p95 5.6–8.4 s, 0 shed | p95 372–376 ms, 42.8–48.0% shed |
| Local, 2 workers, 100 req/s | p95 24.4 s | p95 387 ms, 70.4% shed |
| AWS, 2 workers, 50/100 req/s | p95 12–29 s; 122–134 timeouts at 100 req/s | p95 339–344 ms |
| Local HA, kill gateway after 2 s | single gateway | 0.205–0.257 s failover, no client-visible errors |
| AWS internal ALB, kill gateway | single gateway | 16.576 s target-health change; 180×502, 39×504; recovered |

The 8-worker capacity work also reached 45 requests/s at p95 104 ms and p99 106 ms with zero errors, and least-loaded routing raised latency/error capacity from 31.4 to 86.1 requests/s.

## What failed, and what I learned

The AWS ALB did fail over, but not fast enough for clients. The target group checks every 5 seconds and requires two unhealthy checks. TargetHealth first reported the dead gateway unhealthy 16.576 seconds after the termination request. Dead-target requests started about 5.3 seconds after the load marker and the last one started about 14.3 seconds. The last HTTP 504 completed about 24.3 seconds because it waited the 10-second client timeout.

The local proxy polls every second and retries once on a connection error, so it failed over in about 0.2 seconds with no client-visible errors. The difference is the health-check design, not the gateway code.

The practical fixes are bounded client retry for 502/504, deregistration and draining for planned shutdowns, or faster detection. A hard instance kill cannot drain in-flight connections, so retry is the only fix that covers this failure mode.

## Limits

- One AWS run; all targets in one subnet/AZ.
- No TLS, authentication, public endpoint, or global admission.
- The target group used the default 300-second deregistration delay.
- Static admission is the default; adaptive was tested locally only.
- Two workers; no 1/4/8-worker AWS comparison for admission.
- This is benchmark evidence, not a production availability claim.

## Next

1. Add bounded client retry for ALB 502/504 and rerun the failover phase.
2. Add a shared admission limit across gateways.
3. Test real multi-AZ placement.
4. Add TLS, authentication, and operational dashboards before any public deployment.

## Evidence

- [Overload protection and HA report](../overload-protection.md)
- [Sanitized AWS ALB result](../../benchmarks/results/2026-11-04-aws-alb-ha.json)
- [Cost ledger](../aws-cost-ledger.md)
- [Source repository](https://github.com/althaafsn/distributed-inference-engine-cpp)
