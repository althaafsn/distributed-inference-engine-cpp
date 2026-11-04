"""Gateway health endpoint test. No worker or model is required."""

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GatewayHealthTests(unittest.TestCase):
    def setUp(self):
        self.gateway_bin = os.environ.get("BENCH_GATEWAY_BIN")
        if not self.gateway_bin:
            raise unittest.SkipTest("set BENCH_GATEWAY_BIN to run gateway health tests")
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-health-test-")
        self.log = open(Path(self.tmp.name) / "gateway.log", "w")
        env = os.environ.copy()
        env["GATEWAY_PORT"] = str(self.port)
        env["GATEWAY_ID"] = "health-test-gateway"
        env["ADMISSION_MAX_INFLIGHT"] = "1"
        self.gateway = subprocess.Popen(
            [self.gateway_bin, "127.0.0.1:1"],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.addCleanup(self.stop)
        try:
            self.wait_url(f"http://127.0.0.1:{self.port}/health")
        except Exception:
            self.stop()
            raise

    def stop(self):
        gateway = getattr(self, "gateway", None)
        if gateway is not None:
            if gateway.poll() is None:
                gateway.terminate()
                try:
                    gateway.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    gateway.kill()
                    gateway.wait()
            self.gateway = None
        log = getattr(self, "log", None)
        if log is not None:
            log.close()
            self.log = None
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            tmp.cleanup()
            self.tmp = None

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

    def test_health_is_available_and_identifies_the_gateway(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = json.load(response)
        self.assertEqual(body, {"status": "ok", "gateway_id": "health-test-gateway"})

        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/stats", timeout=5) as response:
            stats = json.load(response)
        self.assertEqual(stats["gateway_id"], "health-test-gateway")
        self.assertEqual(stats["admission"]["max_in_flight"], 1)


if __name__ == "__main__":
    unittest.main()
