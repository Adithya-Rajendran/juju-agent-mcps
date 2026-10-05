# Architecture: how the autoscaler reaches the model and the cluster

One autoscaler tick touches two surfaces:

- the **model endpoint**, any OpenAI-compatible chat-completions API
  (`EVAL_BASE_URL`, e.g. vLLM, Ollama or llama.cpp server), which receives
  the conversation plus OpenAI-style tool declarations and returns
  `tool_calls`;
- the **MCP services** (`juju-docs-mcp` :8001, `juju-ops-mcp` :8002,
  `cos-metrics-mcp` :8003, each serving MCP over streamable HTTP at
  `/mcp`, see `deploy/systemd/`), which execute those calls.

The autoscaler is an ordinary MCP client of those services, so its calls go
through the same server code, the same policy gate and the same audit trail
as any other MCP host connected to them. For how to run it, see
[RUNNING.md](RUNNING.md).

```
eval/auto/autoscaler.py
  │  startup: collect_tools()  ── MCP tools/list ─────────►  :8001/:8002/:8003 /mcp
  │  per tick:
  │    pg_units()          ── juju status (CLI) ──────────►  controller
  │    evaluate_breach()   ── MCP tools/call get_scale_signal ►  :8003 /mcp
  │    run_one()           ── POST /chat/completions ─────►  model endpoint
  │         ◄── tool_calls ───────────────────────────────┘
  │         for each call: ── MCP tools/call <tool_name> ─►  :800x /mcp
  ▼
FastMCP server (juju_ops/server.py, cos_metrics/server.py, juju_docs/server.py)
          juju-ops:    juju_ops.tools ─► policy gate + audit (_dispatch.py)
                       ─► python-libjuju (juju_client.py) ─► controller API
          cos-metrics: cos_metrics.tools ─► Prometheus HTTP API
          juju-docs:   documentation.ubuntu.com search API
```

## 1. Tool discovery (`eval/auto/mcp_client.py`)

`collect_tools()` opens an MCP session (streamable HTTP) to each service and
calls `tools/list`. Every tool becomes an OpenAI tool spec: its name, its
docstring as the description, and its `inputSchema` as the parameters. For
each tool it also builds a callable that opens a short-lived MCP session and
sends `tools/call`; the result's `structuredContent` (or its JSON text) is
returned to the chat loop, and tool errors or transport failures come back
as `{"ok": false, "reason": ...}`. If any endpoint is unreachable the
autoscaler fails at startup, not partway through a run.

`python -m eval.auto.mcp_client [--wait SECONDS] [URL ...]` runs the same
handshake plus `tools/list` as a health check and exits 1 if any service is
down; the session scripts use it before starting.

The full surface is 18 tools: juju-docs (1), juju-ops (13) and cos-metrics (4).

## 2. The chat loop (`eval/harness.py:run_one`)

The autoscaler and the eval harness share `run_one`. It sends a system
message and a user message to `$EVAL_BASE_URL/chat/completions` with
`tools=[...]`, `tool_choice="auto"` and `EVAL_TEMPERATURE`. When the reply
contains `tool_calls`, it dispatches each one, appends the JSON result as a
`role="tool"` message and asks again. It stops when the model replies
without tool calls or after `EVAL_MAX_ROUNDS` rounds.

For the autoscaler, the system message is `eval/system_prompt.md` (the
investigative-flow doctrine) plus the autonomous-mode preamble in
`autoscaler.py`, which authorizes `juju_add_unit` / `juju_remove_unit` with
`dry_run=False`. The user message is the per-tick prompt: current unit
count, breach hint, scale-down target and recent actions.

## 3. The model endpoint

The endpoint accepts the OpenAI-style request, runs inference and returns
the message with any `tool_calls`. It knows nothing about MCP; the model
only sees an ordinary tool list. `EVAL_API_KEY` is sent as a bearer token
only when it is set.

## 4. Execution in the MCP server

The FastMCP server receives the `tools/call` request and validates the
arguments against the tool's schema. Mutating juju-ops tools
go through `juju_ops/_dispatch.py:arun_mutation`. It applies the policy
(app allowlist, `min_units`/`max_units`, config allowlist, and
`force_dry_run` via `Policy.evaluate_dry_run`), runs the libjuju call, and
writes one JSON audit record per call. Refused and failed calls are audited
too. The audit goes to stderr unless `JUJU_OPS_AUDIT_LOG` is set, so by
default it lands in `journalctl --user -u juju-ops-mcp`.

Controller credentials belong to the `juju-ops-mcp` service
(`~/.config/juju-ops-mcp.env`), and the Prometheus URL and recipe belong to
`cos-metrics-mcp`. The autoscaler's own environment does not change either.

## 5. What the driver computes itself

Two inputs come from the driver, not from the model:

- `pg_units()` runs `juju status --format=json` with the local juju CLI to
  get the unit list and the primary. That unit count is ground truth for
  the prompt, and the scale-down target is the highest-numbered unit that
  is not the primary. The CLI's current model must be the one
  `juju-ops-mcp` operates on.
- `evaluate_breach()` calls `get_scale_signal` on the cos-metrics service
  over MCP and reduces the
  signals that evaluated (`ok` not false) to `high` (any signal breached
  `threshold_high` for the whole window), `low` (all signals breached
  `threshold_low`) or `neutral`. The model is told to trust this hint over
  `recommended_replicas`, which lags because it comes from a Prometheus
  scrape.

## How the eval harness differs

`python -m eval.harness` does not use the HTTP services.
`eval/tool_specs.py:collect()`
imports the three FastMCP servers in-process, builds the same OpenAI specs
from their registered tools, and dispatches through FastMCP's `tool.run`.
That keeps the argument validation, policy gate and controller connection
the same as in production (the controller is whatever `juju_ops.juju_client`
is configured for via the `JUJU_*` variables). It is imported lazily inside
`main_async`, so importing `eval.harness` from the autoscaler does not load
the servers. Use the harness for prompt suites. Use the autoscaler when you
want to exercise the deployed services.

## Why FastMCP

Each `@mcp.tool()` definition is the single source of the tool's
description, argument schema and policy gating. MCP hosts over stdio or
HTTP, the autoscaler (via MCP `tools/list`) and the harness (via the
in-process registry) all read the same definition, so editing a docstring
in `juju_ops/server.py` updates every surface.
