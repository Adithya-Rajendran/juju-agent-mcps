"""juju-ops MCP server.

Loads the policy YAML once at startup, then registers every tool. Each
tool dispatches into :mod:`juju_ops.tools`, which talks to a Juju
controller through the python-libjuju API (:mod:`juju_ops.juju_client`).
Controller / model selection comes from environment variables — see
``.env.example`` and :mod:`juju_ops.juju_client` for the resolution
order.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import tools
from .policy import Policy, load_policy

mcp = FastMCP("juju-ops")
_POLICY: Policy | None = None


def _policy() -> Policy:
    """Lazy policy load so ``import`` doesn't require a YAML to exist."""
    global _POLICY
    if _POLICY is None:
        _POLICY = load_policy()
    return _POLICY


# ---------- Tier 1 ----------

@mcp.tool()
async def juju_status(model: str | None = None) -> dict[str, Any]:
    """Return the full ``juju status`` payload for a model.

    Use **only** when the user wants an overview of the whole model. The
    response is large (every application, unit, machine, and relation).

    For targeted questions, prefer the narrower tools:

    - "why is unit X broken/stuck/blocked?" -> ``diagnose_unit``
    - "what's the state of unit X?" -> ``juju_show_unit``
    - "tell me about application X" -> ``juju_show_application``
    - "list machines" -> ``juju_machines``

    Args:
        model: Juju model name (e.g. ``"sandbox"``). **Not** an
            application name. Defaults to the policy's default_model.
    """
    return await tools.juju_status(model, _policy())


@mcp.tool()
async def juju_show_unit(unit: str) -> dict[str, Any]:
    """Return detailed status for a single unit (e.g. ``postgresql/0``).

    Prefer this over ``juju_status`` when the user asks about one unit
    by name. For "why is X stuck/broken" questions, use ``diagnose_unit``.
    """
    return await tools.juju_show_unit(unit, _policy())


@mcp.tool()
async def juju_show_application(app: str) -> dict[str, Any]:
    """Return detailed status for one application (e.g. ``postgresql``).

    Prefer this over ``juju_status`` when the user asks about a single
    application by name.
    """
    return await tools.juju_show_application(app, _policy())


@mcp.tool()
async def juju_machines(model: str | None = None) -> dict[str, Any]:
    """Return the machines section of ``juju status`` for a model."""
    return await tools.juju_machines(model, _policy())


@mcp.tool()
async def juju_debug_log(
    model: str | None = None, lines: int = 100, level: str = "WARNING"
) -> dict[str, Any]:
    """Return the most recent ``juju debug-log`` entries for a model.

    Returns ``{ok: false, reason}`` if the log stream can't be read.

    Args:
        model: Model name. Defaults to the policy's default_model.
        lines: How many of the most recent matching entries to return
            (1..10000). Default 100.
        level: Minimum level: TRACE, DEBUG, INFO, WARNING or ERROR.
            Default WARNING.
    """
    return await tools.juju_debug_log(model, lines, level, _policy())


# ---------- Tier 2 ----------

@mcp.tool()
async def diagnose_unit(unit: str) -> dict[str, Any]:
    """**Use this for any "why is X broken/stuck/unhealthy/blocked"
    question about a single unit.**

    Returns ``{category, evidence, suggested_action}`` in **one call**.
    ``category`` is one of: ``stuck_hook | blocked_config |
    machine_down | healthy | unknown``. ``evidence.recent_log`` holds the
    unit's latest log lines; ``evidence.log_error`` is set if they could
    not be read.

    You do **not** need to call ``juju_status``, ``juju_show_unit``, or
    ``juju_debug_log`` first - this tool already gathers and synthesizes
    them server-side.

    Args:
        unit: Unit name like ``"postgresql/0"`` or ``"api/1"``.
    """
    return await tools.diagnose_unit(unit, _policy())


# ---------- Tier 3 ----------

@mcp.tool()
async def juju_resolved(unit: str, no_retry: bool = False) -> dict[str, Any]:
    """Clear a unit's error state.

    Call directly on a unit reported as ``stuck_hook`` by ``diagnose_unit``.
    You do **not** need ``juju_status`` first. Policy may force dry-run; check ``dry_run_forced_by_policy`` in the result.

    Args:
        unit: Unit name (e.g. ``"api/0"``).
        no_retry: If True, clear the error without rerunning the hook.
    """
    return await tools.juju_resolved(unit, no_retry, _policy())


