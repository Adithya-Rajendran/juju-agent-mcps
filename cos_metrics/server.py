"""cos-metrics MCP server.

Loads the policy YAML once at startup, then registers the four tools. Same
glue pattern as :mod:`juju_ops.server`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import tools
from .policy import CosMetricsPolicy, load_policy

mcp = FastMCP("cos-metrics")
_POLICY: CosMetricsPolicy | None = None


def _policy() -> CosMetricsPolicy:
    """Lazy policy load so ``import`` doesn't require a YAML to exist."""
    global _POLICY
    if _POLICY is None:
        _POLICY = load_policy()
    return _POLICY


@mcp.tool()
async def get_scale_signal(app: str) -> dict[str, Any]:
    """**Use this for any "should I scale X?" question.**

    Returns a structured ``{current_replicas, recommended_replicas, delta,
    reason, signals_evaluated, signals_failed}`` recommendation in **one
    call**.

    Decision logic is server-side and based on per-app PromQL recipes
    declared in ``policy/cos-metrics.yaml``: any signal breaching its
    ``threshold_high`` for >= 50% of the ``breach_window`` recommends
    ``+step_up`` (clamped at ``max_replicas``); only when **every signal
    that evaluated** has been below ``threshold_low`` will the
    recommendation step down (clamped at ``min_replicas``). Signals that
    fail to evaluate are skipped and counted in ``signals_failed``. The
    asymmetry intentionally favors headroom over savings.

    To act on a non-zero ``delta``: scale up with
    ``juju_add_unit(app, count=delta)``; scale down with
    ``juju_remove_unit(unit)`` once per unit to remove. The LLM never
    composes PromQL — recipes are policy-controlled.

    Args:
        app: Application name as it appears in the policy
            (e.g. ``"postgresql"``, ``"api"``).
    """
    return await tools.get_scale_signal(app, _policy())


@mcp.tool()
async def cos_active_alerts(severity: str | None = None) -> dict[str, Any]:
    """List active Prometheus alerts: pending or firing.

    Each alert has a ``state``: ``"firing"`` means it has met its ``for:``
    duration; ``"pending"`` means the condition is true but has not fired
    yet. Report the state; don't call a pending alert firing.

    **Use this for any "is anything wrong / broken / in trouble / paging"
    question about the cluster.** Alerts are pre-filtered, pre-ranked,
    threshold-tuned signals from the cluster's monitoring stack — they
    surface real problems faster than reading status, configs, or logs.
    Reach for this tool *before* `juju_status` / `juju_debug_log` /
    `juju_show_application` when the user's question is about *health*
    rather than *configuration*.

    Args:
        severity: Optional filter — typically ``"critical"`` / ``"warning"``
            / ``"info"``. Matched exactly against the alert's
            ``severity`` label.
    """
    return await tools.cos_active_alerts(severity, _policy())


@mcp.tool()
async def prom_query(query: str) -> dict[str, Any]:
    """Run an instant PromQL query (escape hatch).

    **Prefer ``get_scale_signal`` for autoscale decisions** — it uses
    policy-vetted recipes. Use ``prom_query`` only when the user asks for
    a specific PromQL expression directly.

    **If the result comes back empty (`result.result` is `[]`), prefer
    telling the user the metric isn't scraped over inventing alternative
    queries.** Prometheus only knows about metrics that scrape configs
    explicitly include — guessing alternative names rarely produces
    the right answer and burns a round per attempt.
    """
    return await tools.prom_query(query, _policy())


@mcp.tool()
async def prom_query_range(
    query: str, start: str, end: str, step: str = "15s"
) -> dict[str, Any]:
    """Run a range PromQL query (escape hatch). Same caveat as ``prom_query``.

    Args:
        query: PromQL expression.
        start: Unix timestamp (string of int, e.g. ``"1714857600"``) or
            RFC3339 (e.g. ``"2026-05-06T10:00:00Z"``). **The literal
            string ``"now"`` and relative durations are NOT accepted** by
            Prometheus — pass the current Unix time explicitly.
        end: Same format as ``start``. Same constraints.
        step: Sample interval (e.g. ``"15s"``, ``"1m"``). Defaults to ``"15s"``.
    """
    return await tools.prom_query_range(query, start, end, step, _policy())


# FastMCP enables DNS-rebinding protection (a loopback-only Host allowlist)
# only when constructed with one of these hosts; see main().
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cos-metrics-mcp",
        description="cos-metrics MCP server: Prometheus queries and scale signals.",
    )
    ap.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio (default) for a client that spawns the server, or "
        "streamable-http to serve MCP at http://HOST:PORT/mcp",
    )
    ap.add_argument(
        "--host", default="127.0.0.1",
        help="bind address for streamable-http (default: %(default)s)",
    )
    ap.add_argument(
        "--port", type=int, default=8003,
        help="listen port for streamable-http (default: %(default)s)",
    )
    return ap


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint: serve over stdio or streamable HTTP."""
    args = build_parser().parse_args(argv)
    _policy()  # load before serving so a bad policy fails loudly
    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if args.host not in _LOOPBACK_HOSTS:
            # Match what FastMCP(host=...) would do: the loopback-only Host
            # allowlist set at construction would reject every remote client.
            mcp.settings.transport_security = None
            print(
                f"cos-metrics-mcp: warning: listening on {args.host}:{args.port} "
                "without authentication",
                file=sys.stderr,
            )
    mcp.run(transport=args.transport)


if __name__ == "__main__":  # pragma: no cover
    main()
