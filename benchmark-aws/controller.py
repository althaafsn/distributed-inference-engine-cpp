#!/usr/bin/env python3
"""Create, monitor, collect and remove one tagged EC2 benchmark environment."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGION = "us-east-1"
AMI = "ami-0820d0c589175bc01"
INSTANCE_TYPE = "c6i.large"
TAG_KEY = "temporary-inference-benchmark"
MAX_SECONDS = 30 * 60
BASE_REVISION = "c4d8a08b72df810ffad0dce6a993cecae22494bb"
BASELINE_SOURCE_FILES = (
    "include/batch_processor.h",
    "include/engine_backend.h",
    "include/inference_engine.h",
    "src/worker_node.cpp",
    "src/inference_engine.cpp",
    "src/engine_backend.cpp",
    "src/gateway.cpp",
)
SOURCE_FILES = BASELINE_SOURCE_FILES + (
    "include/tensor_payload.h",
    "include/shutdown_signal.h",
    "include/request_validation.h",
)


class Tee:
    """Copy lifecycle messages to the terminal and the local run record."""

    def __init__(self, terminal, log):
        self.terminal = terminal
        self.log = log

    def write(self, text):
        self.terminal.write(text)
        self.log.write(text)
        self.log.flush()
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log.flush()


def command(
    args: list[str], *, timeout: int = 40, check: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    return subprocess.run(args, timeout=timeout, check=check, text=True, **kwargs)


def aws(*args: str, timeout: int = 40) -> object:
    result = command(
        ["aws", *args, "--region", REGION, "--no-cli-pager", "--output", "json"],
        timeout=timeout,
        capture_output=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def identity() -> str:
    return str(aws("sts", "get-caller-identity")["Account"])


def public_ipv4() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com/", timeout=10) as response:
        value = response.read(100).decode("ascii").strip()
    return str(ipaddress.IPv4Address(value))


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def prepare_source(directory: Path, mode: str) -> dict:
    """Snapshot only the reviewed source changes and scripts before creating AWS resources."""
    if mode not in ("baseline", "instrumented"):
        raise ValueError("unknown source mode")
    root = HERE.parent
    head = command(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True
    ).stdout.strip()
    try:
        command(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", BASE_REVISION, head],
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("pinned baseline is not an ancestor of local HEAD") from exc
    changed = command(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "-z",
            BASE_REVISION,
            "--",
            "src",
            "include",
        ],
        capture_output=True,
    ).stdout.split("\0")
    omitted = sorted(set(filter(None, changed)) - set(SOURCE_FILES))
    if omitted:
        raise RuntimeError(f"source changes outside the delivery allowlist: {', '.join(omitted)}")
    sources = {}
    for name in SOURCE_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"source file is missing or is a symlink: {name}")
        if mode == "baseline":
            try:
                content = subprocess.run(
                    ["git", "-C", str(root), "show", f"{BASE_REVISION}:{name}"],
                    check=True,
                    capture_output=True,
                    timeout=20,
                ).stdout
            except subprocess.CalledProcessError:
                # A new file in the allowlist has no baseline version. The
                # baseline source does not reference it, so it is omitted.
                continue
        else:
            content = path.read_bytes()
        if len(content) > 2_000_000:
            raise RuntimeError(f"source file exceeds size limit: {name}")
        sources[name] = hashlib.sha256(content).hexdigest()

    patch = ""
    if mode == "instrumented":
        patch = command(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--no-color",
                BASE_REVISION,
                "--",
                *SOURCE_FILES,
            ],
            capture_output=True,
        ).stdout
        if not patch or len(patch.encode("utf-8")) > 2_000_000:
            raise RuntimeError("instrumented source patch is empty or too large")
        command(
            ["git", "-C", str(root), "apply", "--check", "--reverse", "-"],
            input=patch,
            capture_output=True,
        )
        for name, expected in sources.items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"source changed while preparing the snapshot: {name}")
        if "queue_wait_us" not in (root / "include/batch_processor.h").read_text(encoding="utf-8"):
            raise RuntimeError("queue timing instrumentation is missing")
        if "batch_execution_us" not in (root / "src/worker_node.cpp").read_text(encoding="utf-8"):
            raise RuntimeError("batch timing instrumentation is missing")
        engine_source = (root / "src/inference_engine.cpp").read_text(encoding="utf-8")
        if (
            "SetIntraOpNumThreads" not in engine_source
            or "hardware_concurrency" not in engine_source
        ):
            raise RuntimeError("capacity-aware ORT thread selection is missing")
        worker_source = (root / "src/worker_node.cpp").read_text(encoding="utf-8")
        gateway_source = (root / "src/gateway.cpp").read_text(encoding="utf-8")
        if "set_tcp_nodelay(true)" not in worker_source:
            raise RuntimeError("worker TCP_NODELAY setting is missing")
        if gateway_source.count("set_tcp_nodelay(true)") < 2:
            raise RuntimeError("gateway TCP_NODELAY settings are missing")
        if "GATEWAY_PORT" not in gateway_source:
            raise RuntimeError("gateway test port override is missing")
        if "decode_float32_le" not in patch or "infer_tensor" not in patch:
            raise RuntimeError("binary tensor transport is missing")

    patch_bytes = patch.encode("utf-8")
    (directory / "source.patch").write_bytes(patch_bytes)
    scripts = {}
    for name in ("remote.sh", "load.py", "verify_source.py"):
        source = HERE / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"benchmark script is missing or is a symlink: {name}")
        content = source.read_bytes()
        if len(content) > 2_000_000:
            raise RuntimeError(f"benchmark script exceeds size limit: {name}")
        (directory / name).write_bytes(content)
        scripts[name] = hashlib.sha256(content).hexdigest()
    manifest = {
        "schema_version": 1,
        "mode": mode,
        "base_revision": BASE_REVISION,
        "local_head": head,
        "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "source_sha256": sources,
        "script_sha256": scripts,
    }
    (directory / "source-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def filters(run_id: str) -> list[str]:
    if not run_id.startswith("infer-") or len(run_id) != 22:
        raise ValueError("invalid run id")
    int(run_id[6:], 16)
    return ["--filters", f"Name=tag:{TAG_KEY},Values={run_id}"]


def tagged_instances(run_id: str) -> list[dict]:
    result = aws("ec2", "describe-instances", *filters(run_id))
    return [
        instance for reservation in result["Reservations"] for instance in reservation["Instances"]
    ]


def tagged_groups(run_id: str) -> list[dict]:
    return aws("ec2", "describe-security-groups", *filters(run_id))["SecurityGroups"]


def tagged_volumes(run_id: str) -> list[dict]:
    return aws("ec2", "describe-volumes", *filters(run_id))["Volumes"]


def resource_absent(kind: str, resource_id: str) -> bool:
    """Confirm a specific known resource is gone; propagate unrelated AWS errors."""
    parameters = {
        "volume": ("describe-volumes", "--volume-ids", "InvalidVolume.NotFound"),
        "group": ("describe-security-groups", "--group-ids", "InvalidGroup.NotFound"),
    }
    operation, id_option, missing = parameters[kind]
    try:
        aws("ec2", operation, id_option, resource_id)
    except subprocess.CalledProcessError as exc:
        if missing in (exc.stderr or ""):
            return True
        raise
    return False


def wait_for_termination(instance_ids: list[str], timeout_seconds: int = 240) -> bool:
    """Require an explicit terminated state for every instance we created."""
    if not instance_ids:
        return True
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            response = aws("ec2", "describe-instances", "--instance-ids", *instance_ids)
            instances = [
                instance
                for reservation in response["Reservations"]
                for instance in reservation["Instances"]
            ]
            terminated = {
                instance["InstanceId"]
                for instance in instances
                if instance["State"]["Name"] == "terminated"
            }
            if set(instance_ids) <= terminated:
                return True
        except subprocess.CalledProcessError as exc:
            if "InvalidInstanceID.NotFound" not in (exc.stderr or ""):
                raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(5)


def find_recorded_instance(instance_id: str, run_id: str, timeout_seconds: int = 30) -> dict:
    """Verify the recorded instance's ownership tag before cleanup."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            response = aws("ec2", "describe-instances", "--instance-ids", instance_id)
            instances = [
                instance
                for reservation in response["Reservations"]
                for instance in reservation["Instances"]
            ]
            for instance in instances:
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                if instance["InstanceId"] == instance_id and tags.get(TAG_KEY) == run_id:
                    return instance
        except subprocess.CalledProcessError as exc:
            if "InvalidInstanceID.NotFound" not in (exc.stderr or ""):
                raise
        if time.monotonic() >= deadline:
            raise RuntimeError("recorded instance ownership is unverified; refusing termination")
        time.sleep(5)


