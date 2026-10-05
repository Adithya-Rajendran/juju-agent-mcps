"""Autonomous postgresql autoscaler driven by an LLM on an OpenAI-compatible endpoint.

Per tick (default 120s):
  1. Read current postgresql units from ``juju status``.
  2. Pre-compute a BREACH hint (high|low|neutral) directly from the
     cos-metrics signal's per-signal breach fractions, so the agent's
     decision doesn't depend on the lagged ``recommended_replicas``
     field. The agent still calls ``get_scale_signal`` itself for
     transparency, but the per-tick prompt tells it which side to act on.
  3. Send a fresh single-turn conversation to qwen with the
     investigative-flow doctrine + autonomous-mode preamble that
     pre-authorizes scale ops.
  4. Let qwen pick juju_add_unit (scale up by 1) or juju_remove_unit
     (scale down by 1) as appropriate. Capture transcript to JSON.

Policy bounds (postgresql min_units/max_units in ``JUJU_OPS_POLICY``, default
policy/juju-ops.yaml) are the hard gate, enforced by juju-ops. The driver
reads the same bounds to fill in the preamble and to decide when to offer a
scale-down target. Out-of-bounds requests get a structured refusal; the
agent should reply 'policy refused: <reason>' and stop.

Run from repo root:

  EVAL_BASE_URL=http://127.0.0.1:11434/v1 EVAL_MODEL=qwen3.6:35b \
  PYTHONPATH=. \
  JUJU_OPS_POLICY=$PWD/policy/juju-ops.yaml \
  python3 -m eval.auto.autoscaler --ticks=30 --interval=120 \
      --out-dir=eval/runs/autoscaler/<run_name>

The cos-metrics signal comes from the cos-metrics-mcp service, so its
policy and Prometheus URL are set on that service, not here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import httpx
import yaml

from eval.auto.mcp_client import (
    DEFAULT_ENDPOINTS,
    collect_tools,
    get_scale_signal,
)
from eval.harness import (
    HTTP_TIMEOUT_S,
    MODEL,
    EventLog,
    load_system_prompt,
    missing_env,
    run_one,
)

DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "policy" / "juju-ops.yaml"

DOCTRINE_AUTONOMOUS_PREAMBLE = """\
You are running in AUTONOMOUS MODE — there is no human in the loop this turn.
This OVERRIDES doctrine §3 for scale ops: you have standing authorization
to call juju_add_unit and juju_remove_unit with dry_run=False whenever the
BREACH hint in the user prompt says you should act. Do not wait for
confirmation; do not default dry_run=True for scale ops in this mode.

Postgresql autoscaler bounds:
  - min units = {min_units}
  - max units = {max_units}

Authoritative inputs for this tick come ONLY from the user prompt:
  - "Current postgresql units = N" — comes directly from juju, ground truth.
  - "Breach hint = high|low|neutral" — pre-computed by the driver from the
    cos-metrics signal's breach_high_fraction / breach_low_fraction. Trust
    this. Ignore signal.recommended_replicas and signal.current_replicas
    — those lag juju by one Prometheus scrape and we have observed them
    giving stale advice.
  - "Recent actions: ..." — last few non-hold ticks, oldest first. Use
    this to detect oscillation: if you scaled up last tick and BREACH
    is still high, scaling up again is fine; but if you flip-flopped
    up and down within the last 3 ticks, prefer hold.

Tick procedure (do this in order):
  1. Call get_scale_signal(app="postgresql") for transparency. Skim it,
     don't reason from its recommended_replicas field.
  2. Let CURRENT and BREACH be the values from the prompt.
  3. Decide:
     - BREACH = high AND CURRENT < {max_units} → call juju_add_unit with
       app="postgresql", count=1, dry_run=False. ONE CALL. Stop.
     - BREACH = low AND CURRENT > {min_units} → call juju_remove_unit with
       dry_run=False. The driver will tell you which unit to target
       in the prompt under "Scale-down target unit"; pass that unit
       string verbatim. ONE CALL. Stop.
     - BREACH = neutral, OR CURRENT is at a bound, OR you flip-flopped
       within the last 3 ticks → reply "hold" and stop.
  4. Reply terse: action taken + reason. One line.

Rules:
  - Never call juju_add_unit or juju_remove_unit with dry_run=True in
    this autonomous mode. Always pass dry_run=False explicitly.
  - Never call mutating tools more than once per tick.
  - If a mutating tool returns ok=false with a "max_units" / "min_units"
    reason, do NOT retry — reply "policy refused: <reason>" and stop.
  - If a mutating tool returns ok=false with any other error, reply with
    that error string verbatim and stop. Do not invent "PoC restriction"
    or "dry-run mode" explanations.
