import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark-aws"))

import controller  # noqa: E402
import multihost_controller  # noqa: E402


class SecurityGroupTests(unittest.TestCase):
    def test_worker_ingress_uses_a_source_group_only(self):
        with mock.patch.object(multihost_controller, "aws") as aws:
            multihost_controller.authorize_group("sg-target", 8001, "sg-gateway")
        permissions = json.loads(aws.call_args.args[-1])
        self.assertEqual(permissions[0]["UserIdGroupPairs"], [{"GroupId": "sg-gateway"}])
        self.assertNotIn("CidrIp", permissions[0])
        self.assertNotIn("CidrIpv6", permissions[0])

    def test_ssh_ingress_is_a_single_host_cidr(self):
        with mock.patch.object(multihost_controller, "aws") as aws:
            multihost_controller.authorize_ip("sg-ssh", 22, "203.0.113.7/32")
        args = aws.call_args.args
        self.assertEqual(args[args.index("--port") + 1], "22")
        self.assertEqual(args[args.index("--cidr") + 1], "203.0.113.7/32")


class CleanupTests(unittest.TestCase):
    def test_cleanup_refuses_a_different_account(self):
        state = {"region": "us-east-1", "account": "111111111111"}
        with mock.patch.object(multihost_controller, "identity", return_value="222222222222"):
            with self.assertRaisesRegex(RuntimeError, "refusing cleanup"):
                multihost_controller.cleanup_state(state)

    def test_cleanup_refuses_a_different_region(self):
        state = {"region": "us-west-2", "account": "111111111111"}
        with mock.patch.object(multihost_controller, "identity", return_value="111111111111"):
            with self.assertRaisesRegex(RuntimeError, "refusing cleanup"):
                multihost_controller.cleanup_state(state)


class FreshCacheTests(unittest.TestCase):
    def test_zero_counters_are_fresh(self):
        multihost_controller.require_fresh_health(
            {"total_requests": 0, "cache_hits": 0}, "worker-1"
        )

    def test_any_request_or_hit_is_rejected(self):
        for health in (
            {"total_requests": 1, "cache_hits": 0},
            {"total_requests": 0, "cache_hits": 1},
        ):
            with self.assertRaisesRegex(RuntimeError, "non-empty cache"):
                multihost_controller.require_fresh_health(health, "worker-1")


class HoldMatrixTests(unittest.TestCase):
    def test_matrix_alternates_baseline_and_admission(self):
        self.assertEqual(
            multihost_controller.hold_sequence([50], 7, 3, True),
            [
                (50, 0, 0),
                (50, 7, 0),
                (50, 7, 1),
                (50, 0, 1),
                (50, 0, 2),
                (50, 7, 2),
            ],
        )

    def test_matrix_runs_each_rate(self):
        self.assertEqual(
            multihost_controller.hold_sequence([25, 100], 7, 1, True),
            [(25, 0, 0), (25, 7, 0), (100, 0, 0), (100, 7, 0)],
        )

    def test_single_hold_uses_one_condition(self):
        self.assertEqual(
            multihost_controller.hold_sequence([50], 7, 3, False),
            [(50, 7, 0)],
        )

    def test_gateway_env_passes_admission_limit(self):
        environment = multihost_controller.gateway_env("least_loaded", 7)
        self.assertIn("ADMISSION_MAX_INFLIGHT=7", environment)
        self.assertIn("ROUTING_POLICY=least_loaded", environment)


class RunIdTests(unittest.TestCase):
    def test_invalid_run_id_is_rejected(self):
        with self.assertRaises(ValueError):
            controller.filters("infer-not-hex")


class StopScriptTests(unittest.TestCase):
    def run_script(self, home: Path, script: str, *args: str) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        return subprocess.run(
            ["bash", script, *args],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )

    def wait_gone(self, process: subprocess.Popen) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return True
            time.sleep(0.05)
        return False

    def test_stop_removes_worker_and_gateway(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="stop-test-") as temp:
            home = Path(temp)
            build = home / "benchmark-work" / "build"
            results = home / "benchmark-work" / "results"
            build.mkdir(parents=True)
            results.mkdir(parents=True)
            for name in ("worker_node", "gateway"):
                executable = build / name
                executable.write_text("#!/usr/bin/env bash\nsleep 300\n")
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            worker = subprocess.Popen(
                [str(build / "worker_node"), "8001", "worker_1", "model"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            gateway = subprocess.Popen(
                [str(build / "gateway")],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (results / "worker-1.pid").write_text(str(worker.pid))
            (results / "gateway.pid").write_text(str(gateway.pid))
            self.run_script(home, str(root / "benchmark-aws" / "multihost" / "remote_stop.sh"))
            self.assertTrue(self.wait_gone(worker))
            self.assertTrue(self.wait_gone(gateway))
            self.assertEqual(list(results.glob("*.pid")), [])

    def test_kill_worker_leaves_other_workers_alive(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="kill-test-") as temp:
            home = Path(temp)
            build = home / "benchmark-work" / "build"
            results = home / "benchmark-work" / "results"
            build.mkdir(parents=True)
            results.mkdir(parents=True)
            executable = build / "worker_node"
            executable.write_text("#!/usr/bin/env bash\nsleep 300\n")
            executable.chmod(0o755)
            first = subprocess.Popen(
                [str(executable), "8001", "worker_1", "model"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            second = subprocess.Popen(
                [str(executable), "8001", "worker_2", "model"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                (results / "worker-1.pid").write_text(str(first.pid))
                (results / "worker-2.pid").write_text(str(second.pid))
                self.run_script(
                    home,
                    str(root / "benchmark-aws" / "multihost" / "remote_kill_worker.sh"),
                    "worker_1",
                )
                self.assertTrue(self.wait_gone(first))
                self.assertIsNone(second.poll())
            finally:
                if second.poll() is None:
                    second.terminate()
                    second.wait(timeout=5)
                self.run_script(home, str(root / "benchmark-aws" / "multihost" / "remote_stop.sh"))


if __name__ == "__main__":
    unittest.main()