def cleanup(state: dict) -> bool:
    """Only modify resources carrying the unpredictable tag of this run."""
    if state["region"] != REGION or identity() != state["account"]:
        raise RuntimeError("account or region differs from the recorded run; refusing cleanup")
    run_id = state["run_id"]
    instances = tagged_instances(run_id)
    known_instance = state.get("instance_id")
    if known_instance and not any(item["InstanceId"] == known_instance for item in instances):
        instances.append(find_recorded_instance(known_instance, run_id))
    known_volumes = set(state.get("volume_ids", []))
    for instance in instances:
        for mapping in instance.get("BlockDeviceMappings", []):
            volume_id = mapping.get("Ebs", {}).get("VolumeId")
            if volume_id:
                known_volumes.add(volume_id)
    uncertain_launch = (
        state.get("launch_attempted") and not state.get("instance_id") and not instances
    )
    uncertain_group = (
        state.get("group_attempted") and not state.get("group_id") and not tagged_groups(run_id)
    )
    active = [
        item["InstanceId"]
        for item in instances
        if item["State"]["Name"] not in ("terminated", "shutting-down")
    ]
    if active:
        print("Terminating tagged instance(s):", ", ".join(active), flush=True)
        aws("ec2", "terminate-instances", "--instance-ids", *active)
    instance_ids = [instance["InstanceId"] for instance in instances]
    if not wait_for_termination(instance_ids):
        print("ERROR: instance termination was not confirmed", file=sys.stderr)
        return False
    known_group = state.get("group_id")
    deadline = time.monotonic() + 120
    while True:
        volumes = tagged_volumes(run_id)
        groups = tagged_groups(run_id)
        known_group_gone = not known_group or resource_absent("group", known_group)
        known_volumes_gone = all(resource_absent("volume", item) for item in known_volumes)
        if not volumes and not groups and known_group_gone and known_volumes_gone:
            break
        # Tag listings can be delayed, so retry deletion as resources become visible.
        for volume in volumes:
            if volume["State"] == "available":
                try:
                    aws("ec2", "delete-volume", "--volume-id", volume["VolumeId"])
                except subprocess.CalledProcessError as exc:
                    if "InvalidVolume.NotFound" not in (exc.stderr or ""):
                        raise
        for group in groups:
            try:
                aws("ec2", "delete-security-group", "--group-id", group["GroupId"])
            except subprocess.CalledProcessError as exc:
                if not any(
                    code in (exc.stderr or "")
                    for code in (
                        "DependencyViolation",
                        "InvalidGroup.InUse",
                        "InvalidGroup.NotFound",
                    )
                ):
                    raise
        if time.monotonic() >= deadline:
            print("ERROR: tagged or recorded groups or volumes still exist", file=sys.stderr)
            return False
        time.sleep(5)
    if uncertain_launch or uncertain_group:
        print("ERROR: creation outcome is unknown; inspect the run tag in AWS", file=sys.stderr)
        return False
    print(
        "Cleanup verified: no active tagged instance, tagged volume, or tagged security group.",
        flush=True,
    )
    return True


