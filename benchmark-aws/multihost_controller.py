#!/usr/bin/env python3
"""Provision an isolated gateway/worker/load topology and run one benchmark candidate."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import controller
from controller import AMI, BASE_REVISION, REGION, TAG_KEY, aws, command, identity

HERE = Path(__file__).resolve().parent
WORKER_TYPE = "c6i.large"
LOAD_TYPE = "c6i.xlarge"
MAX_WORKERS = 8


def run_id() -> str:
    return "infer-" + secrets.token_hex(8)


def save_state(path: Path, state: dict) -> None:
    controller.save_state(path, state)


def create_group(name: str, description: str, run: str, vpc: str) -> str:
    tags = [{"Key": TAG_KEY, "Value": run}]
    result = aws(
        "ec2",
        "create-security-group",
        "--group-name",
        f"{run}-{name}",
        "--description",
        description,
        "--vpc-id",
        vpc,
        "--tag-specifications",
        json.dumps([{"ResourceType": "security-group", "Tags": tags}]),
    )
    return result["GroupId"]


def authorize_ip(group_id: str, port: int, cidr: str) -> None:
    aws(
        "ec2",
        "authorize-security-group-ingress",
        "--group-id",
        group_id,
        "--protocol",
        "tcp",
        "--port",
        str(port),
        "--cidr",
        cidr,
    )


def authorize_group(group_id: str, port: int, source_group: str) -> None:
    aws(
        "ec2",
        "authorize-security-group-ingress",
        "--group-id",
        group_id,
        "--ip-permissions",
        json.dumps(
            [
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "UserIdGroupPairs": [{"GroupId": source_group}],
                }
            ]
        ),
    )


def launch_instance(
    state: dict,
    state_path: Path,
    role: str,
    instance_type: str,
    group_ids: list[str],
    userdata: Path,
    key_name: str,
) -> str:
    tags = [
        {"Key": TAG_KEY, "Value": state["run_id"]},
        {"Key": "Name", "Value": f"{state['run_id']}-{role}"},
    ]
    block = [
        {
            "DeviceName": "/dev/xvda",
            "Ebs": {
                "VolumeSize": 12,
                "VolumeType": "gp3",
                "DeleteOnTermination": True,
                "Encrypted": True,
            },
        }
    ]
    launch = aws(
        "ec2",
        "run-instances",
        "--image-id",
        AMI,
        "--instance-type",
        instance_type,
        "--subnet-id",
        state["subnet"],
        "--security-group-ids",
        *group_ids,
        "--count",
        "1",
        "--client-token",
        f"{state['run_id']}-{role}"[:64],
        "--instance-initiated-shutdown-behavior",
        "terminate",
        "--metadata-options",
        "HttpTokens=required,HttpEndpoint=enabled",
        "--block-device-mappings",
        json.dumps(block),
        "--tag-specifications",
        json.dumps([{"ResourceType": kind, "Tags": tags} for kind in ("instance", "volume")]),
        "--user-data",
        "file://" + str(userdata),
    )
    instance_id = launch["Instances"][0]["InstanceId"]
    state["instances"][role] = {"id": instance_id}
    save_state(state_path, state)
    print(f"launched {role} {instance_id}", flush=True)
    return instance_id


def describe_instance(instance_id: str) -> dict:
    response = aws("ec2", "describe-instances", "--instance-ids", instance_id)
    return response["Reservations"][0]["Instances"][0]


def connect_host(instance_id: str, key: Path, known_hosts: Path) -> tuple[str, list[str]]:
    host_key = controller.console_host_key(instance_id)
    instance = describe_instance(instance_id)
    public = instance.get("PublicIpAddress")
    if not public:
        raise RuntimeError(f"instance {instance_id} has no public address")
    with known_hosts.open("a", encoding="utf-8") as handle:
        handle.write(f"{public} {host_key}\n")
    options = [
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
    ]
    host = f"ec2-user@{public}"
    deadline = time.monotonic() + 240
    while True:
        attempt = command(
            ["ssh", *options, host, "true"], check=False, capture_output=True, timeout=20
        )
        if attempt.returncode == 0:
            return host, options
        if time.monotonic() > deadline:
            raise RuntimeError(f"SSH did not become ready for {instance_id}")
        time.sleep(5)


def scp_file(host: str, options: list[str], source: Path, target: str = "~/"):
    command(["scp", *options, str(source), f"{host}:{target}"], timeout=180)


def ssh_run(host: str, options: list[str], remote: str, timeout: int = 900, check: bool = True):
    return command(
        ["ssh", *options, host, remote],
        check=check,
        capture_output=True,
        timeout=timeout,
    )


def prepare_payload(directory: Path) -> dict:
    manifest = controller.prepare_source(directory, "instrumented")
    for name in (
        "remote_prepare.sh",
        "remote_worker.sh",
        "remote_gateway.sh",
        "remote_load.sh",
        "remote_stop.sh",
        "remote_kill_worker.sh",
    ):
        shutil.copy2(HERE / "multihost" / name, directory / name)
    for name in ("loadgen.py", "workloads.py", "prepare_data.py"):
        shutil.copy2(HERE / "autotune" / name, directory / name)
    shutil.copy2(HERE / "requirements-benchmark.txt", directory / "requirements-benchmark.txt")
    shutil.copy2(HERE / "autotune" / "models.json", directory / "models.json")
    archive = directory.parent / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for child in sorted(directory.iterdir()):
            if child.name in {"payload.tar.gz"}:
                continue
            tar.add(child, arcname=child.name)
    return manifest


def require_fresh_health(health: dict, role: str) -> None:
    total = health.get("total_requests", 0)
    hits = health.get("cache_hits", 0)
    if total != 0 or hits != 0:
        raise RuntimeError(
            f"{role} started with a non-empty cache: total_requests={total} cache_hits={hits}"
        )


def gateway_env(routing: str, admission_max: int) -> str:
    return f"GATEWAY_PORT=8000 ROUTING_POLICY={routing} ADMISSION_MAX_INFLIGHT={admission_max} "


def hold_sequence(
    rates: list[float], admission_max: int, repeats: int, matrix: bool
) -> list[tuple[float, int, int]]:
    conditions = []
    for rate in rates:
        if matrix:
            for repeat in range(repeats):
                order = [0, admission_max] if repeat % 2 == 0 else [admission_max, 0]
                conditions.extend((rate, admission, repeat) for admission in order)
        else:
            conditions.append((rate, admission_max, 0))
    return conditions


def hold_label(rate: float, admission_max: int, repeat: int) -> str:
    return f"hold-rate{rate:g}-a{admission_max}-r{repeat + 1}"


def cleanup_state(state: dict) -> bool:
    if state.get("region") != REGION or identity() != state.get("account"):
        raise RuntimeError("account or region differs from the recorded run; refusing cleanup")
    instance_ids = [item["id"] for item in state.get("instances", {}).values() if item.get("id")]
    if instance_ids:
        aws("ec2", "terminate-instances", "--instance-ids", *instance_ids)
        if not controller.wait_for_termination(instance_ids, timeout_seconds=300):
            print("ERROR: instance termination unverified", file=sys.stderr)
            return False
    pending = {
        name: state.get("groups", {}).get(name)
        for name in ("worker", "gateway", "load", "ssh", "alb")
        if state.get("groups", {}).get(name)
    }
    deadline = time.monotonic() + 180
    while pending:
        for name, group_id in list(pending.items()):
            try:
                aws("ec2", "delete-security-group", "--group-id", group_id)
                pending.pop(name)
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr or ""
                if "InvalidGroup.NotFound" in stderr:
                    pending.pop(name)
                elif any(code in stderr for code in ("DependencyViolation", "InvalidGroup.InUse")):
                    continue
                else:
                    print(f"ERROR deleting group {name}: {stderr}", file=sys.stderr)
                    return False
        if not pending:
            break
        if time.monotonic() >= deadline:
            print("ERROR: security groups still in use", file=sys.stderr)
            return False
        time.sleep(5)
    print("Cleanup verified: all recorded instances terminated and groups removed.", flush=True)
    return True


def run(args: argparse.Namespace) -> int:
    state = {
        "run_id": run_id(),
        "region": REGION,
        "account": None,
        "subnet": None,
        "vpc": None,
        "public_ip": None,
        "groups": {},
        "instances": {},
        "routing": args.routing,
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
    try:
        context = controller.preflight()
        state.update(
            {
                "account": context["account"],
                "subnet": context["subnet"],
                "vpc": context["vpc"],
                "public_ip": context["public_ip"],
            }
        )
        save_state(state_path, state)
        prepare_payload(payload_dir)
        command(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-C",
                "multihost-benchmark",
                "-N",
                "",
                "-f",
                str(key),
            ],
            capture_output=True,
        )
        public_key = key.with_suffix(".pub").read_text().strip()
        userdata = directory / "userdata.sh"
        userdata.write_text(controller.user_data(public_key), encoding="utf-8")

        state["groups"] = {
            "ssh": create_group("ssh", "SSH from owner", state["run_id"], state["vpc"]),
            "gateway": create_group("gateway", "Gateway app port", state["run_id"], state["vpc"]),
            "worker": create_group("worker", "Worker app port", state["run_id"], state["vpc"]),
            "load": create_group("load", "Load generator", state["run_id"], state["vpc"]),
        }
        save_state(state_path, state)
        authorize_ip(state["groups"]["ssh"], 22, state["public_ip"] + "/32")
        authorize_group(state["groups"]["gateway"], 8000, state["groups"]["load"])
        authorize_group(state["groups"]["worker"], 8001, state["groups"]["gateway"])

        launch_instance(
            state,
            state_path,
            "gateway",
            WORKER_TYPE,
            [state["groups"]["ssh"], state["groups"]["gateway"]],
            userdata,
            state["run_id"],
        )
        for number in range(1, args.workers + 1):
            launch_instance(
                state,
                state_path,
                f"worker-{number}",
                WORKER_TYPE,
                [state["groups"]["ssh"], state["groups"]["worker"]],
                userdata,
                state["run_id"],
            )
        launch_instance(
            state,
            state_path,
            "load",
            LOAD_TYPE,
            [state["groups"]["ssh"], state["groups"]["load"]],
            userdata,
            state["run_id"],
        )

        ids = [item["id"] for item in state["instances"].values()]
        aws("ec2", "wait", "instance-running", "--instance-ids", *ids)
        for role, item in state["instances"].items():
            instance = describe_instance(item["id"])
            item["private_ip"] = instance["PrivateIpAddress"]
            item["public_ip"] = instance.get("PublicIpAddress")
            item["volume_ids"] = [
                mapping["Ebs"]["VolumeId"]
                for mapping in instance.get("BlockDeviceMappings", [])
                if mapping.get("Ebs", {}).get("VolumeId")
            ]
        save_state(state_path, state)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ids)) as pool:
            futures = {
                pool.submit(connect_host, item["id"], key, known_hosts): role
                for role, item in state["instances"].items()
            }
            for future in concurrent.futures.as_completed(futures):
                role = futures[future]
                host, options = future.result()
                state["instances"][role]["host"] = host
                state["instances"][role]["ssh_options"] = options
                print(f"connected {role} {host}", flush=True)
        save_state(state_path, state)

        payload_archive = directory / "payload.tar.gz"
        for role in state["instances"]:
            item = state["instances"][role]
            scp_file(item["host"], item["ssh_options"], payload_archive, "~/payload.tar.gz")
            ssh_run(item["host"], item["ssh_options"], "tar -xzf ~/payload.tar.gz -C ~")

        model = args.model
        models = json.loads((HERE / "autotune" / "models.json").read_text())["models"]
        model_entry = next((entry for entry in models if entry["id"] == model), None)
        if model_entry is None:
            raise RuntimeError(f"unknown model id: {model}")
        prepare_command = (
            f"BASE_REVISION={BASE_REVISION} MODEL_NAME={model} "
            f"MODEL_URL='{model_entry['url']}' MODEL_SHA={model_entry['sha256']} "
            "bash ~/remote_prepare.sh"
        )
        prepare_roles = ["gateway"] + [f"worker-{n}" for n in range(1, args.workers + 1)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(prepare_roles)) as pool:
            futures = {}
            for role in prepare_roles:
                item = state["instances"][role]
                log_path = directory / f"{role}-prepare.log"
                futures[
                    pool.submit(ssh_run, item["host"], item["ssh_options"], prepare_command)
                ] = (role, log_path)
            for future in concurrent.futures.as_completed(futures):
                role, log_path = futures[future]
                result = future.result()
                log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
                print(f"prepared {role}", flush=True)

        worker_roles = [f"worker-{n}" for n in range(1, args.workers + 1)]
        service_roles = ["gateway"] + worker_roles

        def wait_remote(url: str, item: dict, timeout: float = 120):
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

        def remote_json(url: str, item: dict, timeout: float = 120) -> dict:
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
            raise RuntimeError(f"remote service did not return JSON: {url}")

        def wait_remote_file(remote_path: str, item: dict, timeout: float = 120):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                result = command(
                    [
                        "ssh",
                        *item["ssh_options"],
                        item["host"],
                        f"test -f {remote_path}",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    return
                time.sleep(1)
            raise RuntimeError(f"remote file did not appear: {remote_path}")

        def start_services(admission_max: int):
            for number, role in enumerate(worker_roles, start=1):
                item = state["instances"][role]
                remote = (
                    f"MODEL_NAME={model} ROLE={role} NODE_ID=worker_{number} PORT=8001 "
                    f"ORT_INTRA_THREADS={args.threads} BATCH_DEADLINE_MS={args.deadline} "
                    f"MAX_BATCH_SIZE={args.max_batch} CACHE_CAPACITY={args.cache} "
                    f"bash ~/remote_worker.sh > ~/benchmark-work/results/worker-{number}-start.log "
                    "2>&1 < /dev/null & echo started"
                )
                ssh_run(item["host"], item["ssh_options"], remote, timeout=30)
            for number, role in enumerate(worker_roles, start=1):
                item = state["instances"][role]
                wait_remote("http://127.0.0.1:8001/health", item)
                require_fresh_health(remote_json("http://127.0.0.1:8001/health", item), role)
            gateway = state["instances"]["gateway"]
            workers = " ".join(
                f"{state['instances'][role]['private_ip']}:8001" for role in worker_roles
            )
            ssh_run(
                gateway["host"],
                gateway["ssh_options"],
                f"{gateway_env(args.routing, admission_max)}"
                f"bash ~/remote_gateway.sh {workers} "
                "> ~/benchmark-work/results/gateway-start.log 2>&1 < /dev/null & echo started",
                timeout=30,
            )
            wait_remote("http://127.0.0.1:8000/stats", gateway)

        def stop_services():
            for role in service_roles:
                item = state["instances"][role]
                ssh_run(
                    item["host"],
                    item["ssh_options"],
                    "bash ~/remote_stop.sh",
                    timeout=60,
                )

        def restart_services(admission_max: int):
            stop_services()
            start_services(admission_max)

        def collect_metrics(target: Path) -> None:
            target.mkdir(parents=True, exist_ok=True)

            def host_metrics(role: str) -> str:
                pid = "gateway.pid" if role == "gateway" else f"{role}.pid"
                return (
                    "R=$HOME/benchmark-work/results; "
                    "echo '=== nproc ==='; nproc; "
                    "echo '=== free ==='; free -m; "
                    "echo '=== mpstat ==='; mpstat 1 1 2>&1 || true; "
                    "echo '=== pidstat ==='; "
                    f'if [ -f "$R/{pid}" ]; then '
                    f'pidstat -p "$(cat "$R/{pid}")" 1 1 2>&1 || true; '
                    "else echo 'pid file missing'; fi"
                )

            def capture(role: str) -> None:
                item = state["instances"][role]
                number = role.split("-")[1] if role.startswith("worker-") else ""
                try:
                    health_url = (
                        "http://127.0.0.1:8000/stats"
                        if role == "gateway"
                        else "http://127.0.0.1:8001/health"
                    )
                    health = remote_json(health_url, item, timeout=10)
                    (target / f"{role}-health.json").write_text(
                        json.dumps(health, indent=2) + "\n", encoding="utf-8"
                    )
                except Exception as exc:
                    (target / f"{role}-health.json").write_text(
                        json.dumps({"error": str(exc)}) + "\n", encoding="utf-8"
                    )
                result = ssh_run(
                    item["host"],
                    item["ssh_options"],
                    host_metrics(role),
                    check=False,
                    timeout=60,
                )
                (target / f"{role}-metrics.txt").write_text(
                    result.stdout + result.stderr, encoding="utf-8"
                )
                vmstat_name = (
                    "gateway-vmstat.txt" if role == "gateway" else f"worker-{number}-vmstat.txt"
                )
                result = ssh_run(
                    item["host"],
                    item["ssh_options"],
                    f"cat ~/benchmark-work/results/{vmstat_name}",
                    check=False,
                    timeout=30,
                )
                (target / f"{role}-vmstat.txt").write_text(
                    result.stdout + result.stderr, encoding="utf-8"
                )
                log_name = "gateway.log" if role == "gateway" else f"{role}.log"
                result = ssh_run(
                    item["host"],
                    item["ssh_options"],
                    f"tail -200 ~/benchmark-work/results/{log_name}",
                    check=False,
                    timeout=30,
                )
                (target / f"{role}-log-tail.txt").write_text(
                    result.stdout + result.stderr, encoding="utf-8"
                )

            for role in service_roles:
                capture(role)

        load = state["instances"]["load"]
        gateway_ip = state["instances"]["gateway"]["private_ip"]

        def load_env(
            level: int | None = None,
            label: str | None = None,
            rate: float | None = None,
            duration: float | None = None,
            mode: str | None = None,
            collect_output: bool = False,
        ) -> str:
            parts = [
                f"GATEWAY_IP={gateway_ip}",
                f"DATA_COUNT={args.data_count}",
                f"ACCURACY_COUNT={args.accuracy_count}",
                f"REQUESTS={args.requests}",
                f"MODE={mode or args.mode}",
                f"TRANSPORT={args.transport}",
            ]
            if level is not None:
                parts.append(f"LEVELS='{level}'")
            elif rate is not None:
                parts.append("LEVELS='1'")
            if label:
                parts.append(f"LABEL={label}")
            if rate is not None:
                parts.extend(
                    [
                        f"RATE={rate}",
                        f"DURATION={duration}",
                        f"CONCURRENCY={args.hold_concurrency}",
                    ]
                )
            if collect_output:
                parts.append("COLLECT_OUTPUT=1")
            return " ".join(parts)

        if not args.skip_accuracy:
            restart_services(args.admission_max)
            result = ssh_run(
                load["host"],
                load["ssh_options"],
                f"PHASE=accuracy {load_env()} bash ~/remote_load.sh",
                timeout=900,
                check=False,
            )
            (directory / "load-accuracy.log").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            if result.returncode:
                raise RuntimeError(
                    f"accuracy phase failed with {result.returncode}; see load-accuracy.log"
                )
            fetch_results(load, directory / "accuracy")
            collect_metrics(directory / "accuracy" / "metrics")

        if not args.skip_levels:
            for level in args.levels:
                restart_services(args.admission_max)
                result = ssh_run(
                    load["host"],
                    load["ssh_options"],
                    f"PHASE=load {load_env(level=level, label=f'level-{level}')} "
                    "bash ~/remote_load.sh",
                    timeout=1200,
                    check=False,
                )
                (directory / f"load-level-{level}.log").write_text(
                    result.stdout + result.stderr, encoding="utf-8"
                )
                if result.returncode:
                    raise RuntimeError(
                        f"level {level} failed with {result.returncode}; see load-level-{level}.log"
                    )
                fetch_results(load, directory / f"level-{level}")
                collect_metrics(directory / f"level-{level}" / "metrics")

        hold_rates = args.hold_rates or ([args.hold_rate] if args.hold_rate is not None else [])
        if hold_rates:
            multi = args.hold_matrix or bool(args.hold_rates)
            conditions = hold_sequence(
                hold_rates, args.admission_max, args.hold_repeats, args.hold_matrix
            )
            for rate, admission, repeat in conditions:
                label = hold_label(rate, admission, repeat) if multi else "hold"
                restart_services(admission)
                result = ssh_run(
                    load["host"],
                    load["ssh_options"],
                    (
                        f"PHASE=load {load_env(rate=rate, duration=args.hold_seconds, label=label)} "
                        "bash ~/remote_load.sh"
                    ),
                    timeout=int(args.hold_seconds) + 900,
                    check=False,
                )
                log_name = f"load-{label}.log"
                (directory / log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
                if result.returncode:
                    raise RuntimeError(
                        f"hold {label} failed with {result.returncode}; see {log_name}"
                    )
                collect_metrics(directory / label / "metrics")
            if multi:
                fetch_results(load, directory / "holds")
            else:
                fetch_results(load, directory / "hold")

        if args.failover_rate is not None:
            restart_services(args.admission_max)
            background = (
                f"PHASE=load {load_env(rate=args.failover_rate, duration=args.failover_seconds, label='failover', mode='repeated', collect_output=True)} "
                "nohup bash ~/remote_load.sh > ~/failover.log "
                "2>&1 < /dev/null & echo started"
            )
            ssh_run(load["host"], load["ssh_options"], background, timeout=30)
            try:
                wait_remote_file("~/benchmark-work/results/failover.started", load, timeout=900)
            except Exception:
                result = ssh_run(
                    load["host"],
                    load["ssh_options"],
                    "tail -80 ~/failover.log",
                    check=False,
                    timeout=30,
                )
                print(result.stdout + result.stderr, flush=True)
                raise
            time.sleep(args.failover_kill_after)
            target_role = f"worker-{args.failover_kill_worker}"
            target = state["instances"][target_role]
            ssh_run(
                target["host"],
                target["ssh_options"],
                f"bash ~/remote_kill_worker.sh worker_{args.failover_kill_worker}",
                timeout=60,
            )
            print(f"killed {target_role} during failover load", flush=True)
            wait_remote_file(
                "~/benchmark-work/results/failover.done",
                load,
                timeout=int(args.failover_seconds) + 300,
            )
            fetch_results(load, directory / "failover")
            collect_metrics(directory / "failover" / "metrics")

        success = True
        result_code = 0
        print("benchmark completed", flush=True)
    finally:
        try:
            cleanup_state(state)
        except Exception as exc:
            print(f"ERROR: cleanup failed: {exc}", file=sys.stderr)
            print(f"state retained at {state_path}", file=sys.stderr)
            success = False
            result_code = 1
        if not success:
            print(f"evidence retained at {directory}", file=sys.stderr)
    return result_code


def fetch_results(load: dict, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    command(
        [
            "scp",
            *load["ssh_options"],
            f"{load['host']}:~/benchmark-results.tar.gz",
            str(target / "benchmark-results.tar.gz"),
        ],
        timeout=180,
    )
    command(
        [
            "scp",
            *load["ssh_options"],
            f"{load['host']}:~/benchmark-results.sha256",
            str(target / "benchmark-results.sha256"),
        ],
        timeout=60,
    )
    archive = target / "benchmark-results.tar.gz"
    expected = (target / "benchmark-results.sha256").read_text().split()[0]
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError("returned benchmark archive checksum mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(target, filter="data")
    print(f"collected results in {target}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--model", default="resnet50-v2-7")
    parser.add_argument(
        "--routing", choices=("hash", "least_loaded", "bounded_load"), default="hash"
    )
    parser.add_argument("--transport", choices=("binary", "json"), default="binary")
    parser.add_argument("--mode", choices=("unique", "repeated", "mixed"), default="mixed")
    parser.add_argument("--levels", type=int, nargs="*", default=[1, 4, 16])
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--accuracy-count", type=int, default=500)
    parser.add_argument("--data-count", type=int, default=500)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--deadline", type=int, default=10)
    parser.add_argument("--max-batch", type=int, default=32)
    parser.add_argument("--cache", type=int, default=1000)
    parser.add_argument("--skip-accuracy", action="store_true")
    parser.add_argument("--skip-levels", action="store_true")
    parser.add_argument("--hold-rate", type=float)
    parser.add_argument("--hold-rates", type=float, nargs="+")
    parser.add_argument("--hold-repeats", type=int, default=1)
    parser.add_argument("--hold-matrix", action="store_true")
    parser.add_argument("--admission-max", type=int, default=0)
    parser.add_argument("--hold-seconds", type=float, default=300.0)
    parser.add_argument("--hold-concurrency", type=int, default=128)
    parser.add_argument("--failover-rate", type=float)
    parser.add_argument("--failover-seconds", type=float, default=60.0)
    parser.add_argument("--failover-kill-after", type=float, default=20.0)
    parser.add_argument("--failover-kill-worker", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.workers > MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    if args.admission_max < 0:
        parser.error("--admission-max must be non-negative")
    if args.hold_repeats < 1:
        parser.error("--hold-repeats must be at least 1")
    if args.hold_rates and args.hold_rate is not None:
        parser.error("use --hold-rates or --hold-rate, not both")
    if args.hold_matrix and args.admission_max <= 0:
        parser.error("--hold-matrix requires --admission-max > 0")
    if args.dry_run:
        hourly = (args.workers + 1) * 0.085 + 0.17
        print(
            json.dumps(
                {
                    "workers": args.workers,
                    "instance_hours_estimate": round(hourly, 3),
                    "estimated_one_hour_cost": round(hourly, 3),
                    "budget": 10.0,
                },
                indent=2,
            )
        )
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
