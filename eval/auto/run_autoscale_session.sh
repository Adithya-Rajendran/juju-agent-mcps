#!/bin/bash
# Orchestrate a 60-min autoscaler session: run the LLM-driven autoscaler in
# the foreground (tee'd to a log), launch pgbench in the background once the
# first tick has landed, let the load run for ~24 minutes, and let the
# autoscaler observe the up-then-down cycle.
#
# Required env (set before invoking):
#   EVAL_BASE_URL (OpenAI-compatible API base), PGPASSWORD, PGHOST
#   (postgresql primary address for pgbench)
# Optional: EVAL_API_KEY (only if the endpoint needs one), EVAL_MODEL
# (default qwen3.6:35b), TICKS (default 30),
# INTERVAL (default 120), DURATION (pgbench load seconds, default 1440),
# CLIENTS (default 90), JUJU_OPS_POLICY (autoscaler bounds).
#
# The cos-metrics signal comes from the cos-metrics-mcp service; set its
# policy and Prometheus URL on that service (see demo_15min.sh for a
# temporary override).

set -euo pipefail

cd "$(dirname "$0")/../.."  # repo root

RUN_NAME="${1:-$(date -u +%Y%m%dT%H%MZ)}"
shift || true
# Anything left in $@ is forwarded to the autoscaler — `--verbose`, `-v`,
# or any future passthrough flag.
OUT_DIR="eval/runs/autoscaler/$RUN_NAME"
mkdir -p "$OUT_DIR"

TICKS="${TICKS:-30}"
INTERVAL="${INTERVAL:-120}"
DURATION="${DURATION:-1440}"
CLIENTS="${CLIENTS:-90}"

export EVAL_BASE_URL="${EVAL_BASE_URL:?need EVAL_BASE_URL (OpenAI-compatible API base, e.g. http://127.0.0.1:11434/v1)}"
: "${PGPASSWORD:?need PGPASSWORD}"
export PGHOST="${PGHOST:?need PGHOST (postgresql primary address)}"
export EVAL_MODEL="${EVAL_MODEL:-qwen3.6:35b}"
export PYTHONPATH="${PYTHONPATH:-.}"
export EVAL_MAX_ROUNDS="${EVAL_MAX_ROUNDS:-6}"
export JUJU_OPS_POLICY="${JUJU_OPS_POLICY:-$PWD/policy/juju-ops.yaml}"
export EVAL_TEMPERATURE="${EVAL_TEMPERATURE:-0.3}"

PYTHON="${PYTHON:-./.venv/bin/python}"

echo "$(date -uIs) run_autoscale_session: out_dir=$OUT_DIR ticks=$TICKS interval=${INTERVAL}s pgbench_duration=${DURATION}s"

# Verify the MCP services are up before launching the autoscaler. The
# autoscaler dispatches every tool call through them; if one is down
# tool calls return transport errors rather than reach the cluster.
# The check does a real MCP handshake + tools/list on each endpoint.
if ! "$PYTHON" -m eval.auto.mcp_client; then
    echo "$(date -uIs) ERROR: MCP services not responding." >&2
    echo "Start with: systemctl --user start juju-docs-mcp juju-ops-mcp cos-metrics-mcp" >&2
    exit 2
fi
echo "$(date -uIs) MCP services OK on :8001 :8002 :8003"

# pgbench launcher — backgrounded subshell that sleeps until the first tick has
# landed, then runs pgbench. Output goes to a log so it doesn't tangle with the
# autoscaler's per-tick prints in the foreground.
SLEEP_BEFORE_PGBENCH="$((INTERVAL + 10))"
echo "$(date -uIs) pgbench scheduled in ${SLEEP_BEFORE_PGBENCH}s (clients=$CLIENTS, duration=${DURATION}s)"
(
    sleep "$SLEEP_BEFORE_PGBENCH"
    echo "$(date -uIs) starting pgbench" >> "$OUT_DIR/pgbench.stdout.log"
    PGBENCH_LOG="$OUT_DIR/pgbench.log" \
    PGUSER=operator PGDATABASE=bench \
    DURATION="$DURATION" CLIENTS="$CLIENTS" JOBS=4 \
        eval/auto/pgbench_load.sh >> "$OUT_DIR/pgbench.stdout.log" 2>&1
    echo "$(date -uIs) pgbench finished" >> "$OUT_DIR/pgbench.stdout.log"
) &
PGB_LAUNCHER_PID=$!
echo "$PGB_LAUNCHER_PID" > "$OUT_DIR/pgbench_launcher.pid"

# Autoscaler in foreground, tee'd to log. Every tick prints to the user's
# terminal AND lands in autoscaler.stdout.log for offline inspection.
# Passthrough args (e.g. --verbose) come from $@.
"$PYTHON" -m eval.auto.autoscaler \
    --ticks="$TICKS" --interval="$INTERVAL" --out-dir="$OUT_DIR" \
    "$@" 2>&1 | tee "$OUT_DIR/autoscaler.stdout.log"
EXIT="${PIPESTATUS[0]}"

echo "$(date -uIs) autoscaler exit=$EXIT"

# Reap the pgbench launcher if it's still going.
if kill -0 "$PGB_LAUNCHER_PID" 2>/dev/null; then
    echo "$(date -uIs) pgbench launcher still alive PID=$PGB_LAUNCHER_PID (load self-terminates on DURATION)"
fi

echo "$(date -uIs) session done; out_dir=$OUT_DIR"
exit "$EXIT"
