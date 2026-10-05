#!/usr/bin/env bash
#
# Wrapper for one multi-model auto-eval sweep. systemd/juju-auto-eval.timer
# runs it every 6 hours; helper.py's fault rotation assumes that cadence.
#
# Logs:        /var/log/auto-eval.log  (or $HOME/auto-eval.log if /var/log not writable)
# Lock:        /tmp/auto-eval.lock     (flock so overlapping runs don't clobber state)
# Working dir: the repo this script lives in (override with REPO=...)
#
# Prereqs (one-time):
#   1. Claude Code installed and `claude` on PATH.
#   2. GitHub remote configured + push auth (deploy key or `gh auth login`).
#   3. juju controller bootstrapped, postgresql 3 units deployed and at baseline.
#   4. EVAL_BASE_URL (and EVAL_API_KEY if the endpoint needs one) in the
#      environment; the systemd unit reads them from
#      ~/.config/juju-auto-eval.env.
#   5. .venv/ at $REPO with the package installed: `uv sync` or
#      `python3 -m venv .venv && .venv/bin/pip install -e .`.
#
# Orchestration split:
#   - This wrapper handles the cluster-state side (baseline/inject/revert)
#     and loops over models. The cluster only sees one inject + one revert
#     per sweep regardless of how many models are evaluated.
#   - The per-model runbook (auto-eval-prompt.md) handles only what is
#     actually per-model: run the harness, grade, summarize, push.
#
# Why this split:
#   - Inject/revert are stateful and must run exactly once per fault per sweep.
#     Doing this in bash (deterministic, single point of control) is safer
#     than asking each Claude invocation to coordinate with siblings.
#   - The runbook becomes shorter and easier for an autonomous agent to
#     execute correctly.

set -uo pipefail

# ---- config -----------------------------------------------------------------
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
PROMPT=$REPO/eval/auto/auto-eval-prompt.md
LOG=${AUTO_EVAL_LOG:-/var/log/auto-eval.log}
[ -w "$(dirname "$LOG")" ] || LOG=$HOME/auto-eval.log

# Models to eval each sweep. Override via:
#   EVAL_MODELS="gemma4:31b qwen3.6:35b" /path/to/auto-eval.sh
MODELS_STR="${EVAL_MODELS:-gemma4:31b qwen3.6:35b}"
read -r -a MODELS <<< "$MODELS_STR"

: "${EVAL_BASE_URL:?set it in ~/.config/juju-auto-eval.env}"
export EVAL_BASE_URL
# Optional: only needed when the endpoint requires a bearer token.
if [ -n "${EVAL_API_KEY:-}" ]; then export EVAL_API_KEY; fi
export EVAL_MAX_ROUNDS="${EVAL_MAX_ROUNDS:-4}"

# Per-model timeout. The unit's TimeoutStartSec=65min caps the whole sweep
# (2 models x 25m plus inject/wait/revert); raise it with more models.
PER_MODEL_TIMEOUT="${PER_MODEL_TIMEOUT:-25m}"

# systemd has a minimal PATH; add snap-installed juju and a user-local claude
export PATH="/snap/bin:$HOME/.local/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}"

# ---- lock against overlapping runs ------------------------------------------
exec 9>/tmp/auto-eval.lock
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] prior run still in progress — skipping" >> "$LOG"
  exit 0
fi

cd "$REPO" || { echo "[$(date -u +%FT%TZ)] cd $REPO failed" >> "$LOG"; exit 0; }

# Shared timestamp for this whole sweep — one inject, one revert, N model runs
export AUTO_EVAL_TS=$(date -u +%Y%m%dT%H%MZ)

PY=$REPO/.venv/bin/python3
[ -x "$PY" ] || PY=python3   # last-resort fallback if venv is missing

log() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }
needs_attention() {
  local msg=$1
  local f=$REPO/eval/runs/needs_attention.md
  [ -f "$f" ] || { mkdir -p "$REPO/eval/runs"; echo "| Timestamp | Detail |" > "$f"; echo "|-----------|--------|" >> "$f"; }
  echo "| $(date -u +%FT%TZ) | $msg |" >> "$f"
}

{
  echo "============================================================"
  echo "[$(date -u +%FT%TZ)] starting auto-eval sweep ts=$AUTO_EVAL_TS  models: ${MODELS[*]}"
  echo "============================================================"
} >> "$LOG"

