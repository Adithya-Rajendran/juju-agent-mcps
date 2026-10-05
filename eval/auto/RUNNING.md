# Running the autoscaler

`eval/auto/autoscaler.py` lets a model (qwen3.6:35b in the reference runs)
drive a real postgresql scaling loop on a Juju controller. Each tick the
model reads the policy-vetted scale signal from cos-metrics and either holds
or makes one `juju_add_unit` / `juju_remove_unit` call through the juju-ops
policy gate. [ARCHITECTURE.md](ARCHITECTURE.md) traces how a tick reaches
the model and the cluster.

There are two ways to run it, both using the same driver:

- **Production cycle (60 min, 5m breach window).** Uses the recipe in
  `policy/cos-metrics.yaml`. One full 3→7→3 traversal under sustained
  pgbench load.
- **Demo cycle (15 min, 2m breach window, 0.5 high threshold).** Uses
  `policy/cos-metrics-demo.yaml` so one scale-up and one scale-down fit in a
  15-minute slot. Everything else is the same.

Both mutate the live postgresql application.

## What the policy gate guarantees

- The postgresql bounds are `min_units`/`max_units` in `policy/juju-ops.yaml`
  (3 and 7 in the shipped file). A call outside them gets a structured
  refusal. The preamble tells the model to quote the refusal and stop, not
  retry. The driver reads the same bounds from `JUJU_OPS_POLICY` for its
  preamble.
- Apps not declared in `policy/juju-ops.yaml` cannot be mutated.
- Provisioning and config changes (`juju_deploy`, `juju_add_model`,
  `juju_integrate`, `juju_config_set`) are in `mutations.force_dry_run`, so
  a runaway loop can only produce plans for them. `juju_add_unit` and
  `juju_remove_unit` must **not** be in that list, or the loop only ever
  prints plans.
- Read-only tools are always allowed.
- The bounds in `policy/cos-metrics.yaml` (`min_replicas`/`max_replicas`)
  must match the juju-ops ones. `tests/test_cos_metrics_policy.py` checks
  this for both cos-metrics policy files.

## Prereqs (one-time)

```bash
# 1. Juju controller with postgresql deployed at >=3 units, all active.
#    The driver runs `juju status` with the local CLI, so the CLI's current
#    model must be the one juju-ops-mcp operates on.
juju status postgresql

# 2. Optional: a constraint so new units land on a usable flavor.
juju set-constraints postgresql instance-type=<flavor>

# 3. Postgres credentials and a `bench` database for pgbench.
juju run postgresql/0 get-password username=operator
#    Export the printed password as PGPASSWORD, and the primary's address
#    as PGHOST, before each run.

# 4. The three MCP services (streamable HTTP on 127.0.0.1:8001-8003). The
#    autoscaler sends every tool call through them. Install them from
#    deploy/systemd/README.md. The Prometheus URL and recipe come from the
#    cos-metrics-mcp service (its policy's prometheus.url, or a
#    COS_PROM_URL drop-in), not from this shell.
systemctl --user status juju-docs-mcp juju-ops-mcp cos-metrics-mcp --no-pager --lines=0
.venv/bin/python -m eval.auto.mcp_client   # MCP handshake + tools/list; exit 1 if any is down

# 5. An OpenAI-compatible model endpoint (vLLM, Ollama, llama.cpp server,
#    ...) with the model loaded and warm. EVAL_BASE_URL includes the
#    version prefix, e.g. http://127.0.0.1:11434/v1 for Ollama.
curl -s -m 10 ${EVAL_API_KEY:+-H "Authorization: Bearer $EVAL_API_KEY"} \
    "$EVAL_BASE_URL/models" | grep qwen3.6
```

`pgbench` must be installed on the host running the session
(`eval/auto/pgbench_load.sh` runs `pgbench -i -s 10` and then the load).

## Production cycle (60 min)

