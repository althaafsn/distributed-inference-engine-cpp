#!/usr/bin/env python3
"""Run one local auto-tuning candidate against a real worker and gateway."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from autotune import loadgen, workloads


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_url(url: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"timeout waiting for {url}")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def start_services(args: argparse.Namespace, output: Path, phase: str):
    worker_ports = [free_port() for _ in range(args.workers)]
    gateway_port = free_port()
    env = os.environ.copy()
    env.update(
        {
            "ORT_INTRA_THREADS": str(args.threads),
            "BATCH_DEADLINE_MS": str(args.deadline),
            "MAX_BATCH_SIZE": str(args.max_batch),
            "CACHE_CAPACITY": str(args.cache),
            "GATEWAY_PORT": str(gateway_port),
            "ADMISSION_MAX_INFLIGHT": str(args.admission_max),
            "ADMISSION_ADAPTIVE": "1" if args.admission_adaptive else "0",
            "ADMISSION_MIN_INFLIGHT": str(args.admission_min),
            "ADMISSION_TARGET_P95_MS": str(args.admission_target_p95_ms),
            "ADMISSION_INTERVAL_MS": str(args.admission_interval_ms),
        }
    )
    logs = []
    workers = []
    gateway = None
    target = f"http://127.0.0.1:{gateway_port}"
    try:
        for index, port in enumerate(worker_ports, start=1):
            worker_log = open(output / f"{phase}-worker-{index}.log", "w")
            logs.append(worker_log)
            workers.append(
                subprocess.Popen(
                    [args.worker_bin, str(port), f"autotune_{index}", str(args.model)],
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            )
        gateway_log = open(output / f"{phase}-gateway.log", "w")
        logs.append(gateway_log)
        gateway = subprocess.Popen(
            [args.gateway_bin, *(f"localhost:{port}" for port in worker_ports)],
            stdout=gateway_log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        for port in worker_ports:
            wait_url(f"http://127.0.0.1:{port}/health")
        wait_url(f"{target}/stats")
        time.sleep(0.5)
    except Exception:
        stop_services(workers, gateway, logs)
        raise
    return workers, gateway, target, logs


def stop_services(workers, gateway, logs) -> None:
    for process in [gateway, *(workers or [])]:
        if process is None:
            continue
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for log in logs:
        log.close()


def run_candidate(args: argparse.Namespace) -> int:
    output = args.output
    if output.exists() and any(output.iterdir()) and not args.force:
        raise SystemExit(f"{output} is not empty; pass --force to replace it")
    output.mkdir(parents=True, exist_ok=True)
    manifest = workloads.load_manifest(args.data)
    model = Path(args.model)
    if not model.is_file():
        raise SystemExit(f"model not found: {model}")
    worker_bin = Path(args.worker_bin)
    gateway_bin = Path(args.gateway_bin)
    if not worker_bin.is_file():
        raise SystemExit(f"worker binary not found: {worker_bin}")
    if not os.access(worker_bin, os.X_OK):
        raise SystemExit(f"worker binary is not executable: {worker_bin}")
    if not gateway_bin.is_file():
        raise SystemExit(f"gateway binary not found: {gateway_bin}")
    if not os.access(gateway_bin, os.X_OK):
        raise SystemExit(f"gateway binary is not executable: {gateway_bin}")
    worker_bin_sha = file_sha(worker_bin)
    gateway_bin_sha = file_sha(gateway_bin)
    retry = loadgen.RetryConfig(
        attempts=args.retry_attempts,
        backoff_ms=args.retry_backoff_ms,
        max_backoff_ms=args.retry_max_backoff_ms,
        jitter=args.retry_jitter,
    )

    summaries = []
    if args.accuracy:
        workers = []
        gateway = None
        logs = []
        try:
            workers, gateway, target, logs = start_services(args, output, "accuracy")
            summary, rows, accuracy_result = asyncio.run(
                loadgen.run(
                    target,
                    args.data,
                    manifest,
                    "unique",
                    "binary",
                    args.accuracy_limit,
                    1,
                    None,
                    args.timeout,
                    True,
                    "accuracy",
                    retry=retry,
                )
            )
            summary["name"] = "accuracy"
            summaries.append(summary)
            (output / "accuracy-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            (output / "accuracy-requests.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            if accuracy_result is not None:
                (output / "accuracy.json").write_text(json.dumps(accuracy_result, indent=2) + "\n")
                print(json.dumps(accuracy_result), flush=True)
        finally:
            stop_services(workers, gateway, logs)

    effective_levels = [] if args.accuracy_only else ([1] if args.rate is not None else args.levels)
    for level in effective_levels:
        workers = []
        gateway = None
        logs = []
        try:
            workers, gateway, target, logs = start_services(args, output, f"level-{level}")
            count = (
                max(1, int(args.rate * args.duration)) if args.rate is not None else args.requests
            )
            concurrency = args.rate_concurrency if args.rate is not None else level
            summary, rows, _ = asyncio.run(
                loadgen.run(
                    target,
                    args.data,
                    manifest,
                    args.mode,
                    args.transport,
                    count,
                    concurrency,
                    args.rate,
                    args.timeout,
                    False,
                    f"rate{args.rate}" if args.rate is not None else f"c{level}",
                    args.seed,
                    retry=retry,
                )
            )
            label = f"rate{args.rate:g}-c{concurrency}" if args.rate is not None else f"c{level}"
            summary["name"] = f"{args.transport}-{args.mode}-{label}"
            summaries.append(summary)
            level_dir = output / f"level-{level}"
            level_dir.mkdir(exist_ok=True)
            with urllib.request.urlopen(f"{target}/stats", timeout=10) as response:
                (level_dir / "stats.json").write_text(
                    response.read().decode() + "\n", encoding="utf-8"
                )
            with urllib.request.urlopen(f"{target}/metrics", timeout=10) as response:
                (level_dir / "metrics.txt").write_text(
                    response.read().decode() + "\n", encoding="utf-8"
                )
            (level_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            (level_dir / "requests.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            print(json.dumps(summary), flush=True)
        finally:
            stop_services(workers, gateway, logs)

    run_record = {
        "model": str(model),
        "model_sha256": file_sha(model),
        "worker_bin": str(worker_bin),
        "worker_bin_sha256": worker_bin_sha,
        "gateway_bin": str(gateway_bin),
        "gateway_bin_sha256": gateway_bin_sha,
        "data_commit": manifest.get("dataset_commit"),
        "data_count": manifest.get("count"),
        "transport": args.transport,
        "mode": args.mode,
        "levels": effective_levels,
        "requests_per_level": (
            max(1, int(args.rate * args.duration)) if args.rate is not None else args.requests
        ),
        "rate": args.rate,
        "duration": args.duration,
        "rate_concurrency": args.rate_concurrency,
        "threads": args.threads,
        "deadline_ms": args.deadline,
        "max_batch": args.max_batch,
        "cache_capacity": args.cache,
        "workers": args.workers,
        "admission_max_inflight": args.admission_max,
        "admission_adaptive": args.admission_adaptive,
        "admission_min_inflight": args.admission_min,
        "admission_target_p95_ms": args.admission_target_p95_ms,
        "admission_interval_ms": args.admission_interval_ms,
        "retry": {
            "attempts": args.retry_attempts,
            "backoff_ms": args.retry_backoff_ms,
            "max_backoff_ms": args.retry_max_backoff_ms,
            "jitter": args.retry_jitter,
        },
        "summaries": summaries,
    }
    (output / "run.json").write_text(json.dumps(run_record, indent=2) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-bin", default="build/worker_node")
    parser.add_argument("--gateway-bin", default="build/gateway")
    parser.add_argument("--transport", choices=("binary", "json"), default="binary")
    parser.add_argument("--mode", choices=("unique", "repeated", "mixed"), default="unique")
    parser.add_argument(
        "--levels",
        type=int,
        nargs="+",
        default=[1, 2, 4],
        help="concurrency levels; ignored when --rate is set",
    )
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--rate", type=float)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--rate-concurrency", type=int, default=128)
    parser.add_argument("--accuracy", action="store_true")
    parser.add_argument("--accuracy-only", action="store_true")
    parser.add_argument("--accuracy-limit", type=int, default=100)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--deadline", type=int, default=10)
    parser.add_argument("--max-batch", type=int, default=32)
    parser.add_argument("--cache", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--admission-max", type=int, default=0)
    parser.add_argument("--admission-adaptive", action="store_true")
    parser.add_argument("--admission-min", type=int, default=1)
    parser.add_argument("--admission-target-p95-ms", type=int, default=400)
    parser.add_argument("--admission-interval-ms", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--retry-attempts", type=int, default=0)
    parser.add_argument("--retry-backoff-ms", type=float, default=100.0)
    parser.add_argument("--retry-max-backoff-ms", type=float, default=1000.0)
    parser.add_argument("--retry-jitter", type=float, default=0.2)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.admission_max < 0:
        parser.error("--admission-max must be non-negative")
    if args.admission_adaptive and args.admission_max <= 0:
        parser.error("--admission-adaptive requires --admission-max > 0")
    if args.admission_min < 1:
        parser.error("--admission-min must be at least 1")
    if args.admission_target_p95_ms < 1:
        parser.error("--admission-target-p95-ms must be at least 1")
    if args.admission_interval_ms < 100:
        parser.error("--admission-interval-ms must be at least 100")
    if args.rate_concurrency < 1:
        parser.error("--rate-concurrency must be at least 1")
    if args.retry_attempts < 0:
        parser.error("--retry-attempts must be non-negative")
    if args.retry_backoff_ms < 0 or args.retry_max_backoff_ms < 0:
        parser.error("retry backoff must be non-negative")
    if not 0 <= args.retry_jitter <= 1:
        parser.error("--retry-jitter must be between 0 and 1")
    return run_candidate(args)


if __name__ == "__main__":
    raise SystemExit(main())
