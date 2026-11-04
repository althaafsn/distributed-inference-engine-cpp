#!/usr/bin/env python3
"""Provision an internal ALB with two gateways and run failover phases."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import controller
import ha_preflight
import multihost_controller as mh
from controller import BASE_REVISION, REGION, aws, command

HERE = Path(__file__).resolve().parent
WORKER_TYPE = "c6i.large"
LOAD_TYPE = "c6i.xlarge"
RUN_LIMIT_SECONDS = 30 * 60


def run_id() -> str:
    return "infer-" + secrets.token_hex(8)


def check_run_limit(start: float) -> None:
    if time.monotonic() - start > RUN_LIMIT_SECONDS:
        raise RuntimeError("run exceeded the 30-minute workload limit")


def save_state(path: Path, state: dict) -> None:
    controller.save_state(path, state)


def create_groups(state: dict, state_path: Path) -> None:
    state["groups"] = {}
    for name, description in (
        ("ssh", "SSH from owner"),
        ("alb", "Internal ALB HTTP"),
        ("gateway", "Gateway app port"),
        ("worker", "Worker app port"),
        ("load", "Load generator"),
    ):
        state["groups"][name] = mh.create_group(name, description, state["run_id"], state["vpc"])
        save_state(state_path, state)
    mh.authorize_ip(state["groups"]["ssh"], 22, state["public_ip"] + "/32")
    mh.authorize_group(state["groups"]["alb"], 8000, state["groups"]["load"])
    mh.authorize_group(state["groups"]["gateway"], 8000, state["groups"]["alb"])
    mh.authorize_group(state["groups"]["worker"], 8001, state["groups"]["gateway"])


def launch_roles(state: dict, state_path: Path, userdata: Path) -> list[str]:
    roles = [
        ("gateway-1", WORKER_TYPE, ["ssh", "gateway"]),
        ("gateway-2", WORKER_TYPE, ["ssh", "gateway"]),
        ("worker-1", WORKER_TYPE, ["ssh", "worker"]),
        ("worker-2", WORKER_TYPE, ["ssh", "worker"]),
        ("load", LOAD_TYPE, ["ssh", "load"]),
    ]
    for role, instance_type, groups in roles:
        mh.launch_instance(
            state,
            state_path,
            role,
            instance_type,
            [state["groups"][name] for name in groups],
            userdata,
            state["run_id"],
        )
    ids = [item["id"] for item in state["instances"].values()]
    aws("ec2", "wait", "instance-running", "--instance-ids", *ids)
    for role, item in state["instances"].items():
        instance = mh.describe_instance(item["id"])
        item["private_ip"] = instance["PrivateIpAddress"]
        item["public_ip"] = instance.get("PublicIpAddress")
        item["volume_ids"] = [
            mapping["Ebs"]["VolumeId"]
            for mapping in instance.get("BlockDeviceMappings", [])
            if mapping.get("Ebs", {}).get("VolumeId")
        ]
    save_state(state_path, state)
    return ids


def connect_roles(state: dict, state_path: Path, key: Path, known_hosts: Path) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(state["instances"])) as pool:
        futures = {
            pool.submit(mh.connect_host, item["id"], key, known_hosts): role
            for role, item in state["instances"].items()
        }
        for future in concurrent.futures.as_completed(futures):
            role = futures[future]
            host, options = future.result()
            state["instances"][role]["host"] = host
            state["instances"][role]["ssh_options"] = options
            print(f"connected {role} {host}", flush=True)
    save_state(state_path, state)


def prepare_roles(state: dict, payload_archive: Path, model: str, output: Path) -> None:
    models = json.loads((HERE / "autotune" / "models.json").read_text())["models"]
    entry = next(item for item in models if item["id"] == model)
    command_text = (
        f"BASE_REVISION={BASE_REVISION} MODEL_NAME={model} "
        f"MODEL_URL='{entry['url']}' MODEL_SHA={entry['sha256']} bash ~/remote_prepare.sh"
    )
    roles = ["gateway-1", "gateway-2", "worker-1", "worker-2"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(roles)) as pool:
        futures = {}
        for role in roles:
            item = state["instances"][role]
            log = output / f"{role}-prepare.log"
            futures[
                pool.submit(mh.ssh_run, item["host"], item["ssh_options"], command_text, 900)
            ] = (role, log)
        for future in concurrent.futures.as_completed(futures):
            role, log = futures[future]
            result = future.result()
            log.write_text(result.stdout + result.stderr, encoding="utf-8")
            print(f"prepared {role}", flush=True)


def wait_remote_file(remote_path: str, item: dict, timeout: float = 900) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = command(
            ["ssh", *item["ssh_options"], item["host"], f"test -f {remote_path}"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"remote file did not appear: {remote_path}")


def wait_url(url: str, item: dict, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = command(
            ["ssh", *item["ssh_options"], item["host"], f"curl -fsS --max-time 2 {url}"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"remote service did not become ready: {url}")


def get_json(url: str, item: dict, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = command(
            ["ssh", *item["ssh_options"], item["host"], f"curl -fsS --max-time 2 {url}"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
        time.sleep(1)
    raise RuntimeError(f"remote JSON did not become ready: {url}")


def start_services(state: dict, model: str, admission: int) -> None:
    for index, role in enumerate(("worker-1", "worker-2"), start=1):
        item = state["instances"][role]
        remote = (
            f"MODEL_NAME={model} ROLE={role} NODE_ID=worker_{index} PORT=8001 "
            f"ORT_INTRA_THREADS=2 BATCH_DEADLINE_MS=10 MAX_BATCH_SIZE=32 CACHE_CAPACITY=1 "
            f"bash ~/remote_worker.sh > ~/benchmark-work/results/worker-{index}-start.log "
            "2>&1 < /dev/null & echo started"
        )
        mh.ssh_run(item["host"], item["ssh_options"], remote, 30)
    for role in ("worker-1", "worker-2"):
        item = state["instances"][role]
        wait_url("http://127.0.0.1:8001/health", item)
        mh.require_fresh_health(get_json("http://127.0.0.1:8001/health", item), role)
    workers = " ".join(
        f"{state['instances'][role]['private_ip']}:8001" for role in ("worker-1", "worker-2")
    )
    for index, role in enumerate(("gateway-1", "gateway-2"), start=1):
        item = state["instances"][role]
        remote = (
            f"GATEWAY_PORT=8000 GATEWAY_ID=gateway-{index} ROUTING_POLICY=least_loaded "
            f"ADMISSION_MAX_INFLIGHT={admission} bash ~/remote_gateway.sh {workers} "
            f"> ~/benchmark-work/results/gateway-{index}-start.log 2>&1 < /dev/null & echo started"
        )
        mh.ssh_run(item["host"], item["ssh_options"], remote, 30)
    for role in ("gateway-1", "gateway-2"):
        wait_url("http://127.0.0.1:8000/health", state["instances"][role])


def start_one_gateway(state: dict, role: str, index: int, admission: int) -> None:
    workers = " ".join(
        f"{state['instances'][worker]['private_ip']}:8001" for worker in ("worker-1", "worker-2")
    )
    item = state["instances"][role]
    remote = (
        f"GATEWAY_PORT=8000 GATEWAY_ID=gateway-{index} ROUTING_POLICY=least_loaded "
        f"ADMISSION_MAX_INFLIGHT={admission} bash ~/remote_gateway.sh {workers} "
        f"> ~/benchmark-work/results/gateway-{index}-recovery-start.log 2>&1 < /dev/null & echo started"
    )
    mh.ssh_run(item["host"], item["ssh_options"], remote, 30)
    wait_url("http://127.0.0.1:8000/health", item)


def create_alb(state: dict, state_path: Path) -> None:
    name = f"{state['run_id']}-gw"
    group = aws(
        "elbv2",
        "create-target-group",
        "--name",
        name,
        "--protocol",
        "HTTP",
        "--port",
        "8000",
        "--vpc-id",
        state["vpc"],
        "--health-check-path",
        "/health",
        "--health-check-interval-seconds",
        "5",
        "--health-check-timeout-seconds",
        "3",
        "--healthy-threshold-count",
        "2",
        "--unhealthy-threshold-count",
        "2",
        "--matcher",
        "HttpCode=200",
        "--target-type",
        "instance",
    )
    state["alb"] = {"target_group_arn": group["TargetGroups"][0]["TargetGroupArn"]}
    save_state(state_path, state)
    load_balancer = aws(
        "elbv2",
        "create-load-balancer",
        "--name",
        f"{state['run_id']}-alb",
        "--subnets",
        *state["subnets"],
        "--security-groups",
        state["groups"]["alb"],
        "--scheme",
        "internal",
        "--type",
        "application",
    )["LoadBalancers"][0]
    state["alb"]["arn"] = load_balancer["LoadBalancerArn"]
    state["alb"]["dns"] = load_balancer["DNSName"]
    save_state(state_path, state)
    listener = aws(
        "elbv2",
        "create-listener",
        "--load-balancer-arn",
        state["alb"]["arn"],
        "--protocol",
        "HTTP",
        "--port",
        "8000",
        "--default-actions",
        f"Type=forward,TargetGroupArn={state['alb']['target_group_arn']}",
    )["Listeners"][0]
    state["alb"]["listener_arn"] = listener["ListenerArn"]
    aws(
        "elbv2",
        "register-targets",
        "--target-group-arn",
        state["alb"]["target_group_arn"],
        "--targets",
        f"Id={state['instances']['gateway-1']['id']},Port=8000",
        f"Id={state['instances']['gateway-2']['id']},Port=8000",
    )
    save_state(state_path, state)
    wait_alb_active(state)
    wait_targets_healthy(state, {"gateway-1", "gateway-2"})


def target_states(state: dict) -> dict[str, str]:
    result = aws(
        "elbv2",
        "describe-target-health",
        "--target-group-arn",
        state["alb"]["target_group_arn"],
    )
    return {
        item["Target"]["Id"]: item["TargetHealth"]["State"]
        for item in result["TargetHealthDescriptions"]
    }


def wait_alb_active(state: dict) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = aws(
            "elbv2",
            "describe-load-balancers",
            "--load-balancer-arns",
            state["alb"]["arn"],
        )
        if result["LoadBalancers"][0]["State"]["Code"] == "active":
            return
        time.sleep(3)
    raise RuntimeError("ALB did not become active")


def wait_targets_healthy(state: dict, roles: set[str], timeout: float = 240) -> None:
    ids = {state["instances"][role]["id"] for role in roles}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = target_states(state)
        if all(states.get(target) == "healthy" for target in ids):
            return
        time.sleep(3)
    raise RuntimeError(f"targets did not become healthy: {target_states(state)}")


def load_env(state: dict, label: str, duration: float) -> str:
    return (
        f"GATEWAY_IP={state['alb']['dns']} DATA_COUNT=500 ACCURACY_COUNT=500 "
        f"REQUESTS=500 MODE=unique TRANSPORT=binary LEVELS='1' LABEL={label} "
        f"RATE=50 DURATION={duration} CONCURRENCY=128"
    )


def collect_state(state: dict, phase: str, output: Path) -> dict:
    result = {"targets": target_states(state)}
    gateway_roles = ["gateway-1", "gateway-2"]
    recovery = state["instances"].get("gateway-1-recovery")
    if recovery and recovery.get("host"):
        gateway_roles[0] = "gateway-1-recovery"
    for index, role in enumerate(gateway_roles, start=1):
        item = state["instances"][role]
        if item.get("id") is None:
            result[f"gateway-{index}"] = {"error": "not recorded"}
            continue
        try:
            result[f"gateway-{index}"] = get_json("http://127.0.0.1:8000/stats", item, timeout=5)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            result[f"gateway-{index}"] = {"error": type(exc).__name__}
    for index, role in enumerate(("worker-1", "worker-2"), start=1):
        result[f"worker-{index}"] = get_json(
            "http://127.0.0.1:8001/health", state["instances"][role], timeout=5
        )
    (output / phase / "state.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def fetch_phase(state: dict, phase: str, output: Path, timeout: float) -> None:
    load = state["instances"]["load"]
    result = mh.ssh_run(
        load["host"],
        load["ssh_options"],
        f"PHASE=load {load_env(state, phase, timeout)} bash ~/remote_load.sh",
        timeout=int(timeout) + 900,
    )
    (output / f"load-{phase}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{phase} load failed; see load-{phase}.log")
    mh.fetch_results(load, output / phase)
    collect_state(state, phase, output)


def run_failover(state: dict, output: Path, state_path: Path) -> dict:
    load = state["instances"]["load"]
    command_text = (
        f"PHASE=load {load_env(state, 'failover', 20)} "
        "nohup bash ~/remote_load.sh > ~/failover.log 2>&1 < /dev/null & echo started"
    )
    mh.ssh_run(load["host"], load["ssh_options"], command_text, 30)
    wait_remote_file("~/benchmark-work/results/failover.started", load, 900)
    load_started_at = time.monotonic()
    time.sleep(2.0)
    before = get_json("http://127.0.0.1:8000/stats", state["instances"]["gateway-1"])
    state["alb"]["gateway1_before_kill"] = before["admission"]
    killed_at = time.monotonic()
    state["alb"]["kill_requested_seconds"] = round(killed_at - load_started_at, 3)
    aws(
        "ec2",
        "terminate-instances",
        "--instance-ids",
        state["instances"]["gateway-1"]["id"],
    )
    observed = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if target_states(state).get(state["instances"]["gateway-1"]["id"]) != "healthy":
            observed = time.monotonic() - killed_at
            break
        time.sleep(0.5)
    state["alb"]["observed_failover_seconds"] = round(observed, 3) if observed else None
    save_state(state_path, state)
    wait_remote_file("~/benchmark-work/results/failover.done", load, 900)
    mh.fetch_results(load, output / "failover")
    collect_state(state, "failover", output)
    return {
        "observed_failover_seconds": state["alb"]["observed_failover_seconds"],
        "gateway1_before_kill": before["admission"],
    }


def launch_recovery_gateway(state: dict, state_path: Path, userdata: Path, model: str) -> None:
    role = "gateway-1-recovery"
    mh.launch_instance(
        state,
        state_path,
        role,
        WORKER_TYPE,
        [state["groups"]["ssh"], state["groups"]["gateway"]],
        userdata,
        state["run_id"],
    )
    aws("ec2", "wait", "instance-running", "--instance-ids", state["instances"][role]["id"])
    instance = mh.describe_instance(state["instances"][role]["id"])
    state["instances"][role].update(
        {
            "private_ip": instance["PrivateIpAddress"],
            "public_ip": instance.get("PublicIpAddress"),
            "volume_ids": [
                mapping["Ebs"]["VolumeId"]
                for mapping in instance.get("BlockDeviceMappings", [])
                if mapping.get("Ebs", {}).get("VolumeId")
            ],
        }
    )
    save_state(state_path, state)
    host, options = mh.connect_host(
        state["instances"][role]["id"],
        state_path.parent / "benchmark_key",
        state_path.parent / "known_hosts",
    )
    state["instances"][role]["host"] = host
    state["instances"][role]["ssh_options"] = options
    save_state(state_path, state)
    mh.scp_file(host, options, state_path.parent / "payload.tar.gz")
    mh.ssh_run(host, options, "tar -xzf ~/payload.tar.gz -C ~")
    models = json.loads((HERE / "autotune" / "models.json").read_text())["models"]
    entry = next(item for item in models if item["id"] == model)
    prepare = (
        f"BASE_REVISION={BASE_REVISION} MODEL_NAME={model} "
        f"MODEL_URL='{entry['url']}' MODEL_SHA={entry['sha256']} bash ~/remote_prepare.sh"
    )
    result = mh.ssh_run(host, options, prepare, 900)
    (state_path.parent / "gateway-1-recovery-prepare.log").write_text(
        result.stdout + result.stderr, encoding="utf-8"
    )
    start_one_gateway(state, role, 1, 4)
    aws(
        "elbv2",
        "register-targets",
        "--target-group-arn",
        state["alb"]["target_group_arn"],
        "--targets",
        f"Id={state['instances'][role]['id']},Port=8000",
    )
    try:
        aws(
            "elbv2",
            "deregister-targets",
            "--target-group-arn",
            state["alb"]["target_group_arn"],
            "--targets",
            f"Id={state['instances']['gateway-1']['id']},Port=8000",
        )
    except subprocess.CalledProcessError as exc:
        print(f"warning: could not deregister old gateway target: {exc.stderr}", file=sys.stderr)
    wait_targets_healthy(state, {role})


def delete_alb(state: dict) -> bool:
    alb = state.get("alb", {})
    if not alb:
        return True
    for key, command_name, arg in (
        ("listener_arn", "delete-listener", "--listener-arn"),
        ("target_group_arn", "delete-target-group", "--target-group-arn"),
        ("arn", "delete-load-balancer", "--load-balancer-arn"),
    ):
        value = alb.get(key)
        if not value:
            continue
        for attempt in range(3):
            try:
                aws("elbv2", command_name, arg, value)
                break
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr or ""
                if "NotFound" in stderr:
                    break
                if attempt == 2:
                    print(f"ERROR deleting ALB resource {key}: {stderr}", file=sys.stderr)
                    return False
                time.sleep(3)
    if not alb.get("arn"):
        return True
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            aws("elbv2", "describe-load-balancers", "--load-balancer-arns", alb["arn"])
        except subprocess.CalledProcessError as exc:
            if "LoadBalancerNotFound" in (exc.stderr or ""):
                return True
        time.sleep(3)
    print("ERROR: ALB deletion was not confirmed", file=sys.stderr)
    return False


def cleanup_ha_state(state: dict) -> bool:
    if not state.get("account"):
        return True
    alb_ok = delete_alb(state)
    base_ok = mh.cleanup_state(state)
    return alb_ok and base_ok


def run(args: argparse.Namespace) -> int:
    state = {
        "run_id": run_id(),
        "region": REGION,
        "account": None,
        "subnet": None,
        "subnets": [],
        "vpc": None,
        "public_ip": None,
        "groups": {},
        "instances": {},
        "alb": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    directory = HERE / "runs" / state["run_id"]
    directory.mkdir(parents=True, exist_ok=False)
    state_path = directory / "state.json"
    key = directory / "benchmark_key"
    known_hosts = directory / "known_hosts"
    known_hosts.touch()
    payload_dir = directory / "payload"
    payload_dir.mkdir()
    save_state(state_path, state)
    success = False
    result_code = 1
    start = time.monotonic()
    try:
        if ha_preflight.run_preflight() != 0:
            raise RuntimeError("preflight failed; refusing to create AWS resources")
        context = controller.preflight()
        subnets = aws(
            "ec2",
            "describe-subnets",
            "--filters",
            f"Name=vpc-id,Values={context['vpc']}",
            "--query",
            "Subnets[].{SubnetId:SubnetId,AvailabilityZone:AvailabilityZone,"
            "MapPublicIpOnLaunch:MapPublicIpOnLaunch,"
            "AvailableIpAddressCount:AvailableIpAddressCount}",
            "--output",
            "json",
        )
        chosen = ha_preflight.choose_subnets(subnets)
        state.update(
            {
                "account": context["account"],
                "subnet": chosen[0]["SubnetId"],
                "subnets": [item["SubnetId"] for item in chosen],
                "vpc": context["vpc"],
                "public_ip": context["public_ip"],
            }
        )
        save_state(state_path, state)
        mh.prepare_payload(payload_dir)
        command(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-C",
                "ha-benchmark",
                "-N",
                "",
                "-f",
                str(key),
            ],
            capture_output=True,
        )
        userdata = directory / "userdata.sh"
        userdata.write_text(
            controller.user_data(key.with_suffix(".pub").read_text().strip()),
            encoding="utf-8",
        )
        create_groups(state, state_path)
        launch_roles(state, state_path, userdata)
        connect_roles(state, state_path, key, known_hosts)
        payload_archive = directory / "payload.tar.gz"
        for role, item in state["instances"].items():
            mh.scp_file(item["host"], item["ssh_options"], payload_archive)
            mh.ssh_run(item["host"], item["ssh_options"], "tar -xzf ~/payload.tar.gz -C ~")
        prepare_roles(state, payload_archive, args.model, directory)
        start_services(state, args.model, args.admission_max)
        check_run_limit(start)
        try:
            create_alb(state, state_path)
        except subprocess.CalledProcessError as exc:
            print(f"ERROR creating ALB resources: {exc.stderr}", file=sys.stderr)
            raise
        print(f"ALB ready: {state['alb']['dns']}", flush=True)

        check_run_limit(start)
        fetch_phase(state, "normal", directory, 10)
        check_run_limit(start)
        failover = run_failover(state, directory, state_path)
        check_run_limit(start)
        launch_recovery_gateway(state, state_path, userdata, args.model)
        check_run_limit(start)
        fetch_phase(state, "recovery", directory, 10)
        state["failover"] = failover
        save_state(state_path, state)
        (directory / "run.json").write_text(json.dumps(state, indent=2) + "\n")
        success = True
        result_code = 0
        print(
            json.dumps(
                {
                    "normal": state["instances"],
                    "failover": failover,
                    "recovery": "complete",
                    "alb_dns": state["alb"]["dns"],
                    "elapsed_seconds": round(time.monotonic() - start, 1),
                }
            ),
            flush=True,
        )
    finally:
        try:
            if not cleanup_ha_state(state):
                success = False
                result_code = 1
        except Exception as exc:
            print(f"ERROR: cleanup failed: {exc}", file=sys.stderr)
            print(f"state retained at {state_path}", file=sys.stderr)
            success = False
            result_code = 1
        if not success:
            print(f"evidence retained at {directory}", file=sys.stderr)
    return result_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("preflight",))
    parser.add_argument("--model", default="resnet50-v2-7")
    parser.add_argument("--admission-max", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "topology": {
                        "gateways": 2,
                        "workers": 2,
                        "load": 1,
                        "alb": "internal application",
                    },
                    "admission_max_per_gateway": args.admission_max,
                    "cost": ha_preflight.estimate(),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "preflight":
        return ha_preflight.run_preflight()
    if args.admission_max < 1:
        parser.error("--admission-max must be at least 1")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
