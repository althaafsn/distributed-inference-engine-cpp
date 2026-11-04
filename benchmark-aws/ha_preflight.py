#!/usr/bin/env python3
"""Preflight and cost estimate for the AWS internal-ALB HA test."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark-aws"))

import controller  # noqa: E402

C6I_LARGE_HOURLY = 0.085
C6I_XLARGE_HOURLY = 0.17
ALB_HOURLY = 0.0225
ALB_LCU_HOURLY = 0.008
IPV4_HOURLY = 0.005
GP3_GB_MONTH = 0.08
GB_PER_INSTANCE = 12
BUDGET_USD = 10.0
SPENT_USD = 3.444618
CAP_USD = 1.50


def estimate() -> dict:
    compute = 4 * C6I_LARGE_HOURLY + C6I_XLARGE_HOURLY
    alb = ALB_HOURLY + ALB_LCU_HOURLY
    ipv4 = 5 * IPV4_HOURLY
    storage = 5 * GB_PER_INSTANCE * GP3_GB_MONTH / (30 * 24)
    hourly = compute + alb + ipv4 + storage
    return {
        "instances": {"c6i.large": 4, "c6i.xlarge": 1},
        "load_balancer": "internal application, 1 listener, 1 target group",
        "compute_hourly_usd": round(compute, 4),
        "alb_hourly_usd": round(alb, 4),
        "ipv4_hourly_usd": round(ipv4, 4),
        "gp3_hourly_usd": round(storage, 4),
        "hourly_usd": round(hourly, 4),
        "estimate_45_min_usd": round(hourly * 0.75, 4),
        "estimate_60_min_usd": round(hourly, 4),
        "cap_usd": CAP_USD,
        "spent_usd": SPENT_USD,
        "remaining_budget_usd": round(BUDGET_USD - SPENT_USD, 6),
    }


def choose_subnets(subnets: list[dict]) -> list[dict]:
    usable = [
        subnet
        for subnet in subnets
        if subnet.get("MapPublicIpOnLaunch") and subnet.get("AvailableIpAddressCount", 0) >= 16
    ]
    if not usable:
        usable = [subnet for subnet in subnets if subnet.get("AvailableIpAddressCount", 0) >= 16]
    by_az: dict[str, dict] = {}
    for subnet in sorted(usable, key=lambda item: item["SubnetId"]):
        by_az.setdefault(subnet["AvailabilityZone"], subnet)
    if len(by_az) < 2:
        raise ValueError("need at least two subnets in different availability zones")
    return [by_az[az] for az in sorted(by_az)[:2]]


def run_preflight() -> int:
    context = controller.preflight()
    subnets = controller.aws(
        "ec2",
        "describe-subnets",
        "--filters",
        f"Name=vpc-id,Values={context['vpc']}",
        "--query",
        "Subnets[].{SubnetId:SubnetId,AvailabilityZone:AvailabilityZone,"
        "MapPublicIpOnLaunch:MapPublicIpOnLaunch,"
        "AvailableIpAddressCount:AvailableIpAddressCount}",
        "--output",
        "json",
    )
    chosen = choose_subnets(subnets)
    active_instances = controller.aws(
        "ec2",
        "describe-instances",
        "--filters",
        "Name=tag-key,Values=temporary-inference-benchmark",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
        "--query",
        "Reservations[].Instances[].InstanceId",
        "--output",
        "json",
    )
    active_volumes = controller.aws(
        "ec2",
        "describe-volumes",
        "--filters",
        "Name=tag-key,Values=temporary-inference-benchmark",
        "--query",
        "Volumes[].VolumeId",
        "--output",
        "json",
    )
    load_balancers = controller.aws(
        "elbv2",
        "describe-load-balancers",
        "--query",
        "LoadBalancers[].LoadBalancerName",
        "--output",
        "json",
    )
    result = {
        "account": context["account"],
        "region": context["region"],
        "vpc": context["vpc"],
        "subnets": chosen,
        "active_tagged_instances": active_instances,
        "active_tagged_volumes": active_volumes,
        "load_balancers": load_balancers,
        "cost": estimate(),
    }
    print(json.dumps(result, indent=2))
    return 0 if not active_instances and not active_volumes else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("preflight",))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "topology": {
                        "gateways": 2,
                        "workers": 2,
                        "load": 1,
                        "alb": 1,
                        "internal": True,
                    },
                    "cost": estimate(),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "preflight":
        return run_preflight()
    parser.error("choose preflight or --dry-run")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