# ---- step 1: pull latest ----------------------------------------------------
git pull --ff-only origin main >> "$LOG" 2>&1 || log "git pull failed (ok if no remote)"

# ---- step 2: baseline check -------------------------------------------------
if ! "$PY" eval/auto/helper.py baseline >> "$LOG" 2>&1; then
  log "baseline check failed — recording and exiting"
  needs_attention "sweep $AUTO_EVAL_TS aborted: baseline check failed (no inject attempted)"
  git add -f eval/runs/needs_attention.md >> "$LOG" 2>&1 || log "git add failed"
  git -c user.email=auto-eval@localhost -c user.name=auto-eval \
      commit -m "auto-eval $AUTO_EVAL_TS: baseline_fail" >> "$LOG" 2>&1 || true
  git push origin main >> "$LOG" 2>&1 || true
  exit 0
fi

# ---- step 3: pick fault -----------------------------------------------------
export AUTO_EVAL_FAULT_ID
AUTO_EVAL_FAULT_ID=$("$PY" eval/auto/helper.py pick-fault 2>>"$LOG")
if [ -z "$AUTO_EVAL_FAULT_ID" ]; then
  log "pick-fault returned nothing — aborting"
  needs_attention "sweep $AUTO_EVAL_TS: pick-fault failed"
  exit 0
fi
log "fault selected: $AUTO_EVAL_FAULT_ID"

# ---- step 4: inject + wait --------------------------------------------------
INJECT_LOG=/tmp/auto-eval-inject-${AUTO_EVAL_TS}.log
if ! "$PY" eval/auto/helper.py inject "$AUTO_EVAL_FAULT_ID" 2>&1 | tee "$INJECT_LOG" >> "$LOG"; then
  log "inject failed — attempting revert + exiting"
  needs_attention "sweep $AUTO_EVAL_TS: inject failed for $AUTO_EVAL_FAULT_ID"
  "$PY" eval/auto/helper.py revert "$AUTO_EVAL_FAULT_ID" >> "$LOG" 2>&1 || true
  exit 0
fi

WAIT=$("$PY" eval/auto/helper.py wait-seconds "$AUTO_EVAL_FAULT_ID" 2>>"$LOG")
log "waiting ${WAIT}s for fault to manifest"
sleep "${WAIT:-60}"

# ---- step 5: per-model loop -------------------------------------------------
for MODEL in "${MODELS[@]}"; do
  {
    echo "------------------------------------------------------------"
    echo "[$(date -u +%FT%TZ)] model=$MODEL  start  fault=$AUTO_EVAL_FAULT_ID"
    echo "------------------------------------------------------------"
  } >> "$LOG"

  EVAL_MODEL="$MODEL" \
  AUTO_EVAL_TS="$AUTO_EVAL_TS" \
  AUTO_EVAL_FAULT_ID="$AUTO_EVAL_FAULT_ID" \
  timeout "$PER_MODEL_TIMEOUT" claude -p "$(cat "$PROMPT")" \
    --permission-mode=acceptEdits \
    --output-format=stream-json \
    >> "$LOG" 2>&1
  RC=$?
  log "model=$MODEL  exit rc=$RC"
done

# ---- step 6: revert + re-verify baseline ------------------------------------
if ! "$PY" eval/auto/helper.py revert "$AUTO_EVAL_FAULT_ID" >> "$LOG" 2>&1; then
  log "revert returned non-zero (may be ok for some faults; verifying baseline)"
fi
if ! "$PY" eval/auto/helper.py baseline >> "$LOG" 2>&1; then
  log "BASELINE NOT RESTORED after revert — flagging"
  needs_attention "sweep $AUTO_EVAL_TS: baseline NOT restored after revert of $AUTO_EVAL_FAULT_ID"
  git add -f eval/runs/needs_attention.md >> "$LOG" 2>&1 || log "git add failed"
  git -c user.email=auto-eval@localhost -c user.name=auto-eval \
      commit -m "auto-eval $AUTO_EVAL_TS: revert_baseline_fail" >> "$LOG" 2>&1 || true
  git push origin main >> "$LOG" 2>&1 || true
fi

log "sweep $AUTO_EVAL_TS done"
exit 0    # always exit 0 so a transient failure isn't retried
