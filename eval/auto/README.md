# eval/auto

Two unattended workflows live here:

| Workflow | What it does | Docs |
|---|---|---|
| **Auto-eval loop** | Every 6h an eval host injects a reversible fault, runs the eval harness against each model, grades the replies and pushes the results. A remote routine reads them and opens improvement PRs. | This file |
| **Autoscaler** | A model drives a live postgresql scale-up/scale-down loop through the deployed MCP services. | [RUNNING.md](RUNNING.md) (how to run), [ARCHITECTURE.md](ARCHITECTURE.md) (how a tick flows) |

## Auto-eval loop

The loop is split into two panes that never talk directly. They share
this git repository.

```
┌─────────────────────────────┐         ┌─────────────────────────────┐
│  Eval host (data producer)  │         │  Remote routine (improver)  │
│  systemd timer: every 6h    │  push   │  /schedule: hourly poll     │
│    └ auto-eval.sh           │ ──────▶ │    └ improver-routine-      │
│        ├ baseline check     │         │      prompt.md              │
│        ├ pick-fault         │         │       ├ exit if no new data │
│        ├ inject + wait      │         │       ├ read recent runs    │
│        ├ per model:         │  PR     │       ├ propose changes     │
│        │   └ claude -p ...  │ ◀────── │       └ open PR             │
│        └ revert + verify    │         │                             │
└─────────────────────────────┘         └─────────────────────────────┘
```

The routine runs in Anthropic's cloud, where it has no network path to the
eval host or the controller. That is why the cluster work happens on the
eval host and only the analysis and code edits happen in the routine. The
eval host fires at 00/06/12/18 UTC. The routine wakes hourly and exits at
once unless the eval host has pushed new `eval/runs/auto_*` data since its
last entry in `IMPROVER_LOG.md`.

### Eval-host files

| File | Role |
|---|---|
| `auto-eval.sh` | One sweep: baseline, then pick-fault, inject and wait, then one `claude -p` per model, then revert and re-check the baseline. Holds a flock so sweeps never overlap. |
| `auto-eval-prompt.md` | The per-model runbook each `claude -p` follows: run the harness, grade, write `summary.md`, append to `AUTO_INDEX.md`, commit and push. |
| `helper.py` | Subcommands `baseline`, `pick-fault [--sweep N]`, `wait-seconds`, `prompts-file`, `inject`, `revert`, `grade`. Its module docstring defines the grading rubric. |
| `fault-library.yaml` | Reversible faults. `pick-fault` rotates through them once per 6-hour sweep. |
| `systemd/juju-auto-eval.{service,timer}` | User units that run `auto-eval.sh` on the 6-hour schedule. |

### Eval-host prereqs

- A Juju controller whose CLI current model is the policy's
  `default_model` (`sandbox` in `policy/juju-ops.yaml`), with **postgresql
  at 3 units**, all `active/idle`. `helper.py baseline` and the fault steps
  use the `juju` CLI, and the baseline check refuses to start a sweep if
  the model is not in that state. Trim extra units with
  `juju remove-unit postgresql/N`.
- The harness calls the juju-ops tools in-process. They connect with the
  `JUJU_*` variables if they are set (see `.env.example`), and otherwise
  with the CLI's current controller.
- The repo cloned to `~/juju-agent-mcps` (the units use
  `%h/juju-agent-mcps`), with `.venv/` set up by `uv sync --locked`.
- `claude` (Claude Code) on `PATH`, plus push access to the GitHub remote
  (a deploy key or `gh auth login`).

### Install

```bash
# 1. The model endpoint, optional API key and overrides, in a user-owned
#    0600 file. EVAL_BASE_URL is any OpenAI-compatible API base, including
#    the version prefix.
install -m 0600 /dev/null ~/.config/juju-auto-eval.env
cat > ~/.config/juju-auto-eval.env <<'EOF'
EVAL_BASE_URL=http://127.0.0.1:11434/v1
# Only if the endpoint needs a bearer token:
# EVAL_API_KEY=...
# EVAL_MODELS="gemma4:31b qwen3.6:35b"
# EVAL_MAX_ROUNDS=4
# PER_MODEL_TIMEOUT=25m
EOF

# 2. The user units.
mkdir -p ~/.config/systemd/user
cp eval/auto/systemd/juju-auto-eval.service eval/auto/systemd/juju-auto-eval.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now juju-auto-eval.timer
loginctl enable-linger "$USER"   # so the timer fires while you're logged out

# 3. Verify.
systemctl --user list-timers juju-auto-eval.timer
```

The service will not start without `~/.config/juju-auto-eval.env`, and
`auto-eval.sh` exits if `EVAL_BASE_URL` is unset. `EVAL_API_KEY` is
optional; without it no `Authorization` header is sent. If the checkout
lives somewhere other than `~/juju-agent-mcps`, override
`WorkingDirectory=` and `ExecStart=` with `systemctl --user edit
juju-auto-eval.service`.

### Operating

```bash
journalctl --user -u juju-auto-eval -f              # follow a sweep
systemctl --user start juju-auto-eval.service       # run one sweep now
tail -f ~/auto-eval.log                             # the wrapper's own log
systemctl --user disable --now juju-auto-eval.timer # pause
```

### Remote routine

The routine is created with Claude Code's `/schedule`. Its configuration
lives in Anthropic's infrastructure, not in this repo:

- **Schedule:** `0 * * * *`. Step 0 of the runbook exits when there is no
  new data, so most wakes do nothing.
- **Repo:** `https://github.com/Adithya-Rajendran/juju-agent-mcps`, with
  the GitHub App installed so the routine can push and run `gh pr create`.
- **Model:** `claude-sonnet-4-6`. **Tools:** `Bash`, `Read`, `Write`,
  `Edit`, `Glob`, `Grep`.
- **Prompt:** "cd into the juju-agent-mcps checkout, then read and follow
  the runbook at eval/auto/improver-routine-prompt.md exactly."

`improver-routine-prompt.md` is the source of truth for the routine's
behaviour. PRs land on `auto-improver/<UTC date>` branches. Rounds with
nothing to propose only append a line to `eval/runs/IMPROVER_LOG.md`. To
pause the routine, disable it at https://claude.ai/code/routines.

### What gets committed

| Path | Written by | Contents |
|---|---|---|
| `eval/runs/auto_<model-slug>_<ts>/` | eval host | One sweep for one model: `harness.log`, transcripts, `grade.json`, `summary.md` |
| `eval/runs/AUTO_INDEX.md` | eval host | One line per (model, sweep) |
| `eval/runs/needs_attention.md` | eval host | Sweeps aborted by a broken baseline, a failed inject or revert, or a failed `git add` |
| `eval/runs/IMPROVER_LOG.md` | routine | One line per improver run, including no-op rounds |

`eval/runs/` is gitignored, so the loop stages exactly these paths with
`git add -f`. Ad-hoc harness runs (`eval/runs/<run_id>/`) and autoscaler
runs (`eval/runs/autoscaler/`) stay local. If the repo is public, sweep
transcripts are public too, including unit names, addresses and log lines
from the eval cluster. Run sweeps only against a cluster whose details you
are happy to publish.

Nothing reaches `main` without a human merge, except the eval host's run data
and the routine's log line.