def preflight() -> dict:
    for executable in ("aws", "ssh", "scp", "ssh-keygen", "git"):
        if not shutil.which(executable):
            raise RuntimeError(f"missing local command: {executable}")
    account = identity()
    images = aws("ec2", "describe-images", "--image-ids", AMI)["Images"]
    if (
        len(images) != 1
        or images[0]["State"] != "available"
        or images[0]["RootDeviceName"] != "/dev/xvda"
    ):
        raise RuntimeError("the pinned AMI is unavailable or its root device differs")
    subnets = aws("ec2", "describe-subnets", "--filters", "Name=default-for-az,Values=true")
    candidates = [
        s for s in subnets["Subnets"] if s.get("MapPublicIpOnLaunch") and s["State"] == "available"
    ]
    if not candidates:
        raise RuntimeError("no default public-IP subnet is available")
    selected = sorted(candidates, key=lambda item: item["AvailabilityZone"])[0]
    addr = public_ipv4()
    print(f"Account {account}, region {REGION}, instance {INSTANCE_TYPE}, AMI {AMI}")
    print(f"Subnet {selected['SubnetId']}, SSH ingress limited to {addr}/32")
    print("Budget ceiling $10 total; workload limit 30 minutes, instance watchdog 60 minutes.")
    print("Preflight does not prove launch, termination, data transfer, or cost ceiling.")
    return {
        "account": account,
        "region": REGION,
        "subnet": selected["SubnetId"],
        "vpc": selected["VpcId"],
        "public_ip": addr,
    }


