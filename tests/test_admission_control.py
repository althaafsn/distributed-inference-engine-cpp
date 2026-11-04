"""Opt-in gateway admission-control tests with a blocking fake worker."""

import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BlockingWorker:
    def __init__(self):
        self.count = 0
        self.received = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                with worker.lock:
                    worker.count += 1
                worker.received.set()
                worker.release.wait(timeout=10)
                if self.path == "/infer_tensor":
                    body = struct.pack("<f", 1.0)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("X-Request-Id", self.headers.get("X-Request-Id", ""))
                    self.send_header("X-Node-Id", "blocking-worker")
                    self.send_header("X-Cached", "false")
                    self.send_header("X-Output-Elements", "1")
                    self.send_header("X-Batch-Size", "1")
                    self.send_header("X-Queue-Wait-Us", "0")
                    self.send_header("X-Batch-Execution-Us", "1")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps(
                    {
                        "request_id": self.headers.get("X-Request-Id", ""),
                        "output_data": [1.0],
                        "node_id": "blocking-worker",
                        "cached": False,
                        "inference_time_us": 1,
                        "queue_wait_us": 0,
                        "batch_execution_us": 1,
                        "batch_size": 1,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class AdmissionControlTests(unittest.TestCase):
    def setUp(self):
        self.gateway_bin = os.environ.get("BENCH_GATEWAY_BIN")
        if not self.gateway_bin:
            raise unittest.SkipTest("set BENCH_GATEWAY_BIN to run admission tests")
        self.worker = BlockingWorker().start()
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory(prefix="admission-test-")
        self.log = open(Path(self.tmp.name) / "gateway.log", "w")
        self.gateway = None
        self.addCleanup(self.worker.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop_gateway)

    def start_gateway(self, limit):
        env = os.environ.copy()
        env["GATEWAY_PORT"] = str(self.port)
        env["ROUTING_POLICY"] = "hash"
        env["ADMISSION_MAX_INFLIGHT"] = limit
        self.gateway = subprocess.Popen(
            [self.gateway_bin, f"127.0.0.1:{self.worker.port}"],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            self.wait_url(f"http://127.0.0.1:{self.port}/stats")
        except Exception:
            self.stop_gateway()
            raise

    def stop_gateway(self):
        self.worker.release.set()
        if self.gateway is not None:
            if self.gateway.poll() is None:
                self.gateway.terminate()
                try:
                    self.gateway.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.gateway.kill()
                    self.gateway.wait()
            self.gateway = None

    @staticmethod
    def wait_url(url, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"timeout waiting for {url}")

    def stats(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/stats", timeout=5) as response:
            return json.load(response)

    def request(self, path, body, headers, timeout=5):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", body, headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, dict(exc.headers), exc.read()
            finally:
                exc.close()

    def post_infer(self, request_id, timeout=5):
        status, headers, body = self.request(
            "/infer",
            json.dumps({"request_id": request_id, "input_data": [1.0, 2.0, 3.0]}).encode(),
            {"Content-Type": "application/json"},
            timeout,
        )
        return status, headers, json.loads(body)

    def post_tensor(self, request_id, timeout=5):
        return self.request(
            "/infer_tensor",
            struct.pack("<3f", 1.0, 2.0, 3.0),
            {"Content-Type": "application/octet-stream", "X-Request-Id": request_id},
            timeout,
        )

    def assert_second_concurrent_request_rejected(self, send):
        result = {}
        first = threading.Thread(
            target=lambda: result.update(first=send("first", timeout=10)),
            daemon=True,
        )
        first.start()
        try:
            self.assertTrue(self.worker.received.wait(timeout=5))
            live = self.stats()
            self.assertEqual(live["admission"]["in_flight"], 1)
            self.assertEqual(live["admission"]["accepted"], 1)
            self.assertEqual(live["admission"]["rejected"], 0)

            status, headers, body = send("second", timeout=2)
            self.assertEqual(status, 503)
            self.assertEqual(headers.get("Retry-After"), "1")
            if isinstance(body, bytes):
                self.assertIn(b"error", body)
            else:
                self.assertIn("error", body)
            self.assertEqual(self.worker.count, 1)
        finally:
            self.worker.release.set()
            first.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertEqual(result["first"][0], 200)

        final = self.stats()
        self.assertEqual(final["admission"]["max_in_flight"], 1)
        self.assertEqual(final["admission"]["in_flight"], 0)
        self.assertEqual(final["admission"]["accepted"], 1)
        self.assertEqual(final["admission"]["rejected"], 1)

    def test_second_concurrent_infer_is_rejected_before_the_worker(self):
        self.start_gateway("1")
        self.assert_second_concurrent_request_rejected(self.post_infer)

    def test_second_concurrent_tensor_is_rejected_before_the_worker(self):
        self.start_gateway("1")
        self.assert_second_concurrent_request_rejected(self.post_tensor)

    def assert_ignored_limit(self, limit, warning):
        self.start_gateway(limit)
        self.assertEqual(self.stats()["admission"]["max_in_flight"], 0)
        self.assertIn(warning, (Path(self.tmp.name) / "gateway.log").read_text())
        self.worker.release.set()
        status, _, _ = self.post_infer(f"config-{limit}")
        self.assertEqual(status, 200)
        self.assertEqual(self.worker.count, 1)

    def test_negative_limit_is_ignored(self):
        self.assert_ignored_limit("-1", "Ignoring invalid ADMISSION_MAX_INFLIGHT")

    def test_out_of_range_limit_is_ignored(self):
        self.assert_ignored_limit("9223372036854775808", "Ignoring invalid ADMISSION_MAX_INFLIGHT")

    def test_malformed_request_releases_the_admission_slot(self):
        self.start_gateway("1")
        self.worker.release.set()
        status, _, _ = self.request("/infer", b"{", {"Content-Type": "application/json"}, timeout=5)
        self.assertEqual(status, 400)
        self.assertEqual(self.stats()["admission"]["in_flight"], 0)
        status, _, _ = self.post_infer("after-malformed")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
