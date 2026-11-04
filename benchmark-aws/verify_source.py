#!/usr/bin/env python3
"""Check the small, allowlisted benchmark source snapshot before it is used."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

BASE_REVISION = "c4d8a08b72df810ffad0dce6a993cecae22494bb"
BASELINE_SOURCE_FILES = {
    "include/batch_processor.h",
    "include/engine_backend.h",
    "include/inference_engine.h",
    "src/worker_node.cpp",
    "src/inference_engine.cpp",
    "src/engine_backend.cpp",
    "src/gateway.cpp",
}
SOURCE_FILES = BASELINE_SOURCE_FILES | {
    "include/tensor_payload.h",
    "include/shutdown_signal.h",
    "include/request_validation.h",
    "include/adaptive_admission.h",
    "include/gateway_metrics.h",
}
SCRIPT_FILES = {"remote.sh", "load.py", "verify_source.py"}


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("source manifest is missing or is a symlink")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported source manifest")
    head = manifest.get("local_head")
    if (
        manifest.get("base_revision") != BASE_REVISION
        or not isinstance(head, str)
        or not re.fullmatch(r"[0-9a-f]{40}", head)
    ):
        raise ValueError("source manifest does not match the expected upstream revision")
    if manifest.get("mode") not in ("baseline", "instrumented"):
        raise ValueError("invalid source mode")
    sources = manifest.get("source_sha256")
    scripts = manifest.get("script_sha256")
    expected = BASELINE_SOURCE_FILES if manifest["mode"] == "baseline" else SOURCE_FILES
    if not isinstance(sources, dict) or set(sources) != expected:
        raise ValueError("source manifest must contain exactly the allowed source files")
    if not isinstance(scripts, dict) or set(scripts) != SCRIPT_FILES:
        raise ValueError("source manifest must contain exactly the benchmark scripts")
    return manifest


def verify_payload(directory: Path, manifest: dict) -> None:
    patch = directory / "source.patch"
    if digest(patch) != manifest.get("patch_sha256"):
        raise ValueError("source patch checksum mismatch")
    size = patch.stat().st_size
    if (manifest["mode"] == "baseline" and size != 0) or (
        manifest["mode"] == "instrumented" and not 0 < size <= 2_000_000
    ):
        raise ValueError("source patch does not match its declared mode")
    for name, expected in manifest["script_sha256"].items():
        if digest(directory / name) != expected:
            raise ValueError(f"benchmark script checksum mismatch: {name}")


def verify_source(directory: Path, manifest: dict) -> None:
    for name, expected in manifest["source_sha256"].items():
        if digest(directory / name) != expected:
            raise ValueError(f"source checksum mismatch: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("payload", "source"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        manifest = read_manifest(args.manifest)
        if args.kind == "payload":
            verify_payload(args.directory, manifest)
        else:
            verify_source(args.directory, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"source verification failed: {exc}\n")
    print(f"Verified {args.kind}: mode={manifest['mode']} base={manifest['base_revision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
