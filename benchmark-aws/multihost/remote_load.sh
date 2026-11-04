#!/usr/bin/env bash
set -Eeuo pipefail
WORK=$HOME/benchmark-work
RESULT=$WORK/results
mkdir -p "$RESULT"
export PATH=/usr/local/bin:/usr/bin:/bin

if [[ ! -x "$WORK/venv/bin/python" ]]; then
    sudo dnf install -y python3.12 python3.12-pip >/dev/null
    python3.12 -m venv "$WORK/venv"
    "$WORK/venv/bin/pip" install --quiet --upgrade pip
    "$WORK/venv/bin/pip" install --quiet -r "$HOME/requirements-benchmark.txt"
fi

if [[ ! -f "$WORK/data/manifest.json" ]]; then
    "$WORK/venv/bin/python" "$HOME/prepare_data.py" \
        --output "$WORK/data" --count "${DATA_COUNT:-500}"
fi

vmstat 1 > "$RESULT/load-vmstat.txt" &
MONITOR=$!
trap 'kill "$MONITOR" 2>/dev/null || true' EXIT

TARGET="http://${GATEWAY_IP}:8000"
if [[ "${PHASE:-both}" == "accuracy" || "${PHASE:-both}" == "both" ]]; then
    "$WORK/venv/bin/python" "$HOME/loadgen.py" \
        --target "$TARGET" \
        --data "$WORK/data" \
        --mode unique \
        --transport binary \
        --requests "${ACCURACY_COUNT:-500}" \
        --concurrency 1 \
        --output "$RESULT/accuracy" \
        --accuracy \
        --allow-remote
fi
if [[ "${PHASE:-both}" == "load" || "${PHASE:-both}" == "both" ]]; then
    for level in ${LEVELS:-1 4 16}; do
        label="${LABEL:-level-$level}"
        touch "$RESULT/$label.started"
        args=(
            --target "$TARGET"
            --data "$WORK/data"
            --mode "${MODE:-mixed}"
            --transport "${TRANSPORT:-binary}"
            --output "$RESULT/$label"
            --allow-remote
        )
        if [[ -n "${RATE:-}" ]]; then
            args+=(--rate "$RATE" --duration "${DURATION:-300}" --concurrency "${CONCURRENCY:-128}")
        else
            args+=(--requests "${REQUESTS:-200}" --concurrency "$level")
        fi
        if [[ "${COLLECT_OUTPUT:-0}" == "1" ]]; then
            args+=(--accuracy)
        fi
        "$WORK/venv/bin/python" "$HOME/loadgen.py" "${args[@]}"
        touch "$RESULT/$label.done"
    done
fi

tar -C "$WORK" -czf "$HOME/benchmark-results.tar.gz" results
sha256sum "$HOME/benchmark-results.tar.gz" > "$HOME/benchmark-results.sha256"
