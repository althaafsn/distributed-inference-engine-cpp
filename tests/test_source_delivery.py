"""Verify the small source payload and its application without cloud resources."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "benchmark-aws" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


controller = module("source_controller", "controller.py")
verifier = module("source_verifier", "verify_source.py")
load = module("source_load", "load.py")


class SourceDeliveryTests(unittest.TestCase):
    def test_both_modes_produce_verified_allowlisted_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for mode in ("baseline", "instrumented"):
                payload = root / mode
                payload.mkdir()
                manifest = controller.prepare_source(payload, mode)
                verifier.verify_payload(
                    payload, verifier.read_manifest(payload / "source-manifest.json")
                )
                subprocess.run(
                    [
                        sys.executable,
                        str(payload / "verify_source.py"),
                        "payload",
                        str(payload),
                        str(payload / "source-manifest.json"),
                    ],
                    check=True,
                    capture_output=True,
                )
                expected_sources = (
                    controller.BASELINE_SOURCE_FILES
                    if mode == "baseline"
                    else controller.SOURCE_FILES
                )
                self.assertEqual(set(manifest["source_sha256"]), set(expected_sources))
                self.assertEqual((payload / "source.patch").stat().st_size == 0, mode == "baseline")

                checkout = root / (mode + "-checkout")
                checkout.mkdir()
                subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
                for path in controller.BASELINE_SOURCE_FILES:
                    target = checkout / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    original = subprocess.run(
                        ["git", "-C", str(ROOT), "show", f"{controller.BASE_REVISION}:{path}"],
                        check=True,
                        capture_output=True,
                    ).stdout
                    target.write_bytes(original)
                if mode == "instrumented":
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(checkout),
                            "apply",
                            "--check",
                            str(payload / "source.patch"),
                        ],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(checkout), "apply", str(payload / "source.patch")],
                        check=True,
                    )
                verifier.verify_source(checkout, manifest)
                (checkout / controller.SOURCE_FILES[0]).write_text("unexpected source")
                with self.assertRaisesRegex(ValueError, "source checksum mismatch"):
                    verifier.verify_source(checkout, manifest)

    def test_tampered_patch_and_scripts_fail_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = controller.prepare_source(directory, "instrumented")
            (directory / "source.patch").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "patch checksum"):
                verifier.verify_payload(directory, manifest)
            manifest = controller.prepare_source(directory, "baseline")
            (directory / "load.py").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "script checksum"):
                verifier.verify_payload(directory, manifest)

    def test_source_manifest_cannot_add_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = controller.prepare_source(directory, "baseline")
            manifest["source_sha256"]["private.txt"] = "bad"
            (directory / "source-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "exactly the allowed"):
                verifier.read_manifest(directory / "source-manifest.json")

    def test_bad_snapshot_prevents_provisioning(self):
        context = {"region": controller.REGION, "account": "123"}
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(controller, "HERE", Path(temp)),
            mock.patch.object(controller, "preflight", return_value=context),
            mock.patch.object(controller, "prepare_source", side_effect=RuntimeError("bad source")),
            mock.patch.object(controller, "provision_instance") as provision,
            mock.patch.object(controller, "cleanup") as cleanup,
        ):
            self.assertEqual(controller.run(), 1)
            provision.assert_not_called()
            cleanup.assert_not_called()

    def test_unexpected_head_is_rejected_before_packaging(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(controller, "BASE_REVISION", "0" * 40),
        ):
            with self.assertRaisesRegex(RuntimeError, "not an ancestor"):
                controller.prepare_source(Path(temp), "instrumented")

    def test_delivery_includes_each_changed_source_file(self):
        for name in (
            "src/inference_engine.cpp",
            "src/engine_backend.cpp",
            "src/gateway.cpp",
            "include/engine_backend.h",
            "include/inference_engine.h",
            "include/request_validation.h",
            "include/shutdown_signal.h",
            "include/tensor_payload.h",
            "include/adaptive_admission.h",
            "include/gateway_metrics.h",
        ):
            self.assertIn(name, controller.SOURCE_FILES)
            self.assertIn(name, verifier.SOURCE_FILES)

    def test_instrumented_patch_contains_selected_source_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            controller.prepare_source(directory, "instrumented")
            patch = (directory / "source.patch").read_text(encoding="utf-8")
            self.assertIn("SetIntraOpNumThreads", patch)
            self.assertIn("hardware_concurrency", patch)
            self.assertGreaterEqual(patch.count("set_tcp_nodelay(true)"), 3)
            self.assertIn("GATEWAY_PORT", patch)
            self.assertIn("decode_float32_le", patch)
            self.assertIn("infer_tensor", patch)
            self.assertIn("block_shutdown_signals", patch)
            self.assertIn("validateInferRequest", patch)
            self.assertIn("ADMISSION_ADAPTIVE", patch)
            self.assertIn("gateway_requests_total", patch)

    def test_other_source_changes_are_not_silently_omitted(self):
        def fake_git(args, **_kwargs):
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, stdout=controller.BASE_REVISION + "\n")
            if "--name-only" in args:
                return subprocess.CompletedProcess(args, 0, stdout="src/not_allowed.cpp\0")
            return subprocess.CompletedProcess(args, 0, stdout="")

        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(controller, "command", side_effect=fake_git),
        ):
            with self.assertRaisesRegex(RuntimeError, "outside the delivery allowlist"):
                controller.prepare_source(Path(temp), "instrumented")

    def test_returned_source_must_match_local_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            controller.prepare_source(directory, "instrumented")
            result = directory / "results"
            result.mkdir()
            for name in ("source.patch", "source-manifest.json"):
                (result / name).write_bytes((directory / name).read_bytes())
            controller.verify_returned_source(directory)
            (result / "source.patch").write_text("wrong patch")
            with self.assertRaisesRegex(RuntimeError, "differs"):
                controller.verify_returned_source(directory)

    def test_required_timings_reject_uninstrumented_results(self):
        summary = {
            "workload": "unique",
            "concurrency": 1,
            "successes": 1,
            "failures": 0,
            "missing_worker_timings": 1,
            "worker_timings_us": {"queue_wait_us": {"samples": 0}},
        }
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(load, "run_level", return_value=(summary, [])),
        ):
            self.assertEqual(load.main([temp, "--require-timings"]), 1)
            self.assertEqual(load.main([temp]), 0)

    def test_required_timings_accept_complete_instrumented_results(self):
        summary = {
            "workload": "unique",
            "concurrency": 1,
            "successes": 1,
            "failures": 0,
            "missing_worker_timings": 0,
            "worker_timings_us": {"queue_wait_us": {"samples": 1}},
        }
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(load, "run_level", return_value=(summary, [])),
        ):
            self.assertEqual(load.main([temp, "--require-timings"]), 0)

    def test_uninstrumented_or_invalid_batch_data_is_missing(self):
        row = {
            "success": True,
            "cached": False,
            "queue_wait_us": None,
            "batch_execution_us": None,
            "batch_size": None,
        }
        self.assertTrue(load.missing_timings(row))
        row.update(queue_wait_us=0, batch_execution_us=10, batch_size=2)
        self.assertFalse(load.missing_timings(row))
        row["cached"] = True
        row["queue_wait_us"] = None
        self.assertFalse(load.missing_timings(row))


if __name__ == "__main__":
    unittest.main()
