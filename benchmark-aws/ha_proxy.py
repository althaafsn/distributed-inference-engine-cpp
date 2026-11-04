#!/usr/bin/env python3
"""Round-robin HTTP proxy with health checks for local gateway HA tests."""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

import aiohttp
from aiohttp import web

RESPONSE_HEADERS = {
    "Content-Type",
    "X-Request-Id",
    "X-Node-Id",
    "X-Cached",
    "X-Output-Elements",
    "X-Batch-Size",
    "X-Queue-Wait-Us",
    "X-Batch-Execution-Us",
    "Retry-After",
}


@dataclass
class Upstream:
    url: str
    healthy: bool = False
    requests: int = 0
    failures: int = 0
    gateway_id: str | None = None
    last_error: str | None = None


class HAProxy:
    def __init__(
        self,
        upstreams: list[str],
        health_interval: float = 1.0,
        request_timeout: float = 30.0,
        retry_other: bool = True,
    ):
        self.upstreams = [Upstream(url.rstrip("/")) for url in upstreams]
        self.health_interval = health_interval
        self.request_timeout = request_timeout
        self.retry_other = retry_other
        self._index = 0
        self._session: aiohttp.ClientSession | None = None
        self._health_task: asyncio.Task | None = None
        self.total_requests = 0
        self.failovers = 0
        self.started_at = time.monotonic()

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=256, force_close=False),
            timeout=aiohttp.ClientTimeout(total=self.request_timeout),
        )
        await self.check_health_once()
        self._health_task = asyncio.create_task(self.health_loop())

    async def stop(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def check_health_once(self) -> None:
        assert self._session is not None
        for upstream in self.upstreams:
            try:
                async with self._session.get(
                    upstream.url + "/health",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as response:
                    body = await response.json()
                    if response.status == 200:
                        upstream.healthy = True
                        upstream.gateway_id = body.get("gateway_id")
                        upstream.last_error = None
                    else:
                        upstream.healthy = False
                        upstream.last_error = f"health status {response.status}"
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                upstream.healthy = False
                upstream.gateway_id = None
                upstream.last_error = type(exc).__name__

    async def health_loop(self) -> None:
        while True:
            await asyncio.sleep(self.health_interval)
            await self.check_health_once()

    def select(self, exclude: set[str] | None = None) -> Upstream | None:
        for _ in range(len(self.upstreams)):
            upstream = self.upstreams[self._index % len(self.upstreams)]
            self._index += 1
            if upstream.healthy and (exclude is None or upstream.url not in exclude):
                return upstream
        return None

    def stats(self) -> dict:
        return {
            "uptime_seconds": round(time.monotonic() - self.started_at, 3),
            "total_requests": self.total_requests,
            "failovers": self.failovers,
            "health_interval_seconds": self.health_interval,
            "upstreams": [
                {
                    "url": upstream.url,
                    "gateway_id": upstream.gateway_id,
                    "healthy": upstream.healthy,
                    "requests": upstream.requests,
                    "failures": upstream.failures,
                    "last_error": upstream.last_error,
                }
                for upstream in self.upstreams
            ],
        }

    async def handle_stats(self, request: web.Request) -> web.Response:
        return web.json_response(self.stats())

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        headers = {}
        for name in ("Content-Type", "X-Request-Id"):
            value = request.headers.get(name)
            if value:
                headers[name] = value
        excluded: set[str] = set()
        attempts = 2 if self.retry_other else 1
        for attempt in range(attempts):
            upstream = self.select(excluded)
            if upstream is None:
                break
            upstream.requests += 1
            self.total_requests += 1
            try:
                async with self._session.request(
                    request.method,
                    upstream.url + request.path_qs,
                    data=body,
                    headers=headers,
                ) as response:
                    response_body = await response.read()
                    upstream.last_error = None
                    if attempt > 0:
                        self.failovers += 1
                    return web.Response(
                        status=response.status,
                        body=response_body,
                        headers={
                            name: response.headers[name]
                            for name in RESPONSE_HEADERS
                            if name in response.headers
                        },
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                upstream.failures += 1
                upstream.healthy = False
                upstream.last_error = type(exc).__name__
                excluded.add(upstream.url)
        return web.json_response(
            {"error": "no healthy gateway", "retry_after_ms": 1000},
            status=503,
            headers={"Retry-After": "1"},
        )

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ha/stats", self.handle_stats)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        app.on_startup.append(lambda _app: self.start())
        app.on_cleanup.append(lambda _app: self.stop())
        return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--upstream", action="append", required=True)
    parser.add_argument("--health-interval", type=float, default=1.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--no-retry-other", action="store_true")
    args = parser.parse_args(argv)
    proxy = HAProxy(
        args.upstream,
        health_interval=args.health_interval,
        request_timeout=args.request_timeout,
        retry_other=not args.no_retry_other,
    )
    web.run_app(proxy.create_app(), host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