"""


def load_pg_bounds(policy_path: Path | None = None) -> tuple[int, int]:
    """Return postgresql (min_units, max_units) from the juju-ops policy."""
    if policy_path is None:
        policy_path = Path(os.environ.get("JUJU_OPS_POLICY") or DEFAULT_POLICY)
    with open(policy_path) as f:
        app = yaml.safe_load(f)["applications"]["postgresql"]
    return int(app["min_units"]), int(app["max_units"])


def per_tick_user_prompt(
    tick: int,
    current_units: int,
    breach: str,
    recent_actions: list[str],
    scale_down_target: str | None,
) -> str:
    history = "; ".join(recent_actions) if recent_actions else "none"
    target_line = (
        f"Scale-down target unit = {scale_down_target}. "
        if scale_down_target
        else ""
    )
    return (
        f"Autoscaler tick {tick}. "
        f"Current postgresql units = {current_units}. "
        f"Breach hint = {breach}. "
        f"{target_line}"
        f"Recent actions: {history}. "
        f"Run the tick procedure now."
    )


async def pg_units() -> tuple[list[str], str | None] | None:
    """Return (unit_names_sorted, primary_or_None) from ``juju status``.

    None when juju status fails or postgresql is not deployed. The primary
    is used to pick a scale-down target: we hand the agent a specific unit
    name in the prompt instead of asking it to do the arithmetic, which
    removes a known preamble-compliance miss (model picking the
    lowest-numbered unit instead of the highest).
    """
    proc = await asyncio.create_subprocess_exec(
        "juju",
        "status",
        "--format=json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        print(f"juju status failed: {err.decode()[:200]}", file=sys.stderr)
        return None
    try:
        pg = json.loads(out).get("applications", {}).get("postgresql")
        if pg is None:
            return None
        units = pg.get("units") or {}
        primary: str | None = None
        for name, body in units.items():
            msg = (body.get("workload-status") or {}).get("message") or ""
            if "Primary" in msg:
                primary = name
                break
        return sorted(units.keys()), primary
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"juju status parse failed: {e}", file=sys.stderr)
        return None


def pick_scale_down_target(units: list[str], primary: str | None) -> str | None:
    """Highest-numbered non-primary unit, or None if no candidates."""
    candidates = [u for u in units if u != primary]
    if not candidates:
        return None
    return max(candidates, key=lambda u: int(u.split("/", 1)[1]))


async def evaluate_breach(
    http: httpx.AsyncClient,
) -> tuple[str, dict | None]:
    """Pull the cos-metrics signal over MCP and reduce it to high|low|neutral.

    A signal is considered "in high breach" if its breach_high_fraction is
    1.0 (i.e. it has been over threshold_high for the entire breach
    window). Symmetric for low. The hint is "high" if any signal is in
    high breach; "low" only if all signals are in low breach; otherwise
    "neutral". This deliberately ignores signal.recommended_replicas,
    which is what the driver-computed breach hint is meant to replace.

    Routes through the cos-metrics-mcp service (:8003) like every other
    tool call, so it exercises the same MCP surface as interactive clients.
    """
    result = await get_scale_signal(http, app="postgresql")
    if not result.get("ok"):
        return "neutral", {"error": result.get("reason", "signal not ok")}
    signals = result.get("signals_evaluated") or []
    # Only consider signals that successfully evaluated. A signal with
    # ok=false (e.g. PromQL returned no series, or the metric isn't
    # scraped) should not be allowed to block all_low — discovered
    # 2026-05-07 in the demo run when transaction_rate's irate[1m]
    # came up empty and pinned breach=neutral after the load drained.
    signals = [s for s in signals if s.get("ok", True)]
    if not signals:
        return "neutral", result
    any_high = any(s.get("breach_high_fraction", 0) >= 1.0 for s in signals)
    all_low = all(s.get("breach_low_fraction", 0) >= 1.0 for s in signals)
    if any_high:
        return "high", result
    if all_low:
        return "low", result
    return "neutral", result


async def main_async(args: argparse.Namespace) -> int:
    min_units, max_units = load_pg_bounds()
    base_doctrine = load_system_prompt(disabled=False) or ""
    preamble = DOCTRINE_AUTONOMOUS_PREAMBLE.format(
        min_units=min_units, max_units=max_units
    )
    system_prompt = base_doctrine.strip() + "\n\n" + preamble.strip()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events = EventLog(out_dir / "events.jsonl")
    summary_path = out_dir / "summary.log"
    with summary_path.open("a", encoding="utf-8") as summary_fh:

        def log(msg: str) -> None:
            line = f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}"
            print(line, flush=True)
            summary_fh.write(line + "\n")
            summary_fh.flush()

        log(
            f"autoscaler starting: ticks={args.ticks} interval={args.interval}s "
            f"model={MODEL} out_dir={out_dir} bounds={min_units}..{max_units}"
        )
        log(f"system_prompt={len(system_prompt)} chars (doctrine + autonomous preamble)")

        events.write(
            event="autoscaler_start",
            ticks=args.ticks,
            interval_s=args.interval,
            model=MODEL,
            system_prompt_chars=len(system_prompt),
        )

        # Bounded ring of last-N non-hold actions, oldest first.
        recent_actions: deque[str] = deque(maxlen=5)

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
                # Discover tools from the running MCP systemd services
                # (juju-docs-mcp / juju-ops-mcp / cos-metrics-mcp). Fails
                # fast at startup if any endpoint is unreachable rather
                # than discovering it mid-tick.
                tools, fns = await collect_tools(http, DEFAULT_ENDPOINTS)
                log(
                    f"discovered {len(tools)} tools from MCP services "
                    f"({', '.join(DEFAULT_ENDPOINTS)})"
                )
                events.write(
                    event="tools_discovered",
                    count=len(tools),
                    names=sorted(fns.keys()),
                    endpoints=list(DEFAULT_ENDPOINTS),
                )
                for tick in range(args.ticks):
                    tick_start = time.monotonic()
                    status = await pg_units()
                    if status is None:
                        log(f"tick {tick:02d}: SKIP — juju status unavailable")
                        events.write(event="tick_skip", tick=tick, reason="juju_unavailable")
                    else:
                        units_now, primary = status
                        current = len(units_now)
                        breach, breach_detail = await evaluate_breach(http)
                        scale_down_target = (
                            pick_scale_down_target(units_now, primary)
                            if breach == "low" and current > min_units
                            else None
                        )
                        item = {
                            "id": f"tick_{tick:02d}",
                            "category": "autoscale",
                            "prompt": per_tick_user_prompt(
                                tick,
                                current,
                                breach,
                                list(recent_actions),
                                scale_down_target,
                            ),
                        }
                        events.write(
                            event="tick_inputs",
                            tick=tick,
                            current=current,
                            breach=breach,
                            scale_down_target=scale_down_target,
                            recent_actions=list(recent_actions),
                            breach_detail=breach_detail,
                        )
                        ck = out_dir / f"{item['id']}.json"
                        t = await run_one(
                            item,
                            tools,
                            fns,
                            http,
                            checkpoint_path=ck,
                            events=events,
                            system_prompt=system_prompt,
                        )
                        ck.write_text(json.dumps(t.__dict__, indent=2, default=str))
                        after = await pg_units()
                        new_count = len(after[0]) if after is not None else None
                        delta = (new_count - current) if (new_count is not None) else None
                        tools_used = "/".join(c["name"] for c in t.tool_calls) or "-"
                        err = f" ERR={t.error}" if t.error else ""
                        if delta and delta != 0:
                            recent_actions.append(
                                f"tick {tick:02d}: {current}->{new_count} "
                                f"(breach={breach})"
                            )
                        log(
                            f"tick {tick:02d}: units {current} -> {new_count} "
                            f"(delta={delta}) breach={breach} "
                            f"rounds={t.rounds} tools=[{tools_used}]{err}"
                        )
                        # Per-tool-call detail. Truncate args/result so a runaway
                        # tool result (e.g. full juju_status payload) doesn't drown
                        # the terminal.
                        for c in t.tool_calls:
                            args_str = json.dumps(c.get("args", {}), default=str)
                            if len(args_str) > 160:
                                args_str = args_str[:157] + "..."
                            summary = c.get("result_summary", "") or ""
                            if len(summary) > 160:
                                summary = summary[:157] + "..."
                            log(f"           tool: {c['name']}({args_str}) -> {summary}")
                        # Full reply, line by line (no 120-char truncation).
                        if t.final_reply:
                            for ln in t.final_reply.strip().split("\n"):
                                if ln.strip():
                                    log(f"           reply: {ln}")
                        # Verbose mode: dump qwen's reasoning_content blocks
                        # (one per round that had any) in the order they were
                        # produced. This is the model's chain of thought —
                        # noisy but the only window into "why did it pick this
                        # tool". Untruncated.
                        if args.verbose and t.reasoning:
                            for i, r in enumerate(t.reasoning):
                                r = (r or "").strip()
                                if not r:
                                    continue
                                for ln in r.split("\n"):
                                    if ln.strip():
                                        log(f"           reasoning[r{i + 1}]: {ln}")

                    # Sleep until next tick boundary, but don't sleep on the last.
                    if tick < args.ticks - 1:
                        elapsed = time.monotonic() - tick_start
                        sleep_s = max(1.0, args.interval - elapsed)
                        await asyncio.sleep(sleep_s)
        finally:
            events.write(event="autoscaler_end")
            events.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=30)
    ap.add_argument("--interval", type=int, default=120, help="seconds per tick")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Per tick, also print qwen's reasoning_content blocks "
        "(one per round) and any non-tool assistant text. Verbose "
        "mode produces a lot of output for a 30-tick production run; "
        "more useful for the 12-tick demo or single-tick smoke tests.",
    )
    args = ap.parse_args()
    missing = missing_env()
    if missing:
        print(missing, file=sys.stderr)
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
