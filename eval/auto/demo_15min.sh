#!/bin/bash
# 15-minute autoscaler demo: shows one scale-up and one scale-down.
#
# Uses policy/cos-metrics-demo.yaml (breach_window=2m, threshold_high=0.5)
# so the cycle fits in 15 min instead of the 60 min the production recipe
# needs. The breach signal is evaluated by the cos-metrics-mcp user service,
# so the demo restarts that service with a runtime drop-in pointing
# COS_METRICS_POLICY at the demo recipe, and restores it on exit.
# juju-ops policy bounds are unchanged — the same hard gate as production.
#
# Required env (set before invoking):
#   EVAL_BASE_URL       — OpenAI-compatible API base (vLLM, Ollama, llama.cpp
#                         server, etc.), e.g. http://127.0.0.1:11434/v1
#   PGPASSWORD          — operator password from `juju run postgresql/0 get-password username=operator`
#   PGHOST              — postgresql primary address for pgbench
# Optional env (sensible demo defaults; override if you need to):
#   EVAL_API_KEY        (bearer token; only if the endpoint needs one)
#   EVAL_MODEL          (default: qwen3.6:35b)
#   TICKS               (default: 12)
#   INTERVAL            (default: 75 seconds)
#   DURATION            (default: 360 seconds = 6 min of pgbench)
#   CLIENTS             (default: 60)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -z "${EVAL_BASE_URL:-}" ]]; then
    echo "ERROR: EVAL_BASE_URL not set (your OpenAI-compatible API base)" >&2
    exit 2
fi
if [[ -z "${PGHOST:-}" ]]; then
    echo "ERROR: PGHOST not set (postgresql primary address for pgbench)" >&2
    exit 2
fi
if [[ -z "${PGPASSWORD:-}" ]]; then
    echo "ERROR: PGPASSWORD not set." >&2
    echo "Get it with: juju run postgresql/0 get-password username=operator" >&2
    exit 2
fi

RUN_NAME="demo_$(date -u +%Y%m%dT%H%MZ)"
OUT_DIR="$REPO_ROOT/eval/runs/autoscaler/$RUN_NAME"

# --- demo-specific overrides; production knobs live in run_autoscale_session.sh
export EVAL_MODEL="${EVAL_MODEL:-qwen3.6:35b}"
export EVAL_BASE_URL
export EVAL_HTTP_TIMEOUT_S="${EVAL_HTTP_TIMEOUT_S:-180}"
# MAX_ROUNDS=5 (was 4) so the autoscaler can produce a final reply
# even if the agent breaks the "ONE CALL per tick" rule and runs an
# extra signal+action pair — happened during the 16:41Z verbose demo
# run (tick 06: scaled 3→4→5, hit MAX_ROUNDS=4, no final reply).
# Doesn't fix the over-scale itself; that's a preamble compliance
# improvement parked for later.
export EVAL_MAX_ROUNDS="${EVAL_MAX_ROUNDS:-5}"
export EVAL_TEMPERATURE="${EVAL_TEMPERATURE:-0.3}"
DEMO_COS_POLICY="$REPO_ROOT/policy/cos-metrics-demo.yaml"
export JUJU_OPS_POLICY="${JUJU_OPS_POLICY:-$REPO_ROOT/policy/juju-ops.yaml}"
export TICKS="${TICKS:-12}"
export INTERVAL="${INTERVAL:-75}"
export DURATION="${DURATION:-360}"
export CLIENTS="${CLIENTS:-60}"

# Pre-flight checks — fail fast before mutating anything.
echo "$(date -uIs) demo pre-flight checks…"

if ! juju status postgresql --format=json 2>/dev/null | grep -q '"applications"'; then
    echo "ERROR: 'juju status postgresql' did not return a healthy model. Bootstrap first." >&2
    exit 2
fi

CURRENT_UNITS=$(juju status postgresql --format=json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['applications']['postgresql']['units']))")

if [[ "$CURRENT_UNITS" != "3" ]]; then
    cat <<EOF >&2
