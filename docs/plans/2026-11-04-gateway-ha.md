# Gateway HA and failover

## Objective

Remove the single-gateway failure point for the local benchmark topology:

- run two gateway processes against the same two workers,
- put a small HA proxy in front with health checks and failover,
- measure normal operation, gateway loss, and gateway recovery,
- document the per-gateway admission tradeoff.

## Non-goals

- No production load balancer, TLS, authentication, or public deployment.
- No AWS run in this phase. The local topology costs $0.
- No global distributed admission limit. The current limit is per gateway.
- No multi-AZ or autoscaling claim.

## Verified facts

- The gateway is a single process and a single point of failure.
- Admission is local to each gateway; two gateways each with limit 7 allow up to 14 in flight.
- The local host has 16 logical CPUs; port 8000 is used by an unrelated process, so the test uses high ports.
- `nginx` and `haproxy` are not installed. A small Python HA proxy will be used as a test harness.
- No tagged AWS instances or volumes are running. The project total is $3.4446 of $10.

## Design

### Gateway health

Add `GET /health` to the gateway. It returns HTTP 200 and a small JSON body with the gateway ID. It is never admission-rejected. `GATEWAY_ID` is read from the environment and defaults to `gateway`.

### HA proxy

Add `benchmark-aws/ha_proxy.py` using `aiohttp`:

- Listen on a configurable port.
- Accept a list of gateway base URLs.
- Poll each gateway `/health` every second.
- Round-robin only healthy gateways.
- Forward method, path, body, and content type to the selected gateway.
- On a connection error or timeout, retry the other healthy gateway once.
- Return 503 with `Retry-After: 1` only if no healthy gateway remains.
- Expose `/ha/stats` with upstream health, request counts, failovers, and gateway IDs.

This is an explicitly labeled test harness, not a production load balancer.

### Orchestration

Add `benchmark-aws/run_ha.py`:

- Start two workers, two gateways, and the HA proxy on free high ports.
- Use `ADMISSION_MAX_INFLIGHT` per gateway; default 4 (about 8 total).
- Run a normal load phase, kill one gateway, run a failover phase, restart it, and run a recovery phase.
- Collect load summaries, proxy stats, each gateway `/stats`, and worker health.
- Clean up every process and print a sanitized summary.

### Experiment

- 2 workers, ResNet50, 500 unique images, cache 1, binary transport.
- 50 offered requests/s, 10 seconds, 128 connections.
- Per-gateway admission limit 4.
- Normal: 10 seconds through both gateways.
- Failover: kill gateway 1 after 2 seconds; the proxy must route to gateway 2.
- Recovery: restart gateway 1; traffic must return to both gateways.
- One additional normal run with per-gateway limit 7 to show the total-limit effect.

## Affected files

- `src/gateway.cpp` (`/health`, `GATEWAY_ID`)
- `benchmark-aws/ha_proxy.py` (new)
- `benchmark-aws/run_ha.py` (new)
- `tests/test_ha_proxy.py` (new)
- `tests/test_gateway_health.py` (new)
- `.github/workflows/ci.yml` (health test in the cpp job)
- reports and experiment ledger after the run

## Risks and controls

- **Custom proxy overhead:** it shares the host CPU and is not production software. Report it as a test harness.
- **Retry duplication:** inference is idempotent; the proxy retries only on connection failure, not on 503.
- **Per-gateway admission:** the total limit doubles with two gateways. Test 4 each and 7 each; report the tradeoff.
- **Failover detection delay:** one-second health interval; measure the actual failover window.
- **Port conflicts:** use free high ports; never touch the unrelated port-8000 process.
- **No global limiter:** the surviving gateway keeps its local limit, so capacity drops during failover. State this explicitly.

## Acceptance criteria

1. Two gateways both serve traffic through the proxy.
2. Killing one gateway causes the proxy to mark it down and route new requests to the survivor with no proxy-level 5xx other than controlled 503s.
3. Restarting the gateway returns it to rotation.
4. The report includes normal, failover, and recovery p95, 503 rate, accepted throughput, and per-gateway counters.
5. Proxy tests cover round-robin, health removal, failover, recovery, and both-down behavior.
6. Full Python suite, CTest, Ruff, source-delivery checks, and CI pass.
7. Report and experiment ledger state the per-gateway admission and single-host limits; no resume edit.

## Verification

```sh
python3 -m unittest tests.test_ha_proxy tests.test_gateway_health -v
BENCH_WORKER_BIN=... BENCH_GATEWAY_BIN=... BENCH_MODEL=... \
  python3 benchmark-aws/run_ha.py --rate 50 --duration 10
ctest --test-dir /tmp/opencode/cmake-build-hardening --output-on-failure
python3 -m unittest discover -s tests -v
ruff check benchmark-aws tests
```