def user_data(public_key: str) -> str:
    parts = public_key.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("invalid public key")
    try:
        base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError("invalid public key encoding") from exc
    public_key = " ".join(parts[:2])
    return f"""#!/bin/bash
set -eu
if ! shutdown -h +60; then poweroff; exit 1; fi
install -d -m 700 -o ec2-user -g ec2-user /home/ec2-user/.ssh
printf '%s\\n' '{public_key}' >> /home/ec2-user/.ssh/authorized_keys
chown ec2-user:ec2-user /home/ec2-user/.ssh/authorized_keys
chmod 600 /home/ec2-user/.ssh/authorized_keys
printf 'BENCH_HOST_KEY %s\\n' "$(cat /etc/ssh/ssh_host_ed25519_key.pub)" > /dev/console
printf 'BENCH_WATCHDOG_SET\\n' > /dev/console
"""


def console_host_key(instance: str) -> str:
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            data = aws("ec2", "get-console-output", "--instance-id", instance, "--latest")
        except subprocess.CalledProcessError as exc:
            if "AccessDenied" in (exc.stderr or "") or "UnauthorizedOperation" in (
                exc.stderr or ""
            ):
                raise
            time.sleep(8)
            continue
        output = data.get("Output") or ""
        if "BENCH_HOST_KEY" not in output:
            try:
                output = base64.b64decode(output).decode("utf-8", "replace")
            except (ValueError, UnicodeError):
                output = ""
        if "BENCH_WATCHDOG_SET" in output:
            for line in output.splitlines():
                if "BENCH_HOST_KEY ssh-ed25519 " in line:
                    key = line.split("BENCH_HOST_KEY ", 1)[1].strip().split()
                    if len(key) >= 2 and key[0] == "ssh-ed25519":
                        return " ".join(key[:2])
        time.sleep(8)
    raise RuntimeError("timed shutdown and authenticated console host key not observed")