WARNING: postgresql has $CURRENT_UNITS units, demo expects exactly 3.
The demo will still run, but the lifecycle won't start from a clean
baseline. To reset:

  juju status postgresql --format=json | python3 -c "
import json, sys
d = json.load(sys.stdin)
units = sorted(d['applications']['postgresql']['units'].keys(),
               key=lambda u: int(u.split('/', 1)[1]), reverse=True)
for u in units[:-3]:
    print(f'juju remove-unit --no-prompt {u}')
" | sh

EOF
fi

# Point cos-metrics-mcp at the demo recipe for the length of the demo. A
# runtime drop-in lives under $XDG_RUNTIME_DIR, so even if the cleanup
# trap never runs it is gone after a reboot.
DROPIN_DIR="${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR not set; run from a user session}/systemd/user/cos-metrics-mcp.service.d"
restore_cos_metrics() {
    rm -f "$DROPIN_DIR/demo.conf"
    systemctl --user daemon-reload
    systemctl --user restart cos-metrics-mcp
    echo "$(date -uIs) cos-metrics-mcp restored to its configured policy"
}
mkdir -p "$DROPIN_DIR"
printf '[Service]\nEnvironment=COS_METRICS_POLICY=%s\n' "$DEMO_COS_POLICY" > "$DROPIN_DIR/demo.conf"
trap restore_cos_metrics EXIT
systemctl --user daemon-reload
systemctl --user restart cos-metrics-mcp
echo "$(date -uIs) cos-metrics-mcp restarted with $DEMO_COS_POLICY"

# Verify the three MCP services are up — autoscaler dispatches all
# tool calls through them, so a missing service means dead tick.
# cos-metrics-mcp was just restarted, so give it up to 30s to bind.
echo "$(date -uIs) probing MCP services on 8001/8002/8003…"
if ! "${PYTHON:-./.venv/bin/python}" -m eval.auto.mcp_client --wait 30; then
    cat <<EOF >&2
ERROR: MCP services are not responding.
The autoscaler routes all tool calls through the systemd-managed MCP
services. Start them with:

    systemctl --user start juju-docs-mcp juju-ops-mcp cos-metrics-mcp

Then re-run this demo. See deploy/systemd/README.md for install
instructions.
EOF
    exit 2
fi
echo "$(date -uIs) MCP services OK"

# Verify the model endpoint is responsive (single 30s probe).
echo "$(date -uIs) probing model endpoint…"
AUTH_HEADER=()
if [[ -n "${EVAL_API_KEY:-}" ]]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${EVAL_API_KEY}")
fi
PROBE=$(curl -s -m 30 -o /dev/null -w "%{http_code}" \
    -X POST "${EVAL_BASE_URL}/chat/completions" \
    ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${EVAL_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"OK\"}],\"stream\":false,\"max_tokens\":3}")
if [[ "$PROBE" != "200" ]]; then
    echo "ERROR: model endpoint probe returned HTTP $PROBE — model not responding. Is it loaded?" >&2
    exit 2
fi
echo "$(date -uIs) model endpoint OK"

cat <<EOF

================================================================
  Autoscaler 15-minute demo
================================================================
  Run:        $RUN_NAME
  Out dir:    $OUT_DIR
  Recipe:     $DEMO_COS_POLICY (cos-metrics-mcp drop-in)
  Bounds:     postgresql min=3 max=7 (juju-ops policy)
  Cluster:    $CURRENT_UNITS units
  Schedule:   $TICKS ticks × ${INTERVAL}s = $((TICKS*INTERVAL/60)) min total
  Load:       pgbench -c $CLIENTS for ${DURATION}s, starting after tick 1
  Watch:      tail -f $OUT_DIR/summary.log
================================================================

EOF

# Run the orchestrator with our demo env overrides (not exec, so the EXIT
# trap restores cos-metrics-mcp). Any args the user passed (e.g. --verbose)
# are forwarded through.
bash "$REPO_ROOT/eval/auto/run_autoscale_session.sh" "$RUN_NAME" "$@"