```bash
cd /path/to/juju-agent-mcps
export EVAL_BASE_URL=http://127.0.0.1:11434/v1   # your OpenAI-compatible endpoint
# export EVAL_API_KEY=...      # only if the endpoint needs a bearer token
export PGPASSWORD=...          # from prereq 3
export PGHOST=<postgresql primary address>

TICKS=30 INTERVAL=120 DURATION=1440 CLIENTS=90 \
  bash eval/auto/run_autoscale_session.sh prod_$(date -u +%Y%m%dT%H%MZ)
```

`run_autoscale_session.sh` checks the MCP services, starts pgbench one
interval after the first tick, runs the autoscaler in the foreground and
fills in defaults for the variables in the table below.

The run takes 60 min. The cluster typically goes 3 → 7, holds at 7, then
steps back down to 3. Output goes to `eval/runs/autoscaler/<run>/`:

- `tick_NN.json`: the per-tick transcript.
- `summary.log`: tick lines, tool calls and replies.
- `events.jsonl`: one line per dispatch.
- `pgbench.log`: progress samples every 30s.
- `autoscaler.stdout.log`.

This directory is gitignored. To watch a run live:

```bash
tail -f eval/runs/autoscaler/<run>/summary.log
```

## Demo cycle (15 min)

```bash
cd /path/to/juju-agent-mcps
export EVAL_BASE_URL=http://127.0.0.1:11434/v1
# export EVAL_API_KEY=...
export PGPASSWORD=...
export PGHOST=<postgresql primary address>
bash eval/auto/demo_15min.sh
```

`demo_15min.sh` runs these steps in order:

1. Warns if postgresql is not at exactly 3 units.
2. Points `cos-metrics-mcp` at `policy/cos-metrics-demo.yaml` through a
   runtime drop-in (`$XDG_RUNTIME_DIR/systemd/user/cos-metrics-mcp.service.d/demo.conf`)
   and restarts it.
3. Probes the MCP services (waiting up to 30s for the restarted
   cos-metrics-mcp) and sends one short chat completion to the model
   endpoint.
4. Runs `run_autoscale_session.sh` with the demo defaults.

On exit, the script removes the drop-in and restarts the service with its
configured policy. Because the drop-in lives under the runtime directory,
it is also gone after a reboot. The script must run in a user session where
`XDG_RUNTIME_DIR` is set.

The demo takes about 15 min, and the cluster goes 3 → 4 (or 5) → 3. With
`breach_window=2m` and `threshold_high=0.5`, the breach is detected within
about 3 min of pgbench starting, and scale-down starts about 3 min after it
ends.

### Verbose mode

Add `--verbose` (or `-v`) to print the model's per-round
`reasoning_content` alongside the tick summary. Both scripts forward
extra arguments to the autoscaler:

```bash
bash eval/auto/demo_15min.sh --verbose
TICKS=30 INTERVAL=120 DURATION=1440 CLIENTS=90 \
  bash eval/auto/run_autoscale_session.sh prod_$(date -u +%Y%m%dT%H%MZ) --verbose
```

A verbose tick adds 5–30 lines per round. That is fine for the 12-tick demo
but noisy for a 30-tick production run. For a long run, go without it and
read the reasoning from `tick_NN.json` afterwards.

## Tunable knobs

| Env var | Production default | Demo default | What it does |
|---|---|---|---|
| `TICKS` | 30 | 12 | Ticks before the autoscaler exits |
| `INTERVAL` | 120 | 75 | Seconds per tick (must leave the model time to think) |
| `DURATION` | 1440 | 360 | pgbench load duration in seconds |
| `CLIENTS` | 90 | 60 | pgbench client count |
| `PGHOST` | required | required | postgresql primary for pgbench |
| `EVAL_BASE_URL` | required | required | OpenAI-compatible API base, including the version prefix (e.g. `/v1`) |
| `EVAL_API_KEY` | unset | unset | Bearer token, only if the endpoint needs one |
| `EVAL_MODEL` | `qwen3.6:35b` | `qwen3.6:35b` | Model name at the endpoint |
| `EVAL_MAX_ROUNDS` | 6 | 5 | Per-tick model round cap |
| `EVAL_TEMPERATURE` | 0.3 | 0.3 | Sampling temperature |
| `EVAL_HTTP_TIMEOUT_S` | 120 | 180 | httpx timeout for chat completions |
| `JUJU_OPS_POLICY` | `policy/juju-ops.yaml` | same | Where the driver reads postgresql min/max |
| `PYTHON` | `./.venv/bin/python` | same | Interpreter for the autoscaler |

