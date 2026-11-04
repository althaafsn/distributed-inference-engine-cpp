"""Async real-image load generator for the auto-tuning harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import resource
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp

try:
    from .workloads import load_manifest, select_images, tensor_bytes
except ImportError:
    from workloads import load_manifest, select_images, tensor_bytes


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 0
    backoff_ms: float = 100.0
    max_backoff_ms: float = 1000.0
    jitter: float = 0.2


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


def retry_delay_seconds(
    attempt: int,
    config: RetryConfig,
    retry_after: float | None = None,
    random_value: float | None = None,
) -> float:
    base = config.backoff_ms / 1000.0
    exponential = min(config.max_backoff_ms / 1000.0, base * (2 ** max(0, attempt - 1)))
    if config.jitter > 0:
        value = random.random() if random_value is None else random_value
        exponential *= 1.0 + config.jitter * (2.0 * value - 1.0)
        exponential = min(config.max_backoff_ms / 1000.0, max(0.0, exponential))
    # Retry-After is a server minimum. Jitter must never reduce it.
    return max(0.0, max(exponential, retry_after or 0.0))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def build_specs(
    data_dir: Path,
    manifest: dict,
    mode: str,
    count: int,
    transport: str,
    prefix: str,
    seed: int = 0,
) -> list[dict]:
    entries = select_images(manifest, mode, count, seed)
    # JSON bodies are large; 200 requests is enough for a transport comparison
    # while keeping the precomputed payload set below about 250 MB.
    if transport == "json" and count > 200:
        raise ValueError("JSON transport is limited to 200 requests; use binary for capacity")
    payload_cache: dict[str, bytes] = {}
    specs = []
    for index, entry in enumerate(entries):
        request_id = f"{prefix}-{index}"
        if transport == "binary":
            key = entry["tensor_path"]
            payload = payload_cache.get(key)
            if payload is None:
                payload = tensor_bytes(data_dir, entry)
                payload_cache[key] = payload
            specs.append({"request_id": request_id, "entry": entry, "payload": payload})
        else:
            tensor = tensor_bytes(data_dir, entry)
            body = json.dumps(
                {
                    "request_id": request_id,
                    "input_data": list(struct.unpack(f"<{len(tensor) // 4}f", tensor)),
                }
            ).encode()
            specs.append({"request_id": request_id, "entry": entry, "body": body})
    return specs


async def _attempt(
    session: aiohttp.ClientSession,
    target: str,
    spec: dict,
    transport: str,
    collect_output: bool,
) -> dict:
    result = {"status": None, "success": False}
    try:
        if transport == "binary":
            async with session.post(
                target + "/infer_tensor",
                data=spec["payload"],
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Request-Id": spec["request_id"],
                },
            ) as response:
                body = await response.read()
                result["status"] = response.status
                result["retry_after"] = response.headers.get("Retry-After")
                result["cached"] = response.headers.get("X-Cached") == "true"
                result["node_id"] = response.headers.get("X-Node-Id")
                result["batch_size"] = int(response.headers.get("X-Batch-Size", "0"))
                result["queue_wait_us"] = int(response.headers.get("X-Queue-Wait-Us", "0"))
                result["batch_execution_us"] = int(
                    response.headers.get("X-Batch-Execution-Us", "0")
                )
                if response.status == 200:
                    if not result["cached"] and not body:
                        raise ValueError("empty binary response")
                    output = list(struct.unpack(f"<{len(body) // 4}f", body))
                    result["success"] = True
                    if collect_output:
                        result["output"] = output
        else:
            async with session.post(
                target + "/infer",
                data=spec["body"],
                headers={"Content-Type": "application/json"},
            ) as response:
                body = await response.read()
                result["status"] = response.status
                result["retry_after"] = response.headers.get("Retry-After")
                if response.status == 200:
                    parsed = json.loads(body)
                    result["cached"] = bool(parsed.get("cached"))
                    result["node_id"] = parsed.get("node_id")
                    result["batch_size"] = parsed.get("batch_size") or 0
                    result["queue_wait_us"] = parsed.get("queue_wait_us") or 0
                    result["batch_execution_us"] = parsed.get("batch_execution_us") or 0
                    result["success"] = bool(parsed.get("output_data"))
                    if collect_output:
                        result["output"] = parsed["output_data"]
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
        result["error"] = type(exc).__name__
    return result


async def _send_one(
    session: aiohttp.ClientSession,
    target: str,
    spec: dict,
    transport: str,
    collect_output: bool,
    retry: RetryConfig | None = None,
) -> dict:
    retry = retry or RetryConfig()
    started = time.perf_counter()
    row = {
        "request_id": spec["request_id"],
        "label_index": spec["entry"]["label_index"],
    }
    attempts = 0
    retried = False
    while True:
        attempts += 1
        result = await _attempt(session, target, spec, transport, collect_output)
        row.update(result)
        if result.get("success") or result.get("status") != 503 or attempts > retry.attempts:
            break
        retried = True
        delay = retry_delay_seconds(attempts, retry, parse_retry_after(result.get("retry_after")))
        await asyncio.sleep(delay)
    row["attempts"] = attempts
    row["retried"] = retried
    row["retry_success"] = retried and bool(row.get("success"))
    row["retry_exhausted"] = retried and not row.get("success")
    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return row


async def _run_session(
    target: str,
    specs: list[dict],
    concurrency: int,
    transport: str,
    timeout: float,
    collect_output: bool,
    rate: float | None,
    retry: RetryConfig | None = None,
) -> tuple[list[dict], float]:
    connector = aiohttp.TCPConnector(limit=max(1, concurrency), force_close=False)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    started = time.monotonic()
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
        if rate is None:
            queue: asyncio.Queue = asyncio.Queue()
            for spec in specs:
                queue.put_nowait(spec)

            async def worker() -> None:
                while True:
                    try:
                        spec = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    rows.append(
                        await _send_one(session, target, spec, transport, collect_output, retry)
                    )

            rows: list[dict] = []
            await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
        else:
            rows = []
            tasks = []
            schedule_start = time.monotonic()
            for index, spec in enumerate(specs):
                target_time = schedule_start + index / rate
                delay = target_time - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                tasks.append(
                    asyncio.create_task(
                        _send_one(session, target, spec, transport, collect_output, retry)
                    )
                )
            rows.extend(await asyncio.gather(*tasks))
    return rows, time.monotonic() - started


def summarize(
    rows: list[dict],
    elapsed_seconds: float,
    transport: str,
    mode: str,
    concurrency: int | None = None,
    rate: float | None = None,
) -> dict:
    successful = [row for row in rows if row.get("success")]
    latencies = [row["latency_ms"] for row in successful]
    failures = [row for row in rows if not row.get("success")]
    error_rate = len(failures) / len(rows) if rows else 0.0
    cache_hits = sum(bool(row.get("cached")) for row in successful)
    node_counts: dict[str, int] = {}
    for row in successful:
        node = row.get("node_id")
        if node:
            node_counts[node] = node_counts.get(node, 0) + 1
    batch_sizes = [row["batch_size"] for row in successful if row.get("batch_size")]
    cpu = resource.getrusage(resource.RUSAGE_SELF)
    client_cpu_percent = (
        (cpu.ru_utime + cpu.ru_stime) / elapsed_seconds * 100 if elapsed_seconds else 0
    )
    p50 = percentile(latencies, 0.50)
    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)
    within_slo = (
        bool(latencies)
        and p95 is not None
        and p95 <= 500
        and p99 is not None
        and p99 <= 1000
        and error_rate <= 0.001
    )
    attempts = sum(row.get("attempts", 1) for row in rows)
    retried_requests = sum(1 for row in rows if row.get("retried"))
    retry_successes = sum(1 for row in rows if row.get("retry_success"))
    retry_exhausted = sum(1 for row in rows if row.get("retry_exhausted"))
    return {
        "mode": mode,
        "transport": transport,
        "concurrency": concurrency,
        "rate": rate,
        "requests": len(rows),
        "successes": len(successful),
        "failures": len(failures),
        "error_rate": round(error_rate, 6),
        "wall_seconds": round(elapsed_seconds, 3),
        "successful_requests_per_second": round(len(successful) / elapsed_seconds, 3)
        if elapsed_seconds
        else 0,
        "latency_ms": {"p50": p50, "p95": p95, "p99": p99},
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / len(successful), 4) if successful else 0,
        "node_counts": node_counts,
        "batch_size_mean": round(sum(batch_sizes) / len(batch_sizes), 2) if batch_sizes else None,
        "client_cpu_percent": round(client_cpu_percent, 2),
        "slo": {
            "p95_ms": 500,
            "p99_ms": 1000,
            "error_rate": 0.001,
            "within_slo": within_slo,
        },
        "retry": {
            "attempts": attempts,
            "retried_requests": retried_requests,
            "retry_successes": retry_successes,
            "retry_exhausted": retry_exhausted,
        },
        "errors": [
            {"status": row.get("status"), "error": row.get("error")} for row in failures[:20]
        ],
    }


def accuracy(rows: list[dict]) -> dict:
    if not rows:
        return {"samples": 0, "top1": None, "top5": None}
    top1 = 0
    top5 = 0
    samples = 0
    for row in rows:
        output = row.get("output")
        if not output:
            continue
        samples += 1
        ranked = sorted(range(len(output)), key=lambda index: output[index], reverse=True)
        label = row["label_index"]
        if ranked[0] == label:
            top1 += 1
        if label in ranked[:5]:
            top5 += 1
    return {
        "samples": samples,
        "top1": round(top1 / samples, 4) if samples else None,
        "top5": round(top5 / samples, 4) if samples else None,
    }


async def run(
    target: str,
    data_dir: Path,
    manifest: dict,
    mode: str,
    transport: str,
    count: int,
    concurrency: int,
    rate: float | None,
    timeout: float,
    collect_output: bool,
    prefix: str,
    seed: int = 0,
    retry: RetryConfig | None = None,
) -> tuple[dict, list[dict], dict | None]:
    specs = build_specs(data_dir, manifest, mode, count, transport, prefix, seed)
    rows, elapsed = await _run_session(
        target,
        specs,
        concurrency,
        transport,
        timeout,
        collect_output,
        rate,
        retry,
    )
    summary = summarize(rows, elapsed, transport, mode, concurrency, rate)
    accuracy_result = accuracy(rows) if collect_output else None
    return summary, rows, accuracy_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="http://127.0.0.1:8000")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--mode", choices=("unique", "repeated", "mixed"), default="unique")
    parser.add_argument("--transport", choices=("binary", "json"), default="binary")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--rate", type=float)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--accuracy", action="store_true")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--retry-attempts", type=int, default=0)
    parser.add_argument("--retry-backoff-ms", type=float, default=100.0)
    parser.add_argument("--retry-max-backoff-ms", type=float, default=1000.0)
    parser.add_argument("--retry-jitter", type=float, default=0.2)
    args = parser.parse_args(argv)

    if not args.allow_remote and not args.target.startswith(
        ("http://127.0.0.1", "http://localhost")
    ):
        parser.error("refusing a non-loopback target without --allow-remote")
    if args.retry_attempts < 0:
        parser.error("--retry-attempts must be non-negative")
    if args.retry_backoff_ms < 0 or args.retry_max_backoff_ms < 0:
        parser.error("retry backoff must be non-negative")
    if not 0 <= args.retry_jitter <= 1:
        parser.error("--retry-jitter must be between 0 and 1")
    retry = RetryConfig(
        attempts=args.retry_attempts,
        backoff_ms=args.retry_backoff_ms,
        max_backoff_ms=args.retry_max_backoff_ms,
        jitter=args.retry_jitter,
    )
    manifest = load_manifest(args.data)
    count = args.requests
    if args.rate is not None:
        count = max(1, int(args.rate * args.duration))
    summary, rows, accuracy_result = asyncio.run(
        run(
            args.target,
            args.data,
            manifest,
            args.mode,
            args.transport,
            count,
            args.concurrency,
            args.rate,
            args.timeout,
            args.accuracy,
            args.output.name,
            args.seed,
            retry,
        )
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.output / "requests.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    if accuracy_result is not None:
        (args.output / "accuracy.json").write_text(json.dumps(accuracy_result, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