@mcp.tool()
async def juju_config_set(
    app: str, key: str, value: str, dry_run: bool = True
) -> dict[str, Any]:
    """Set a single application config key (subject to a per-app allowlist).

    You do **not** need to call ``juju_status`` or
    ``juju_show_application`` first - this tool validates server-side.
    Policy may force dry-run; check ``dry_run_forced_by_policy`` in the result.

    Args:
        app: Application name (e.g. ``"postgresql"``).
        key: Config key. Must be in the per-app allowlist.
        value: New value, always passed as a string.
        dry_run: If True (default), return the plan without applying it.
    """
    return await tools.juju_config_set(app, key, value, dry_run, _policy())


# ---------- Tier 4 - scale ops ----------

@mcp.tool()
async def juju_add_unit(
    app: str, count: int = 1, to: str | None = None, dry_run: bool = True
) -> dict[str, Any]:
    """Add ``count`` units to an existing ``app`` (scale up).

    You do **not** need ``juju_status`` first - this tool reads the current
    unit count and refuses any request that would exceed the policy's
    ``max_units``. To scale down, call ``juju_remove_unit`` once per unit.
    Policy may force dry-run; check ``dry_run_forced_by_policy`` in the result.

    Args:
        app: Application name.
        count: Units to add (>= 1).
        to: Optional placement directive (e.g. ``"lxd:0"``).
        dry_run: If True (default), return the plan without adding units.
    """
    return await tools.juju_add_unit(app, count, to, dry_run, _policy())


# ---------- Tier 4 - provisioning ----------

@mcp.tool()
async def juju_deploy(
    charm: str,
    app_name: str | None = None,
    num_units: int = 1,
    channel: str = "stable",
    config: dict[str, Any] | None = None,
    to: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Deploy a single Charmhub charm into the model.

    Use this for "deploy X", "install X", "add X to my model" prompts.
    Refuses if the app_name already exists (use ``juju_add_unit`` to
    grow an existing app) and refuses bundles. Policy may force dry-run; check ``dry_run_forced_by_policy`` in the result.

    Args:
        charm: Charm name as on Charmhub (e.g. ``"postgresql"``).
        app_name: Application name in the model. Defaults to charm name.
        num_units: Defaults to 1. Use 0 for subordinate charms.
        channel: Channel like ``"stable"``, ``"edge"``, or ``"14/stable"``.
        config: Initial config overrides (a ``trust`` key is refused).
        to: Optional placement directive.
        dry_run: If True (default), return the plan without deploying.
    """
    return await tools.juju_deploy(
        charm, app_name, num_units, channel, config, to, dry_run, _policy()
    )


@mcp.tool()
async def juju_add_model(name: str, dry_run: bool = True) -> dict[str, Any]:
    """Create a new Juju model on the current controller.

    Use for "create a model called X" / "spin up a new workspace".
    Refuses if the model already exists. Policy may force dry-run; check ``dry_run_forced_by_policy`` in the result.

    Args:
        name: Model name (lowercase alphanumeric and hyphens).
        dry_run: If True (default), return the plan without creating it.
    """
    return await tools.juju_add_model(name, dry_run, _policy())


@mcp.tool()
async def juju_integrate(
    app_a: str, app_b: str, dry_run: bool = True
) -> dict[str, Any]:
    """Integrate (relate) two applications.

    Use for "relate X to Y", "integrate X with Y", "connect X to Y".
    Juju 3 calls this ``integrate``; older docs say ``add-relation``.
    Both endpoints can be ``app`` or ``app:endpoint``, and both apps must
    already exist. Policy may force dry-run; check ``dry_run_forced_by_policy`` in the result.

    Args:
        app_a: First endpoint (``"postgresql"`` or ``"postgresql:db"``).
        app_b: Second endpoint.
        dry_run: If True (default), return the plan without integrating.
    """
    return await tools.juju_integrate(app_a, app_b, dry_run, _policy())


# ---------- Tier 5 ----------

@mcp.tool()
async def juju_remove_unit(unit: str, dry_run: bool = True) -> dict[str, Any]:
    """Remove a single unit (scale down by one).

    Validates the unit exists and refuses any removal that would drop the
    app below the policy's ``min_units``. To remove several units, call
    this once per unit. Policy may force dry-run; check ``dry_run_forced_by_policy`` in the result.

    Args:
        unit: Unit name (e.g. ``"postgresql/2"``).
        dry_run: If True (default), return the plan without removing it.
    """
    return await tools.juju_remove_unit(unit, dry_run, _policy())


# FastMCP enables DNS-rebinding protection (a loopback-only Host allowlist)
# only when constructed with one of these hosts; see main().
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="juju-ops-mcp",
        description="juju-ops MCP server: policy-bounded Juju operations.",
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
        "--port", type=int, default=8002,
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
                f"juju-ops-mcp: warning: listening on {args.host}:{args.port} "
                "without authentication",
                file=sys.stderr,
            )
    mcp.run(transport=args.transport)


if __name__ == "__main__":  # pragma: no cover
    main()
