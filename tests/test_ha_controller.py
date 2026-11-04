"""Tests for the AWS ALB HA controller safety helpers."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark-aws"))

import ha_controller  # noqa: E402


def aws_error(stderr: str = "boom") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["aws"], stderr=stderr)


class CleanupTests(unittest.TestCase):
    def state(self) -> dict:
        return {"account": "123456789012", "alb": {"target_group_arn": "tg"}}

    def test_base_cleanup_runs_when_alb_delete_returns_false(self):
        with (
            mock.patch.object(ha_controller, "delete_alb", return_value=False),
            mock.patch.object(ha_controller.mh, "cleanup_state", return_value=True) as base,
        ):
            self.assertFalse(ha_controller.cleanup_ha_state(self.state()))
        base.assert_called_once()

    def test_base_cleanup_runs_when_alb_delete_raises(self):
        with (
            mock.patch.object(ha_controller, "delete_alb", side_effect=TimeoutError("slow")),
            mock.patch.object(ha_controller.mh, "cleanup_state", return_value=True) as base,
        ):
            self.assertFalse(ha_controller.cleanup_ha_state(self.state()))
        base.assert_called_once()

    def test_base_cleanup_failure_is_reported(self):
        with (
            mock.patch.object(ha_controller, "delete_alb", return_value=True),
            mock.patch.object(ha_controller.mh, "cleanup_state", return_value=False),
        ):
            self.assertFalse(ha_controller.cleanup_ha_state(self.state()))

    def test_no_account_skips_aws(self):
        with mock.patch.object(ha_controller, "delete_alb") as alb:
            self.assertTrue(ha_controller.cleanup_ha_state({}))
        alb.assert_not_called()


class DeleteAlbTests(unittest.TestCase):
    def test_deletes_load_balancer_when_target_group_delete_fails(self):
        calls = []

        def fake_aws(service, command, *args):
            calls.append(command)
            if command == "delete-target-group":
                raise aws_error("DependencyViolation")
            if command == "describe-load-balancers":
                raise aws_error("LoadBalancerNotFound")
            return {}

        state = {"alb": {"listener_arn": "l", "target_group_arn": "tg", "arn": "lb"}}
        with (
            mock.patch.object(ha_controller, "aws", side_effect=fake_aws),
            mock.patch.object(ha_controller.time, "sleep", return_value=None),
        ):
            self.assertFalse(ha_controller.delete_alb(state))
        self.assertIn("delete-load-balancer", calls)

    def test_not_found_counts_as_deleted(self):
        def fake_aws(service, command, *args):
            if command == "describe-load-balancers":
                raise aws_error("LoadBalancerNotFound")
            raise aws_error("NotFound")

        state = {"alb": {"listener_arn": "l", "target_group_arn": "tg", "arn": "lb"}}
        with mock.patch.object(ha_controller, "aws", side_effect=fake_aws):
            self.assertTrue(ha_controller.delete_alb(state))


class CreateAlbTests(unittest.TestCase):
    def state(self) -> dict:
        return {
            "run_id": "infer-test",
            "vpc": "vpc-1",
            "subnets": ["subnet-a", "subnet-b"],
            "groups": {"alb": "sg-alb"},
            "instances": {"gateway-1": {"id": "i-1"}, "gateway-2": {"id": "i-2"}},
        }

    def test_saves_state_after_listener_before_registration(self):
        saved = []

        def fake_save(path, state):
            saved.append(json.loads(json.dumps(state)))

        def fake_aws(service, command, *args):
            if command == "register-targets":
                raise aws_error("registration failed")
            if command == "create-target-group":
                return {"TargetGroups": [{"TargetGroupArn": "tg"}]}
            if command == "create-load-balancer":
                return {"LoadBalancers": [{"LoadBalancerArn": "lb", "DNSName": "dns"}]}
            if command == "create-listener":
                return {"Listeners": [{"ListenerArn": "listener"}]}
            raise AssertionError(command)

        state = self.state()
        with (
            mock.patch.object(ha_controller, "aws", side_effect=fake_aws),
            mock.patch.object(ha_controller, "save_state", side_effect=fake_save),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                ha_controller.create_alb(state, Path("state.json"))
        self.assertTrue(
            any(item.get("alb", {}).get("listener_arn") == "listener" for item in saved)
        )

    def test_tags_target_group_and_load_balancer(self):
        commands = []

        def fake_aws(service, command, *args):
            commands.append((command, args))
            if command == "create-target-group":
                return {"TargetGroups": [{"TargetGroupArn": "tg"}]}
            if command == "create-load-balancer":
                return {"LoadBalancers": [{"LoadBalancerArn": "lb", "DNSName": "dns"}]}
            if command == "create-listener":
                return {"Listeners": [{"ListenerArn": "listener"}]}
            return {}

        with (
            mock.patch.object(ha_controller, "aws", side_effect=fake_aws),
            mock.patch.object(ha_controller, "save_state"),
            mock.patch.object(ha_controller, "wait_alb_active"),
            mock.patch.object(ha_controller, "wait_targets_healthy"),
        ):
            ha_controller.create_alb(self.state(), Path("state.json"))

        tagged = {
            command: args[args.index("--tags") + 1]
            for command, args in commands
            if "--tags" in args
        }
        expected = f"Key={ha_controller.controller.TAG_KEY},Value=infer-test"
        self.assertEqual(
            tagged,
            {"create-target-group": expected, "create-load-balancer": expected},
        )


class RunLimitTests(unittest.TestCase):
    def test_raises_after_limit(self):
        now = 1000 + ha_controller.RUN_LIMIT_SECONDS + 1
        with mock.patch.object(ha_controller.time, "monotonic", return_value=now):
            with self.assertRaises(RuntimeError):
                ha_controller.check_run_limit(1000)

    def test_allows_within_limit(self):
        with mock.patch.object(ha_controller.time, "monotonic", return_value=1000):
            ha_controller.check_run_limit(1000)


class PreflightOrderTests(unittest.TestCase):
    def test_run_aborts_before_resource_creation(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(ha_controller, "HERE", Path(tmp)),
            mock.patch.object(ha_controller, "run_id", return_value="infer-test"),
            mock.patch.object(
                ha_controller.ha_preflight, "run_preflight", return_value=1
            ) as preflight,
            mock.patch.object(ha_controller.controller, "preflight") as context,
            mock.patch.object(ha_controller, "create_groups") as groups,
        ):
            code = ha_controller.run(mock.Mock(model="m", admission_max=4))
        self.assertEqual(code, 1)
        preflight.assert_called_once()
        context.assert_not_called()
        groups.assert_not_called()


if __name__ == "__main__":
    unittest.main()
