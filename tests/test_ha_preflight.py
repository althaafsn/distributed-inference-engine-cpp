"""Tests for the AWS ALB HA preflight helpers."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark-aws"))

import ha_preflight  # noqa: E402


class SubnetSelectionTests(unittest.TestCase):
    def test_prefers_public_subnets_in_distinct_azs(self):
        subnets = [
            {
                "SubnetId": "subnet-a",
                "AvailabilityZone": "us-east-1a",
                "MapPublicIpOnLaunch": True,
                "AvailableIpAddressCount": 100,
            },
            {
                "SubnetId": "subnet-b",
                "AvailabilityZone": "us-east-1b",
                "MapPublicIpOnLaunch": True,
                "AvailableIpAddressCount": 100,
            },
            {
                "SubnetId": "subnet-c",
                "AvailabilityZone": "us-east-1c",
                "MapPublicIpOnLaunch": False,
                "AvailableIpAddressCount": 100,
            },
        ]
        chosen = ha_preflight.choose_subnets(subnets)
        self.assertEqual([subnet["SubnetId"] for subnet in chosen], ["subnet-a", "subnet-b"])

    def test_requires_two_availability_zones(self):
        subnets = [
            {
                "SubnetId": "subnet-a",
                "AvailabilityZone": "us-east-1a",
                "MapPublicIpOnLaunch": True,
                "AvailableIpAddressCount": 100,
            },
            {
                "SubnetId": "subnet-b",
                "AvailabilityZone": "us-east-1a",
                "MapPublicIpOnLaunch": True,
                "AvailableIpAddressCount": 100,
            },
        ]
        with self.assertRaisesRegex(ValueError, "different availability zones"):
            ha_preflight.choose_subnets(subnets)

    def test_rejects_subnets_with_too_few_addresses(self):
        subnets = [
            {
                "SubnetId": "subnet-a",
                "AvailabilityZone": "us-east-1a",
                "MapPublicIpOnLaunch": True,
                "AvailableIpAddressCount": 5,
            },
            {
                "SubnetId": "subnet-b",
                "AvailabilityZone": "us-east-1b",
                "MapPublicIpOnLaunch": True,
                "AvailableIpAddressCount": 5,
            },
        ]
        with self.assertRaisesRegex(ValueError, "different availability zones"):
            ha_preflight.choose_subnets(subnets)


class PreflightFailureTests(unittest.TestCase):
    def clean(self) -> dict:
        return {
            "active_tagged_instances": [],
            "active_tagged_volumes": [],
            "load_balancers": [],
            "elb_permissions": {"status": "ok", "target_groups": []},
        }

    def test_clean_result_has_no_failures(self):
        self.assertEqual(ha_preflight.preflight_failures(self.clean()), [])

    def test_active_instance_fails(self):
        result = self.clean()
        result["active_tagged_instances"] = ["i-1"]
        self.assertTrue(ha_preflight.preflight_failures(result))

    def test_existing_load_balancer_fails(self):
        result = self.clean()
        result["load_balancers"] = ["alb"]
        self.assertTrue(ha_preflight.preflight_failures(result))

    def test_existing_target_group_fails(self):
        result = self.clean()
        result["elb_permissions"]["target_groups"] = ["tg"]
        self.assertTrue(ha_preflight.preflight_failures(result))

    def test_denied_permissions_fail(self):
        result = self.clean()
        result["elb_permissions"] = {"status": "denied", "target_groups": []}
        self.assertTrue(ha_preflight.preflight_failures(result))


class CostEstimateTests(unittest.TestCase):
    def test_estimate_stays_below_the_cap(self):
        estimate = ha_preflight.estimate()
        self.assertLess(estimate["hourly_usd"], ha_preflight.CAP_USD)
        self.assertLess(estimate["estimate_60_min_usd"], ha_preflight.CAP_USD)
        self.assertGreater(estimate["remaining_budget_usd"], 0)
        self.assertEqual(estimate["instances"], {"c6i.large": 4, "c6i.xlarge": 1})

    def test_dry_run_prints_topology(self):
        self.assertEqual(ha_preflight.main(["--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