The Prometheus query timeout is `COS_METRICS_TIMEOUT_S` (default 30s). It is
read by the `cos-metrics-mcp` service, so set it in a
`systemctl --user edit cos-metrics-mcp` drop-in if you need to change it.

With the demo's `INTERVAL=75` and the model taking 10–30s per active tick,
there is some headroom, but less than in production. If the model is
evicted and a tick spends about 50s on a cold load, the next tick's
effective interval shrinks.

## Resetting the cluster between runs

If a previous run ended above 3 units:

```bash
# Remove the highest-numbered units until 3 are left.
juju status postgresql --format=json \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
units = sorted(d['applications']['postgresql']['units'],
               key=lambda u: int(u.split('/', 1)[1]), reverse=True)
for u in units[:-3]:
    print(f'juju remove-unit --no-prompt {u}')
" | sh

# Wait until the cluster is back at 3 units.
until [ "$(juju status postgresql --format=json | python3 -c \
  'import json,sys; print(len(json.load(sys.stdin)["applications"]["postgresql"]["units"]))')" = "3" ]; do
    sleep 15
done
```

## What you should see

`summary.log` has one line per tick, followed by indented tool and reply
lines:

```
[03:56:43Z] tick 04: units 3 -> 4 (delta=1) breach=high rounds=3 tools=[get_scale_signal/juju_add_unit]
[03:56:43Z]            tool: get_scale_signal({"app": "postgresql"}) -> ok=True ...
[03:56:43Z]            tool: juju_add_unit({"app": "postgresql", "count": 1, "dry_run": false}) -> ok=True ...
[03:56:43Z]            reply: Added 1 unit to postgresql (connection_saturation breach). Now at 4 units.
```

The fields in each tick line are:

- `units N -> M`: the juju unit count at tick start and tick end. A
  non-zero delta means the action took effect within the tick.
- `breach`: the hint the driver passed to the model (`low`, `neutral` or
  `high`). It is computed from the signals that evaluated, using their
  `breach_high_fraction` / `breach_low_fraction` against 1.0 (the whole
  window breached). `get_scale_signal` also reports `signals_failed`. A
  failed signal is ignored, not counted as low.
- `tools=[…]`: the tools the model called this tick. A healthy tick calls
  one tool (hold) or two (signal, then action).
- `SKIP — juju status unavailable`: the driver's own `juju status` call
  failed, so no prompt was sent for that tick.

## Common failures and what they mean

- **`ERR=HTTP 504`**: the model endpoint, or a proxy in front of it,
  timed out. Usually the server is reloading the model after eviction
  or is overloaded. The
  autoscaler logs the failed tick and moves on, so one bad tick rarely
  matters. If most ticks fail, no scaling happens.
- **`hit MAX_ROUNDS (N) without final reply`**: the model used every round
  without producing a final reply. This usually means it broke the
  one-call-per-tick rule (for example, a second signal and action pair).
  Check the tick's `tool:` lines. The demo raises the cap to 5 for this
  reason.
- **`policy refused: ... max_units` / `min_units`**: the model asked for a
  count outside the bounds. This is expected near a bound, and the gate is
  doing its job.
- **Ticks keep SKIPping**: `juju status --format=json` is failing for the
  whole model. Orphaned storage after a `juju remove-unit --force` has
  caused this before. Fix it with `juju remove-storage --force pgdata/<n>`.
- **Extra units after the run**: a unit removal hung in the cloud. Check
  `juju status postgresql` and use the reset recipe above.
