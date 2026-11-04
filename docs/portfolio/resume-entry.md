# Resume entry

## Primary entry

**Distributed Inference Engine — C++17, ONNX Runtime, AWS** · 2026  
*Fork extension of [AbhiramDodda/distributed-inference-engine-cpp](https://github.com/AbhiramDodda/distributed-inference-engine-cpp)*

- Extended a forked C++ inference engine with per-gateway admission control, Prometheus metrics, and retry-aware load generation; cut accepted p95 from 5.6–8.4 s to 372–376 ms at 50 req/s by shedding 43–48% with HTTP 503 and `Retry-After`.
- Built an AWS harness with two gateways, two workers, an internal ALB, and a load generator; ran normal/failover/recovery at 50 req/s and measured a 16.6 s target-health failover with 180 HTTP 502/39 504.
- Added bounded `SIGTERM` shutdown, JSON request validation, and a loopback circuit-breaker failover test; grew the suite to 120 Python tests, including real-binary loopback tests, plus 5 C++ tests in CI.
- Tracked spend against a $10 cap ($3.57 total, including two failed setup attempts) and verified zero remaining AWS resources after every run.

## Tailoring

**Backend / distributed systems:** keep bullets 1 and 2 first. Emphasize the 503 contract, p95 and accepted throughput, the compare-and-swap admission loop, and the failover analysis.

**Infrastructure / platform:** keep bullets 2 and 4 first. Emphasize the ALB target group, health checks, preflight, cleanup, IAM policy, and cost control.

**SRE / reliability:** keep bullets 2 and 3 first. Emphasize the measured failover window, the difference between health-state and client-visible errors, and the hardening tests.

Use three bullets if space is tight. Drop the cost bullet first; it is a differentiator, but the engineering bullets are stronger.

## Evidence map

| Claim | Evidence |
| --- | --- |
| p95 5.6–8.4 s → 372–376 ms at 50 req/s | `docs/overload-protection.md`, local table |
| 43–48% shed with 503 + Retry-After | same table; `src/gateway.cpp` admission path |
| AWS 2 gateways, 2 workers, internal ALB | `benchmarks/results/2026-11-04-aws-alb-ha.json` |
| 16.576 s target-health failover | same artifact, `failover.observed_failover_seconds` |
| 180 × 502 and 39 × 504 | same artifact, `phases.failover.status_counts` |
| 120 Python tests, 5 C++ tests | `tests/`, CTest output |
| $3.57 of $10 | `docs/aws-cost-ledger.md` |
| Cleanup verified | artifact `cleanup` field; read-only AWS check |

## Do not claim

- “Production-ready”, “zero downtime”, or “multi-AZ”. The run was one AZ and exposed 502/504.
- “Global rate limiting”. The limit is per gateway.
- “TLS”, “authentication”, or “public endpoint”. None were built.
- “Wrote the original engine”. It is a fork; credit the upstream author.
- “Handles 50 requests/s”. The offered rate was 50; accepted throughput was about 23–32 requests/s after shedding.
- “Reduced latency by 95%” without the context: accepted p95, 50 requests/s, 2 workers, local host.

## Can you defend this?

Answer these without notes before you put the project on a resume.

1. **Why admission control instead of autoscaling?** Admission bounds in-flight work immediately. Autoscaling reacts later and does not protect the queue during the reaction window. The experiment isolates admission, not elasticity.
2. **Why p95 and accepted throughput?** Tail latency is what users feel. Accepted throughput shows the goodput that remains after shedding; offered rate alone hides the rejection cost.
3. **Why 7 locally and 4 per gateway on AWS?** Seven was tuned for two local workers at 50 requests/s. On AWS, four per gateway (eight total) gave better p95 than seven each (fourteen total), which rose to 500 ms.
4. **Why 503 with `Retry-After`?** It is an explicit, retryable rejection. The client can back off instead of waiting in an unbounded queue. A silent timeout hides the overload.
5. **Why did the baseline return 200 but take 8–24 s?** The queue was unbounded. Every request eventually completed, but the latency was unusable. HTTP 200 is not proof of a healthy service.
6. **Why 16.576 s for the ALB?** The health check runs every 5 s and needs two unhealthy checks. The timer starts at the termination request, so detection plus reporting produced 16.576 s.
7. **Why 502 and 504?** The ALB kept routing to the dead target until it was marked unhealthy. 502 means the target could not be reached; 504 means the client timed out after 10 s.
8. **Why 0.2 s locally?** The proxy polls every second and retries once on a connection error. It detects the dead gateway immediately, not through a two-check health threshold.
9. **What is the difference between 16.6 s, 5.3–14.3 s, and 24.3 s?** 16.6 s is the TargetHealth observation. 5.3–14.3 s is the range of request starts routed to the dead target. 24.3 s is the completion of the last 504 after its 10 s timeout.
10. **Why no global admission?** A shared limiter needs a consistent counter or coordinator, TTLs, and failure handling. That is a separate distributed-systems problem. Local limits sum, and total capacity drops during failover.
11. **What would you do next?** Bounded client retry for 502/504, a shared admission limit, multi-AZ placement, then TLS and authentication before any public deployment.
12. **What did you personally do?** You set the goals, approved the design, commissioned the runs, and reviewed the results. Implementation and execution were AI-assisted. Say that plainly and be ready to explain the code paths. Do not claim you wrote every line.

## Risks before publishing

- The upstream repository did not specify a license when the fork was prepared. Confirm whether you may publish or link the fork before putting the repository URL on a resume.
- The project is strongest when described as distributed systems and performance engineering, not as a general backend service. It has no database, authentication, or multi-tenant API.
- The 16.576 s failover result is a limitation, not a failure of the project. Present it as measured evidence and a clear next step.