def verify_archive(archive: Path, digest_file: Path, output: Path) -> None:
    expected = digest_file.read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if expected != actual:
        raise RuntimeError("transferred archive checksum mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        required = {"results/summary.json", "results/requests.jsonl", "results/exit-status"}
        present = {member.name.removeprefix("./") for member in members if member.isfile()}
        if not required <= present:
            raise RuntimeError("benchmark archive lacks required results")
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise RuntimeError("unsafe archive member")
        tar.extractall(output, filter="data")
    if (output / "results" / "exit-status").read_text().strip() != "0":
        raise RuntimeError("remote workload failed")


def provision_instance(state: dict, directory: Path, state_path: Path, key: Path) -> None:
    """Create tagged AWS resources, recording each creation before continuing."""
    command(
        ["ssh-keygen", "-q", "-t", "ed25519", "-C", "benchmark", "-N", "", "-f", str(key)],
        capture_output=True,
    )
    public_key = key.with_suffix(".pub").read_text().strip()
    userdata = directory / "userdata.sh"
    userdata.write_text(user_data(public_key), encoding="utf-8")
    tags = [{"Key": TAG_KEY, "Value": state["run_id"]}]

    state["group_attempted"] = True
    save_state(state_path, state)
    print("Creating a temporary security group for this run.", flush=True)
    group = aws(
        "ec2",
        "create-security-group",
        "--group-name",
        state["run_id"],
        "--description",
        "Temporary inference benchmark",
        "--vpc-id",
        state["vpc"],
        "--tag-specifications",
        json.dumps([{"ResourceType": "security-group", "Tags": tags}]),
    )
    state["group_id"] = group["GroupId"]
    save_state(state_path, state)
    aws(
        "ec2",
        "authorize-security-group-ingress",
        "--group-id",
        state["group_id"],
        "--protocol",
        "tcp",
        "--port",
        "22",
        "--cidr",
        state["public_ip"] + "/32",
    )

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
    tag_specifications = [{"ResourceType": kind, "Tags": tags} for kind in ("instance", "volume")]
    state["launch_attempted"] = True
    save_state(state_path, state)
    launch = aws(
        "ec2",
        "run-instances",
        "--image-id",
        AMI,
        "--instance-type",
        INSTANCE_TYPE,
        "--subnet-id",
        state["subnet"],
        "--security-group-ids",
        state["group_id"],
        "--count",
        "1",
        "--client-token",
        state["run_id"],
        "--instance-initiated-shutdown-behavior",
        "terminate",
        "--metadata-options",
        "HttpTokens=required,HttpEndpoint=enabled",
        "--block-device-mappings",
        json.dumps(block),
        "--tag-specifications",
        json.dumps(tag_specifications),
        "--user-data",
        "file://" + str(userdata),
    )
    state["instance_id"] = launch["Instances"][0]["InstanceId"]
    save_state(state_path, state)
    print(f"Launched {state['instance_id']}; local evidence: {directory}", flush=True)


def connect_to_instance(
    state: dict, directory: Path, state_path: Path, key: Path
) -> tuple[str, list[str]]:
    """Verify the host key through EC2, record the root volume, and wait for SSH."""
    host_key = console_host_key(state["instance_id"])
    response = aws("ec2", "describe-instances", "--instance-ids", state["instance_id"])
    instance = response["Reservations"][0]["Instances"][0]
    state["volume_ids"] = [
        item["Ebs"]["VolumeId"]
        for item in instance.get("BlockDeviceMappings", [])
        if item.get("Ebs", {}).get("VolumeId")
    ]
    if not state["volume_ids"]:
        raise RuntimeError(
            "instance root volume ID was not available for later cleanup verification"
        )
    save_state(state_path, state)
    address = str(ipaddress.IPv4Address(instance["PublicIpAddress"]))
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(f"{address} {host_key}\n", encoding="utf-8")
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
    host = f"ec2-user@{address}"
    deadline = time.monotonic() + 180
    while True:
        attempt = command(
            ["ssh", *options, host, "true"], check=False, capture_output=True, timeout=15
        )
        if attempt.returncode == 0:
            return host, options
        if time.monotonic() > deadline:
            raise RuntimeError("SSH did not become ready")
        time.sleep(5)


def verify_returned_source(directory: Path) -> None:
    """Confirm the returned source record matches the local pre-launch snapshot."""
    for name in ("source.patch", "source-manifest.json"):
        if (directory / name).read_bytes() != (directory / "results" / name).read_bytes():
            raise RuntimeError(f"returned source evidence differs from local snapshot: {name}")


def execute_and_collect(host: str, options: list[str], directory: Path) -> None:
    """Stream the workload log, retrieve the archive, and verify its contents."""
    payload = ("remote.sh", "load.py", "verify_source.py", "source.patch", "source-manifest.json")
    command(
        ["scp", *options, *(str(directory / name) for name in payload), f"{host}:~/"],
        timeout=120,
    )
    print("Running workload; remote output is streamed to remote-stream.log", flush=True)
    with (directory / "remote-stream.log").open("w", encoding="utf-8") as stream:
        result = command(
            ["ssh", *options, host, "bash ~/remote.sh"],
            check=False,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=MAX_SECONDS,
        )
    for name in ("benchmark-results.tar.gz", "benchmark-results.sha256"):
        command(["scp", *options, f"{host}:~/{name}", str(directory / name)], timeout=120)
    if result.returncode:
        raise RuntimeError(
            f"remote workload returned {result.returncode}; inspect remote-stream.log"
        )
    verify_archive(
        directory / "benchmark-results.tar.gz", directory / "benchmark-results.sha256", directory
    )
    verify_returned_source(directory)


def recover_logs(host: str, options: list[str], directory: Path, success: bool) -> None:
    """Try to preserve cloud-init and partial workload logs before termination."""
    try:
        with (directory / "cloud-init.log").open("w", encoding="utf-8") as log:
            command(
                ["ssh", *options, host, "sudo cat /var/log/cloud-init-output.log"],
                check=False,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=25,
            )
        if not success:
            with (directory / "partial-transfer.log").open("w", encoding="utf-8") as log:
                command(
                    [
                        "scp",
                        *options,
                        "-r",
                        f"{host}:~/benchmark-work/results",
                        str(directory / "partial-results"),
                    ],
                    check=False,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=40,
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Partial log recovery failed: {exc}", file=sys.stderr, flush=True)


def run(source_mode: str = "instrumented") -> int:
    """Run the four stages and always attempt cleanup after AWS changes."""
    state = preflight()
    state["run_id"] = "infer-" + secrets.token_hex(8)
    directory = HERE / "runs" / state["run_id"]
    directory.mkdir(parents=True, mode=0o700)
    state_path = directory / "state.json"
    save_state(state_path, state)
    key = directory / "id_ed25519"
    success = False
    clean = False
    host = None
    options = None

    with (directory / "controller.log").open("a", encoding="utf-8") as lifecycle_log:
        with (
            contextlib.redirect_stdout(Tee(sys.stdout, lifecycle_log)),
            contextlib.redirect_stderr(Tee(sys.stderr, lifecycle_log)),
        ):
            try:
                manifest = prepare_source(directory, source_mode)
                state["source_mode"] = source_mode
                state["patch_sha256"] = manifest["patch_sha256"]
                save_state(state_path, state)
                print(f"Prepared {source_mode} source; patch SHA-256: {manifest['patch_sha256']}")
                provision_instance(state, directory, state_path, key)
                host, options = connect_to_instance(state, directory, state_path, key)
                execute_and_collect(host, options, directory)
                success = True
            except (Exception, KeyboardInterrupt) as exc:
                print(f"ERROR: benchmark did not complete: {exc}", file=sys.stderr, flush=True)
                if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                    print(
                        f"Command error: {exc.stderr.strip()[:1500]}", file=sys.stderr, flush=True
                    )
            finally:
                if host and options:
                    recover_logs(host, options, directory, success)
                try:
                    clean = cleanup(state) if state.get("group_attempted") else True
                except Exception as exc:
                    print(
                        f"ERROR: cleanup not verified: {exc}. See {state_path}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                        print(
                            f"Cleanup command error: {exc.stderr.strip()[:1500]}",
                            file=sys.stderr,
                            flush=True,
                        )
                state["cleanup_verified"] = clean
                state["benchmark_succeeded"] = success
                try:
                    save_state(state_path, state)
                finally:
                    for secret in (key, key.with_suffix(".pub"), directory / "userdata.sh"):
                        secret.unlink(missing_ok=True)
                print(f"Evidence directory: {directory}", flush=True)
    return 0 if success and clean else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("preflight")
    run_parser = sub.add_parser("run")
    run_parser.add_argument(
        "--source", choices=("baseline", "instrumented"), default="instrumented"
    )
    recovery = sub.add_parser("cleanup")
    recovery.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if args.action == "preflight":
            preflight()
            return 0
        if args.action == "cleanup":
            state = json.loads(args.state.read_text(encoding="utf-8"))
            return 0 if cleanup(state) else 1
        return run(args.source)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
