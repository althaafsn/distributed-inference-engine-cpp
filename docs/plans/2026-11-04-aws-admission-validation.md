# AWS multi-host admission validation

## Objective

Validate the gateway admission limit on a real multi-host EC2 topology: one gateway, two workers, and a separate load generator. Compare baseline (admission disabled) against `ADMISSION_MAX_INFLIGHT=7` at 25, 50, and 100 offered requests/s.

## Non-goals

- No public deployment, DNS, TLS, authentication, or per-user quota.
- No autoscaler, multi-AZ failover, or distributed rate limiter.
- No model or worker-batching change.
- No resume edit.
- No load test against a live user service.

## Verified facts

- AWS identity and the `us-east-1` region were verified with `sts get-caller-identity`.
- The project had spent about `$3.312` of the `$10` ceiling before this run; the cost ledger records the current total.
- `benchmark-aws/multihost_controller.py` already provisions one gateway, 1–8 workers, and one load generator with restrictive security groups and verified cleanup.
- The controller supports one open-loop hold (`--hold-rate`) but does not pass `ADMISSION_MAX_INFLIGHT` and does not run a paired matrix.
- The local 2-worker result at 50 req/s was baseline p95 5.6–8.4 s versus admission p95 372–376 ms. Accepted throughput was similar.
- The gateway admission code is already on `main` and is included in the benchmark source payload.

## Design

Extend `benchmark-aws/multihost_controller.py`:

- Add `--admission-max N` (default 0).
- Add `--hold-rates R [R ...]`, `--hold-repeats N`, and `--hold-matrix`.
- In matrix mode, run baseline and admission alternately for each repeat:
  - repeat 0: baseline, admission
  - repeat 1: admission, baseline
  - repeat 2: baseline, admission
- Pass `ADMISSION_MAX_INFLIGHT=<value>` to the gateway environment for each condition.
- Restart workers and gateway before every condition so the cache and counters start clean.
- Label each run `hold-rate<R>-a<limit>-r<repeat>`.
- Collect the load archive once at the end of the matrix, and collect gateway/worker metrics after each condition.

Topology:

- 1 × `c6i.xlarge` load generator.
- 1 × `c6i.large` gateway.
- 2 × `c6i.large` workers.
- `us-east-1`, public subnets, SSH only from the owner `/32`, gateway port 8000 only from the load group, worker port 8001 only from the gateway group.

Experiment:

- ResNet50-v2-7, 500 unique images, `CACHE_CAPACITY=1`, binary transport, 2 ORT threads per worker.
- Offered rates: 25, 50, 100 requests/s.
- Duration: 10 seconds per run.
- Repeats: 3 at each rate.
- Routing: `least_loaded` for both conditions.
- Admission limit: 7.

## Cost

Compute estimate from the controller: `(2 workers + 1 gateway) * $0.085 + 1 load * $0.17 = $0.425/hour`. Add about `$0.03/hour` for four public IPv4 addresses and 48 GB of gp3 storage. Expected setup, 18 runs, and cleanup: about 15 minutes, or roughly **$0.15–$0.30**. Hard cap for this experiment: **$1.50**. The controller schedules a 60-minute instance termination and limits the workload to 30 minutes.

## Risks and controls

- **Cleanup failure:** the controller records instance, volume, and security-group IDs in `state.json`, terminates them, and verifies termination. If cleanup fails, stop and use the recorded state; do not touch unrelated resources.
- **Instance or network failure:** the run fails closed and cleanup still runs in `finally`.
- **Model download or build failure:** the existing controller already handles this; no new deployment path.
- **Noise:** conditions alternate order and restart services. Instance-to-instance variation remains a limit.
- **No retry client:** the load generator does not retry after `503`. The shed rate is a property of the offered load, not a client policy.
- **Billing delay:** Cost Explorer is delayed. The ledger uses conservative runtime estimates.

## Acceptance criteria

1. Controller unit tests pass and the dry-run reports the expected topology and cost.
2. AWS preflight passes and no application port is open to the public internet.
3. The matrix completes at 25, 50, and 100 requests/s with three repeats per condition.
4. At 50 and 100 requests/s, admission accepted p95 is lower than baseline and all excess responses are controlled `503`s.
5. All instances, volumes, and security groups are terminated and verified.
6. `docs/overload-protection.md`, the sanitized JSON, `EXPERIMENTS.md`, and the cost ledger record the AWS result, cost, and limits.
7. No resume or unrelated project is modified.

## Verification

```sh
python3 -m unittest tests.test_multihost_setup -v
python3 -m unittest discover -s tests -v
python3 benchmark-aws/multihost_controller.py --dry-run --workers 2
python3 benchmark-aws/controller.py preflight
```

Then the bounded run, followed by cleanup verification and a cost check.
