"""Opt-in gateway metrics test with a blocking fake worker."""

import json
import os
import socket
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
                body = json.dumps(
                    {
                        "request_id": self.headers.get("X-Request-Id", ""),
                        "output_data": [1.0],
                        "node_id": "metrics-worker",
                        "cached": False,
                        "batch_size": 1,
                        "queue_wait_us": 0,
                        "batch_execution_us": 1,
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


class GatewayMetricsTests(unittest.TestCase):
    def setUp(self):
        self.gateway_bin = os.environ.get("BENCH_GATEWAY_BIN")
        if not self.gateway_bin:
            raise unittest.SkipTest("set BENCH_GATEWAY_BIN to run gateway metrics tests")
        self.worker = BlockingWorker().start()
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory(prefix="metrics-test-")
        self.log = open(Path(self.tmp.name) / "gateway.log", "w")
        self.gateway = None
        self.addCleanup(self.worker.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop_gateway)

    def start_gateway(self, **extra):
        env = os.environ.copy()
        env["GATEWAY_PORT"] = str(self.port)
        env["ROUTING_POLICY"] = "hash"
        env.update({key: str(value) for key, value in extra.items()})
        self.gateway = subprocess.Popen(
            [self.gateway_bin, f"127.0.0.1:{self.worker.port}"],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.wait_url(f"http://127.0.0.1:{self.port}/stats")

    def stop_gateway(self):
        self.worker.release.set()
        if self.gateway is not None and self.gateway.poll() is None:
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

    def infer(self, request_id, timeout=5):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/infer",
            json.dumps({"request_id": request_id, "input_data": [1.0, 2.0, 3.0]}).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            finally:
                exc.close()

    def metrics(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/metrics", timeout=5) as response:
            self.assertEqual(response.status, 200)
            return response.read().decode("utf-8")

    def test_metrics_reports_request_and_admission_series(self):
        self.start_gateway(ADMISSION_MAX_INFLIGHT="2")
        self.worker.release.set()
        status, _ = self.infer("metrics-1")
        self.assertEqual(status, 200)
        text = self.metrics()
        self.assertIn('gateway_requests_total{endpoint="/infer",status="200"} 1', text)
        self.assertIn('gateway_request_duration_seconds_count{endpoint="/infer"} 1', text)
        self.assertIn("gateway_admission_accepted_total 1", text)
        self.assertIn("gateway_admission_in_flight 0", text)
        self.assertIn("gateway_admission_limit 2", text)
        self.assertIn('gateway_worker_in_flight{worker="127.0.0.1:', text)

    def test_metrics_is_not_rejected_when_admission_is_full(self):
        self.start_gateway(ADMISSION_MAX_INFLIGHT="1")
        first = threading.Thread(
            target=lambda: self.infer("first", timeout=10),
            daemon=True,
        )
        first.start()
        self.assertTrue(self.worker.received.wait(timeout=5))
        text = self.metrics()
        self.assertIn("gateway_admission_in_flight 1", text)

        status, body = self.infer("second", timeout=2)
        self.assertEqual(status, 503)
        self.assertIn("error", body)
        self.worker.release.set()
        first.join(timeout=10)
        self.assertFalse(first.is_alive())

        text = self.metrics()
        self.assertIn('gateway_requests_total{endpoint="/infer",status="503"} 1', text)
        self.assertIn("gateway_admission_rejected_total 1", text)
        self.assertIn("gateway_admission_in_flight 0", text)

    def test_adaptive_configuration_is_visible(self):
        self.start_gateway(
            ADMISSION_ADAPTIVE="1",
            ADMISSION_MAX_INFLIGHT="4",
            ADMISSION_MIN_INFLIGHT="1",
            ADMISSION_TARGET_P95_MS="400",
        )
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/stats", timeout=5) as response:
            admission = json.load(response)["admission"]
        self.assertTrue(admission["adaptive"])
        self.assertEqual(admission["configured_max"], 4)
        self.assertEqual(admission["min_in_flight"], 1)
        self.assertEqual(admission["target_p95_ms"], 400)
        text = self.metrics()
        self.assertIn("gateway_admission_adaptive 1", text)
        self.assertIn("gateway_admission_limit 4", text)
        self.assertIn("gateway_admission_target_p95_ms 400", text)


if __name__ == "__main__":
    unittest.main()
