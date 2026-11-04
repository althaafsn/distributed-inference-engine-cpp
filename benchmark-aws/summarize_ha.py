#!/usr/bin/env python3
"""Summarize local HA runs into the sanitized gateway-HA artifact."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_run(run: Path) -> dict:
    run_json = load(run / "run.json")
    record = {
        "run": run.name,
        "observed_failover_seconds": run_json.get("observed_failover_seconds"),
    }
    previous_gateways = None
    previous_proxy = None
    normal_gateway1 = None
    for phase in ("normal", "failover", "recovery"):
        summary = load(run / phase / "summary.json")
        state = load(run / phase / "state.json")
        proxy = state["proxy"]
        gateway_deltas = []
        for index, gateway in enumerate(state["gateways"]):
            admission = gateway.get("admission", {})
            if "error" in gateway:
                gateway_deltas.append(
                    {"gateway_id": gateway.get("gateway_id"), "error": gateway["error"]}
                )
                continue
            if (
                previous_gateways
                and index < len(previous_gateways)
                and "error" not in previous_gateways[index]
            ):
                previous = previous_gateways[index].get("admission", {})
                accepted = admission.get("accepted", 0) - previous.get("accepted", 0)
                rejected = admission.get("rejected", 0) - previous.get("rejected", 0)
            else:
                accepted = admission.get("accepted", 0)
                rejected = admission.get("rejected", 0)
            gateway_deltas.append(
                {
                    "gateway_id": gateway.get("gateway_id"),
                    "accepted": accepted,
                    "rejected": rejected,
                    "healthy": True,
                }
            )
        if phase == "normal":
            normal_gateway1 = state["gateways"][0]["admission"]
        if previous_proxy:
            proxy_requests = sum(item["requests"] for item in proxy["upstreams"]) - sum(
                item["requests"] for item in previous_proxy["upstreams"]
            )
            failovers = proxy["failovers"] - previous_proxy["failovers"]
        else:
            proxy_requests = sum(item["requests"] for item in proxy["upstreams"])
            failovers = proxy["failovers"]
        record[phase] = {
            "requests": summary["requests"],
            "successes": summary["successes"],
            "failures": summary["failures"],
            "accepted_rps": summary["successful_requests_per_second"],
            "wall_seconds": summary["wall_seconds"],
            "latency_ms": summary["latency_ms"],
            "proxy_requests": proxy_requests,
            "proxy_failovers": failovers,
            "gateways": gateway_deltas,
            "upstream_health": [
                {
                    "gateway_id": item["gateway_id"],
                    "healthy": item["healthy"],
                    "last_error": item["last_error"],
                }
                for item in proxy["upstreams"]
            ],
        }
        if phase == "failover":
            before = run_json["failover_gateway1_before_kill"]["admission"]
            record["failover"]["gateway1_before_kill"] = {
                "accepted": before["accepted"] - normal_gateway1["accepted"],
                "rejected": before["rejected"] - normal_gateway1["rejected"],
            }
        previous_gateways = state["gateways"]
        previous_proxy = proxy
    return record


def aggregate(records: list[dict], phase: str) -> dict:
    subset = [record[phase] for record in records]

    def stats(key: str) -> dict:
        values = [item[key] for item in subset]
        return {
            "median": round(statistics.median(values), 3),
            "min": min(values),
            "max": max(values),
        }

    return {
        "runs": len(subset),
        "successes": stats("successes"),
        "failures": stats("failures"),
        "accepted_rps": stats("accepted_rps"),
        "wall_seconds": stats("wall_seconds"),
        "p95_ms": {
            "median": round(statistics.median([item["latency_ms"]["p95"] for item in subset]), 3),
            "min": min(item["latency_ms"]["p95"] for item in subset),
            "max": max(item["latency_ms"]["p95"] for item in subset),
        },
        "p99_ms": {
            "median": round(statistics.median([item["latency_ms"]["p99"] for item in subset]), 3),
            "min": min(item["latency_ms"]["p99"] for item in subset),
            "max": max(item["latency_ms"]["p99"] for item in subset),
        },
        "proxy_failovers": sum(item["proxy_failovers"] for item in subset),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--normal7", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    records = [summarize_run(run) for run in args.run]
    run_json = load(args.run[0] / "run.json")
    limit7 = None
    if args.normal7 is not None:
        summary = load(args.normal7 / "normal" / "summary.json")
        limit7 = {
            "successes": summary["successes"],
            "failures": summary["failures"],
            "accepted_rps": summary["successful_requests_per_second"],
            "p95_ms": summary["latency_ms"]["p95"],
            "p99_ms": summary["latency_ms"]["p99"],
        }
    payload = {
        "schema_version": 1,
        "date": "2026-11-04",
        "scope": "Local two-gateway HA and failover comparison. Not an AWS or production result.",
        "binaries": {
            "gateway_sha256": run_json["gateway_sha256"],
            "worker_sha256": run_json["worker_sha256"],
        },
        "topology": {
            "workers": 2,
            "gateways": 2,
            "ha_proxy": "local aiohttp proxy, round-robin with 1 s health checks and one connection-error retry",
            "admission_max_per_gateway": 4,
            "total_admission_limit": 8,
            "model": "resnet50-v2-7",
            "data": "500-image ImageNet subset, unique, cold cache (CACHE_CAPACITY=1)",
            "transport": "raw float32",
            "rate_rps": 50,
            "duration_seconds": 10,
            "concurrency": 128,
            "failover_kill_after_seconds": 2,
        },
        "records": records,
        "aggregates": {
            phase: aggregate(records, phase) for phase in ("normal", "failover", "recovery")
        },
        "limit_7_normal": limit7,
        "notes": [
            "The HA proxy is a local test harness, not a production load balancer.",
            "Admission is per gateway. Limit 4 per gateway gives a total of 8; limit 7 per gateway gives a total of 14.",
            "During failover the surviving gateway keeps its local limit, so total admission capacity drops.",
            "The proxy retries only on connection errors, not on 503 responses.",
            "All three failover runs had one expected upstream ClientConnectorError, retried successfully. There were no client-visible connection failures and no proxy-generated both-down 503.",
            "No AWS resources were used; cost is $0.",
        ],
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({phase: payload["aggregates"][phase] for phase in payload["aggregates"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
