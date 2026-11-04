"""Tests for the retry-aware loadgen path."""

import socket
import struct
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark-aws"))

try:
    import aiohttp  # noqa: F401
    from autotune import loadgen
except ImportError:  # aiohttp is installed in the benchmark venv
    loadgen = None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeServer:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = 0
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                with server.lock:
                    index = server.requests
                    server.requests += 1
                status = server.statuses[min(index, len(server.statuses) - 1)]
                if status == 200:
                    body = struct.pack("<f", 1.0)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("X-Cached", "false")
                    self.send_header("X-Node-Id", "fake")
                    self.send_header("X-Output-Elements", "1")
                    self.send_header("X-Batch-Size", "1")
                    self.send_header("X-Queue-Wait-Us", "0")
                    self.send_header("X-Batch-Execution-Us", "1")
                else:
                    body = b'{"error":"failure"}'
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    if status == 503:
                        self.send_header("Retry-After", "0")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        if loadgen is None:
            self.skipTest("install aiohttp to run loadgen retry tests")

    def test_parse_retry_after(self):
        self.assertEqual(loadgen.parse_retry_after("1"), 1.0)
        self.assertEqual(loadgen.parse_retry_after("0.5"), 0.5)
        self.assertEqual(loadgen.parse_retry_after("-1"), 0.0)
        self.assertIsNone(loadgen.parse_retry_after("not-a-number"))
        self.assertIsNone(loadgen.parse_retry_after(None))

    def test_exponential_backoff_and_cap(self):
        config = loadgen.RetryConfig(attempts=4, backoff_ms=100, max_backoff_ms=250, jitter=0)
        self.assertAlmostEqual(loadgen.retry_delay_seconds(1, config), 0.1)
        self.assertAlmostEqual(loadgen.retry_delay_seconds(2, config), 0.2)
        self.assertAlmostEqual(loadgen.retry_delay_seconds(3, config), 0.25)
        self.assertAlmostEqual(loadgen.retry_delay_seconds(4, config), 0.25)

    def test_retry_after_is_honored(self):
        config = loadgen.RetryConfig(attempts=1, backoff_ms=10, jitter=0)
        self.assertAlmostEqual(loadgen.retry_delay_seconds(1, config, 0.5), 0.5)

    def test_jitter_bounds(self):
        config = loadgen.RetryConfig(attempts=1, backoff_ms=100, jitter=0.2)
        self.assertAlmostEqual(loadgen.retry_delay_seconds(1, config, random_value=0.0), 0.08)
        self.assertAlmostEqual(loadgen.retry_delay_seconds(1, config, random_value=1.0), 0.12)


class RetryRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if loadgen is None:
            self.skipTest("install aiohttp to run loadgen retry tests")

    async def send(self, statuses, retry):
        server = FakeServer(statuses).start()
        spec = {
            "request_id": "retry-test",
            "entry": {"label_index": 0},
            "payload": struct.pack("<f", 1.0),
        }
        connector = aiohttp.TCPConnector(limit=1)
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                return await loadgen._send_one(session, server.url, spec, "binary", False, retry)
        finally:
            server.stop()

    async def test_retries_503_then_succeeds(self):
        row = await self.send(
            [503, 200],
            loadgen.RetryConfig(attempts=2, backoff_ms=1, max_backoff_ms=1, jitter=0),
        )
        self.assertEqual(row["status"], 200)
        self.assertTrue(row["success"])
        self.assertEqual(row["attempts"], 2)
        self.assertTrue(row["retried"])
        self.assertTrue(row["retry_success"])
        self.assertFalse(row["retry_exhausted"])

    async def test_exhausts_retries(self):
        row = await self.send(
            [503, 503, 503],
            loadgen.RetryConfig(attempts=2, backoff_ms=1, max_backoff_ms=1, jitter=0),
        )
        self.assertEqual(row["status"], 503)
        self.assertFalse(row["success"])
        self.assertEqual(row["attempts"], 3)
        self.assertTrue(row["retried"])
        self.assertFalse(row["retry_success"])
        self.assertTrue(row["retry_exhausted"])

    async def test_does_not_retry_500(self):
        row = await self.send(
            [500],
            loadgen.RetryConfig(attempts=2, backoff_ms=1, max_backoff_ms=1, jitter=0),
        )
        self.assertEqual(row["status"], 500)
        self.assertEqual(row["attempts"], 1)
        self.assertFalse(row["retried"])

    async def test_no_retry_config_keeps_503(self):
        row = await self.send([503], None)
        self.assertEqual(row["status"], 503)
        self.assertEqual(row["attempts"], 1)
        self.assertFalse(row["retried"])


if __name__ == "__main__":
    unittest.main()
