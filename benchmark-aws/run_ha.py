#!/usr/bin/env python3
"""Run a local two-gateway HA and failover benchmark."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark-aws"))

from autotune import loadgen, workloads  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_url(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError(f"timeout waiting for {url}")


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def stop_process(process: subprocess.Popen | None, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def start_worker(
    args: argparse.Namespace, index: int, port: int, env: dict, logs: list
) -> subprocess.Popen:
    log = open(args.output / f"worker-{index}.log", "w")
    logs.append(log)
    return subprocess.Popen(
        [args.worker_bin, str(port), f"ha_worker_{index}", str(args.model)],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )


def start_gateway(
    args: argparse.Namespace, index: int, port: int, env: dict, logs: list
) -> subprocess.Popen:
    log_path = args.output / f"gateway-{index}.log"
    log = open(log_path, "a" if log_path.exists() else "w")
    logs.append(log)
    gateway_env = env.copy()
    gateway_env["GATEWAY_PORT"] = str(port)
    gateway_env["GATEWAY_ID"] = f"gateway-{index}"
    gateway_env["ADMISSION_MAX_INFLIGHT"] = str(args.admission_max)
    return subprocess.Popen(
        [
            args.gateway_bin,
            f"127.0.0.1:{args.worker_ports[0]}",
            f"127.0.0.1:{args.worker_ports[1]}",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=gateway_env,
    )


def start_proxy(args: argparse.Namespace, port: int, logs: list) -> subprocess.Popen:
    log = open(args.output / "proxy.log", "w")
    logs.append(log)
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "benchmark-aws" / "ha_proxy.py"),
            "--port",
            str(port),
            "--upstream",
            f"http://127.0.0.1:{args.gateway_ports[0]}",
            "--upstream",
            f"http://127.0.0.1:{args.gateway_ports[1]}",
            "--health-interval",
            "1.0",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )


async def run_load(args: argparse.Namespace, manifest: dict, phase: str) -> dict:
    count = max(1, int(args.rate * args.duration))
    summary, rows, _ = await loadgen.run(
        f"http://127.0.0.1:{args.proxy_port}",
        args.data,
        manifest,
        "unique",
        "binary",
        count,
        args.concurrency,
        args.rate,
        args.timeout,
        False,
        phase,
    )
    phase_dir = args.output / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (phase_dir / "requests.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return summary


def collect_state(args: argparse.Namespace, phase: str, gateways: list) -> dict:
    state = {"proxy": get_json(f"http://127.0.0.1:{args.proxy_port}/ha/stats")}
    gateway_stats = []
    for index, process in enumerate(gateways, start=1):
        if process is None or process.poll() is not None:
            gateway_stats.append({"gateway_id": f"gateway-{index}", "error": "not running"})
            continue
        try:
            gateway_stats.append(
                get_json(f"http://127.0.0.1:{args.gateway_ports[index - 1]}/stats")
            )
        except (OSError, urllib.error.URLError) as exc:
            gateway_stats.append({"gateway_id": f"gateway-{index}", "error": type(exc).__name__})
    state["gateways"] = gateway_stats
    state["workers"] = [get_json(f"http://127.0.0.1:{port}/health") for port in args.worker_ports]
    (args.output / phase / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-bin", default="build/worker_node")
    parser.add_argument("--gateway-bin", default="build/gateway")
    parser.add_argument("--rate", type=float, default=50.0)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=128)
    parser.add_argument("--admission-max", type=int, default=4)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--kill-after", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--normal-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.output.exists() and any(args.output.iterdir()) and not args.force:
        parser.error(f"{args.output} is not empty; pass --force")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = workloads.load_manifest(args.data)
    args.worker_ports = [free_port(), free_port()]
    args.gateway_ports = [free_port(), free_port()]
    args.proxy_port = free_port()
    env = os.environ.copy()
    env.update(
        {
            "ORT_INTRA_THREADS": str(args.threads),
            "BATCH_DEADLINE_MS": "10",
            "MAX_BATCH_SIZE": "32",
            "CACHE_CAPACITY": "1",
            "ROUTING_POLICY": "least_loaded",
            "ADMISSION_ADAPTIVE": "0",
        }
    )
    logs: list = []
    workers = []
    gateways = [None, None]
    proxy = None
    run_record = {
        "rate": args.rate,
        "duration": args.duration,
        "admission_max_per_gateway": args.admission_max,
        "worker_ports": args.worker_ports,
        "gateway_ports": args.gateway_ports,
        "proxy_port": args.proxy_port,
        "gateway_sha256": hashlib.sha256(Path(args.gateway_bin).read_bytes()).hexdigest(),
        "worker_sha256": hashlib.sha256(Path(args.worker_bin).read_bytes()).hexdigest(),
    }
    try:
        for index, port in enumerate(args.worker_ports, start=1):
            workers.append(start_worker(args, index, port, env, logs))
        for port in args.worker_ports:
            wait_url(f"http://127.0.0.1:{port}/health")
        for index, port in enumerate(args.gateway_ports, start=1):
            gateways[index - 1] = start_gateway(args, index, port, env, logs)
        for port in args.gateway_ports:
            wait_url(f"http://127.0.0.1:{port}/health")
        proxy = start_proxy(args, args.proxy_port, logs)
        wait_url(f"http://127.0.0.1:{args.proxy_port}/ha/stats")

        print("normal phase", flush=True)
        run_record["normal"] = asyncio.run(run_load(args, manifest, "normal"))
        run_record["normal_state"] = collect_state(args, "normal", gateways)
        if args.normal_only:
            (args.output / "run.json").write_text(json.dumps(run_record, indent=2) + "\n")
            return 0

        print("failover phase", flush=True)

        async def failover() -> dict:
            task = asyncio.create_task(run_load(args, manifest, "failover"))
            await asyncio.sleep(args.kill_after)
            gateways[0].terminate()
            while gateways[0].poll() is None:
                await asyncio.sleep(0.05)
            run_record["gateway1_terminated_at_seconds"] = args.kill_after
            return await task

        run_record["failover"] = asyncio.run(failover())
        run_record["failover_state"] = collect_state(args, "failover", gateways)

        print("recovery phase", flush=True)
        gateways[0] = start_gateway(args, 1, args.gateway_ports[0], env, logs)
        wait_url(f"http://127.0.0.1:{args.gateway_ports[0]}/health")
        time.sleep(1.2)
        run_record["recovery"] = asyncio.run(run_load(args, manifest, "recovery"))
        run_record["recovery_state"] = collect_state(args, "recovery", gateways)
        (args.output / "run.json").write_text(json.dumps(run_record, indent=2) + "\n")
        return 0
    finally:
        stop_process(proxy)
        for gateway in gateways:
            stop_process(gateway)
        for worker in workers:
            stop_process(worker)
        for log in logs:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
