#!/bin/bash
# Drive pgbench against the postgresql primary to push connection_saturation
# above 0.80. Bounded by --duration so the script self-terminates; safe to
# Ctrl-C as well. Logs to eval/runs/autoscaler/<run>/pgbench.log.
#
# Env: PGHOST and PGPASSWORD (required); PGUSER, PGDATABASE (optional).

set -euo pipefail

CLIENTS="${CLIENTS:-90}"
JOBS="${JOBS:-4}"
DURATION="${DURATION:-1500}"   # default 25 min
DB="${PGDATABASE:-bench}"
HOST="${PGHOST:?need PGHOST (postgresql primary address)}"
USER="${PGUSER:-operator}"

LOG="${PGBENCH_LOG:-/tmp/pgbench.log}"

echo "$(date -uIs) pgbench warmup -i (scale=10) on $DB" | tee -a "$LOG"
PGPASSWORD="${PGPASSWORD:?need PGPASSWORD}" pgbench -i -s 10 -h "$HOST" -U "$USER" "$DB" >>"$LOG" 2>&1

echo "$(date -uIs) pgbench load: -c $CLIENTS -j $JOBS -T $DURATION" | tee -a "$LOG"
PGPASSWORD="${PGPASSWORD}" pgbench -h "$HOST" -U "$USER" \
    -c "$CLIENTS" -j "$JOBS" -T "$DURATION" \
    --report-per-command --progress=30 \
    "$DB" >>"$LOG" 2>&1
echo "$(date -uIs) pgbench load finished" | tee -a "$LOG"
