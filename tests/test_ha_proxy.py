"""Tests for the local gateway HA proxy."""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark-aws"))

try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from ha_proxy import HAProxy
except ImportError:  # aiohttp is installed in the benchmark and CI checks jobs
    web = None
    TestClient = TestServer = HAProxy = None


class FakeGateway:
    def __init__(self, gateway_id):
        self.gateway_id = gateway_id
        self.healthy = True
        self.respond_status = 200
        self.hits = 0
        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_post("/infer", self.infer)

    async def health(self, request):
        return web.json_response(
            {"status": "ok" if self.healthy else "down", "gateway_id": self.gateway_id},
            status=200 if self.healthy else 503,
        )

    async def infer(self, request):
        self.hits += 1
        body = await request.json()
        if self.respond_status != 200:
            return web.json_response(
                {"error": "failure"},
                status=self.respond_status,
                headers={"Retry-After": "1"},
            )
        return web.json_response({"request_id": body.get("request_id"), "output_data": [1.0]})


class HAProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if web is None:
            self.skipTest("install aiohttp to run HA proxy tests")
        self.gateway1 = FakeGateway("gateway-1")
        self.gateway2 = FakeGateway("gateway-2")
        self.server1 = TestServer(self.gateway1.app)
        self.server2 = TestServer(self.gateway2.app)
        await self.server1.start_server()
        await self.server2.start_server()
        self.proxy = HAProxy(
            [
                str(self.server1.make_url("")).rstrip("/"),
                str(self.server2.make_url("")).rstrip("/"),
            ],
            health_interval=0.05,
        )
        self.proxy_server = TestServer(self.proxy.create_app())
        self.client = TestClient(self.proxy_server)
        await self.client.start_server()

    async def asyncTearDown(self):
        client = getattr(self, "client", None)
        if client is not None:
            await client.close()
            self.client = None
        for name in ("server1", "server2"):
            server = getattr(self, name, None)
            if server is not None and not server.closed:
                await server.close()

    async def post(self, request_id):
        async with self.client.post(
            "/infer", json={"request_id": request_id, "input_data": [1.0, 2.0]}
        ) as response:
            return response.status, await response.json()

    async def stats(self):
        async with self.client.get("/ha/stats") as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def test_round_robin_spreads_requests(self):
        for index in range(4):
            status, body = await self.post(f"r{index}")
            self.assertEqual(status, 200)
            self.assertEqual(body["output_data"], [1.0])
        self.assertEqual(self.gateway1.hits, 2)
        self.assertEqual(self.gateway2.hits, 2)
        stats = await self.stats()
        self.assertEqual(
            {item["gateway_id"] for item in stats["upstreams"]},
            {"gateway-1", "gateway-2"},
        )

    async def test_health_failure_removes_a_gateway_and_recovery_restores_it(self):
        self.gateway1.healthy = False
        await asyncio.sleep(0.12)
        status, _ = await self.post("failover")
        self.assertEqual(status, 200)
        self.assertEqual(self.gateway1.hits, 0)
        self.assertEqual(self.gateway2.hits, 1)
        stats = await self.stats()
        self.assertFalse(stats["upstreams"][0]["healthy"])

        self.gateway1.healthy = True
        await asyncio.sleep(0.12)
        for index in range(4):
            status, _ = await self.post(f"recover-{index}")
            self.assertEqual(status, 200)
        self.assertGreater(self.gateway1.hits, 0)
        self.assertGreater(self.gateway2.hits, 1)

    async def test_connection_failure_retries_the_other_gateway(self):
        if self.proxy._health_task is not None:
            self.proxy._health_task.cancel()
            try:
                await self.proxy._health_task
            except asyncio.CancelledError:
                pass
            self.proxy._health_task = None
        await self.server1.close()
        status, body = await self.post("connection-failover")
        self.assertEqual(status, 200)
        self.assertEqual(body["output_data"], [1.0])
        self.assertEqual(self.gateway1.hits, 0)
        self.assertGreaterEqual(self.gateway2.hits, 1)
        stats = await self.stats()
        self.assertGreaterEqual(stats["failovers"], 1)

    async def test_503_is_forwarded_without_retry(self):
        self.gateway1.respond_status = 503
        self.gateway2.respond_status = 503
        async with self.client.post(
            "/infer", json={"request_id": "no-retry", "input_data": [1.0]}
        ) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual(response.headers.get("Retry-After"), "1")
        self.assertEqual(self.gateway1.hits + self.gateway2.hits, 1)

    async def test_both_gateways_down_returns_503(self):
        self.gateway1.healthy = False
        self.gateway2.healthy = False
        await asyncio.sleep(0.12)
        async with self.client.post(
            "/infer", json={"request_id": "down", "input_data": [1.0]}
        ) as response:
            self.assertEqual(response.status, 503)
            self.assertEqual(response.headers.get("Retry-After"), "1")
            body = await response.json()
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
