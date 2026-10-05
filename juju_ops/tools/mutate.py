"""Mutation tools backed by the python-libjuju API.

Every tool runs a ``body(g)`` through :func:`juju_ops._dispatch.arun_mutation`:
the body calls ``g.check(...)`` / ``g.fail(...)`` for each validator and
policy gate, opens the model, checks that the targets exist, and only then
decides between returning a dry-run plan and executing. That order means a
dry-run plan is only ever returned for a call that would have been allowed
to run.

Provisioning vs. mutation policy asymmetry
------------------------------------------
Provisioning tools (``juju_deploy``, ``juju_add_model``, ``juju_integrate``)
bypass the ``policy.applications`` allowlist: they create new resources
that are by definition not yet listed in policy. Their safety net is
``policy.force_dry_run``.

Mutation tools (``juju_resolved``, ``juju_config_set``, ``juju_add_unit``,
``juju_remove_unit``) always refuse apps that have no entry in
``policy.applications``; scale tools are further bounded by that entry's
``min_units`` / ``max_units``.
"""

from __future__ import annotations

from typing import Any

from juju.client import client
from juju.origin import Channel

from .._dispatch import Guard, arun_mutation
from .._responses import MutationResponse
from ..juju_client import juju_controller, juju_model
from ..policy import Policy
from ..validation import (
    validate_app_name,
    validate_endpoint,
    validate_juju_ident,
    validate_num_units,
    validate_unit_name,
)


def _dry_run_plan(tool: str, plan: dict[str, Any], forced: bool) -> MutationResponse:
    """Standard dry-run response used by every mutation tool."""
    return {
        "ok": True,
        "tool": tool,
        "dry_run": True,
        "dry_run_forced_by_policy": forced,
        "plan": plan,
        "result": (
            "dry-run plan; force_dry_run policy in effect"
            if forced
            else "dry-run plan; pass dry_run=False to execute"
        ),
    }


def _executed(tool: str, plan: dict[str, Any]) -> MutationResponse:
    """Standard response for a mutation that ran against the controller."""
    return {
        "ok": True,
        "tool": tool,
        "dry_run": False,
        "dry_run_forced_by_policy": False,
        "plan": plan,
        "result": "executed",
    }


def _unit_app(unit: str) -> str:
    return unit.split("/", 1)[0]


# ---------------------------------------------------------------------------
# Tier 3 — low-risk mutations
# ---------------------------------------------------------------------------

async def juju_resolved(
    unit: str, no_retry: bool, policy: Policy
) -> MutationResponse:
    tool = "juju_resolved"
    args = {"unit": unit, "no_retry": bool(no_retry)}

    async def body(g: Guard) -> MutationResponse:
        g.check(validate_unit_name(unit))
        g.check(policy.can_mutate(_unit_app(unit)))
        plan = {"unit": unit, "no_retry": bool(no_retry)}
        async with juju_model(policy.default_model) as m:
            if unit not in m.units:
                g.fail(f"unit {unit!r} not found")
            d = policy.evaluate_dry_run(tool, False)
            if d.effective:
                return _dry_run_plan(tool, plan, d.forced_by_policy)
            await m.units[unit].resolved(retry=not no_retry)
        return _executed(tool, plan)

    return await arun_mutation(tool, args, body)


async def juju_config_set(
    app: str, key: str, value: str, dry_run: bool, policy: Policy
) -> MutationResponse:
    tool = "juju_config_set"
    args = {"app": app, "key": key, "value": value, "dry_run": bool(dry_run)}

    async def body(g: Guard) -> MutationResponse:
        g.check(validate_app_name(app))
        g.check(policy.can_set_config(app, key))
        plan = {"app": app, "key": key, "value": value}
        async with juju_model(policy.default_model) as m:
            if app not in m.applications:
                g.fail(f"application {app!r} not found")
            d = policy.evaluate_dry_run(tool, dry_run)
            if d.effective:
                return _dry_run_plan(tool, plan, d.forced_by_policy)
            await m.applications[app].set_config({key: value})
        return _executed(tool, plan)

    return await arun_mutation(tool, args, body)


# ---------------------------------------------------------------------------
# Tier 4 — scale ops
# ---------------------------------------------------------------------------

async def juju_add_unit(
    app: str, count: int, to: str | None, dry_run: bool, policy: Policy
) -> MutationResponse:
    tool = "juju_add_unit"
    args = {"app": app, "count": count, "to": to, "dry_run": bool(dry_run)}

    async def body(g: Guard) -> MutationResponse:
        g.check(validate_app_name(app))
        g.check(validate_num_units(count, allow_zero=False, name="count"))
        g.check(policy.can_mutate(app))
        async with juju_model(policy.default_model) as m:
            if app not in m.applications:
                g.fail(f"application {app!r} not found")
            a = m.applications[app]
            current = len(a.units)
            target = current + count
            g.check(policy.can_scale_to(app, target), current=current)
            plan = {"app": app, "count": count, "to": to,
                    "target": target, "current": current}
            d = policy.evaluate_dry_run(tool, dry_run)
            if d.effective:
                return _dry_run_plan(tool, plan, d.forced_by_policy)
            await a.add_units(count=count, to=to)
        return _executed(tool, plan)

    return await arun_mutation(tool, args, body)


# ---------------------------------------------------------------------------
# Tier 4 — provisioning (deploy / add_model / integrate)
# ---------------------------------------------------------------------------

async def _charmhub_origin_type(m: Any, name: str, channel: str) -> str:
    """Resolve ``ch:<name>`` on ``channel`` and return its origin type
    (``"charm"`` or ``"bundle"``), the same Charms.ResolveCharms call
    Model.deploy makes before choosing between a charm and a bundle."""
    ch = Channel.parse(channel or "stable").normalize()
    facade = client.CharmsFacade.from_connection(m.connection())
    resp = await facade.ResolveCharms(resolve=[{
        "reference": f"ch:{name}",
        "charm-origin": {
            "source": "charm-hub",
            "architecture": await m._resolve_architecture(),
            "track": ch.track,
            "risk": ch.risk,
        },
    }])
    [result] = resp.results
    if result.error:
        raise RuntimeError(f"resolving ch:{name}: {result.error.message}")
    return result.charm_origin.type_


async def juju_deploy(
    charm: str,
    app_name: str | None,
    num_units: int,
    channel: str,
    config: dict[str, Any] | None,
    to: str | None,
    dry_run: bool,
    policy: Policy,
) -> MutationResponse:
    tool = "juju_deploy"
    args = {
        "charm": charm,
        "app_name": app_name,
        "num_units": num_units,
        "channel": channel,
        "to": to,
        "dry_run": bool(dry_run),
    }

    async def body(g: Guard) -> MutationResponse:
        # Charmhub charms only: a "local:" or filesystem path would make
        # the server upload a directory from its own disk.
        name = charm.removeprefix("ch:") if isinstance(charm, str) else charm
        g.check(validate_juju_ident(name, kind="charm name"))
        target = app_name or name
        g.check(validate_app_name(target))
        g.check(validate_num_units(num_units, allow_zero=True))
        # libjuju reads config["trust"] (truthy, so even the string "false")
        # and grants the charm access to cloud credentials. Trust is an
        # operator decision, not something the model can set.
        if config and "trust" in config:
            g.fail("'trust' cannot be set via juju_deploy config")
        plan = {"charm": name, "app_name": target, "num_units": num_units,
                "channel": channel, "config": config or {}, "to": to}
        async with juju_model(policy.default_model) as m:
            if target in m.applications:
                g.fail(
                    f"application {target!r} already exists; use juju_add_unit "
                    "to scale it"
                )
            # Model.deploy would run a bundle's whole plan and ignore
            # app_name/num_units/config/to, so the plan above would not
            # describe what runs. Only single charms are deployable here.
            if await _charmhub_origin_type(m, name, channel) == "bundle":
                g.fail(f"{name!r} is a bundle; juju_deploy only deploys single charms")
            d = policy.evaluate_dry_run(tool, dry_run)
            if d.effective:
                return _dry_run_plan(tool, plan, d.forced_by_policy)
            await m.deploy(
                name,
                application_name=target,
                num_units=num_units,
                channel=channel,
                config=dict(config or {}),
                to=to,
            )
        return _executed(tool, plan)

    return await arun_mutation(tool, args, body)


async def juju_add_model(
    name: str, dry_run: bool, policy: Policy
) -> MutationResponse:
    tool = "juju_add_model"
    args = {"name": name, "dry_run": bool(dry_run)}

    async def body(g: Guard) -> MutationResponse:
        g.check(validate_juju_ident(name, kind="model name"))
        plan = {"name": name}
        async with juju_controller() as c:
            if name in await c.list_models():
                g.fail(f"model {name!r} already exists")
            d = policy.evaluate_dry_run(tool, dry_run)
            if d.effective:
                return _dry_run_plan(tool, plan, d.forced_by_policy)
            # ModelManager.CreateModel directly: Controller.add_model also
            # pushes local CLI credentials and SSH keys and returns an open
            # connection to the new model, none of which the server wants.
            # The owner is the identity this connection logged in as.
            mm = client.ModelManagerFacade.from_connection(c.connection())
            await mm.CreateModel(
                name=name,
                owner_tag=c.connection().info["user-info"]["identity"],
                cloud_tag="",
                region="",
                config={},
                credential="",
            )
        return _executed(tool, plan)

    return await arun_mutation(tool, args, body)


async def juju_integrate(
    app_a: str,
    app_b: str,
    dry_run: bool,
    policy: Policy,
) -> MutationResponse:
    tool = "juju_integrate"
    args = {"app_a": app_a, "app_b": app_b, "dry_run": bool(dry_run)}

    async def body(g: Guard) -> MutationResponse:
        g.check(validate_endpoint(app_a))
        g.check(validate_endpoint(app_b))
        base_a = app_a.split(":", 1)[0]
        base_b = app_b.split(":", 1)[0]
        if base_a == base_b:
            g.fail("cannot integrate an app with itself")
        plan = {"app_a": app_a, "app_b": app_b}
        async with juju_model(policy.default_model) as m:
            for base in (base_a, base_b):
                if base not in m.applications:
                    g.fail(f"application {base!r} not found")
            d = policy.evaluate_dry_run(tool, dry_run)
            if d.effective:
                return _dry_run_plan(tool, plan, d.forced_by_policy)
            await m.integrate(app_a, app_b)
        return _executed(tool, plan)

    return await arun_mutation(tool, args, body)


# ---------------------------------------------------------------------------
# Tier 5 — destructive
# ---------------------------------------------------------------------------

async def juju_remove_unit(
    unit: str, dry_run: bool, policy: Policy
) -> MutationResponse:
    tool = "juju_remove_unit"
    args = {"unit": unit, "dry_run": bool(dry_run)}

    async def body(g: Guard) -> MutationResponse:
        g.check(validate_unit_name(unit))
        app = _unit_app(unit)
        g.check(policy.can_mutate(app))
        async with juju_model(policy.default_model) as m:
            a = m.applications.get(app)
            if unit not in m.units or a is None:
                g.fail(f"unit {unit!r} not found")
            current = len(a.units)
            g.check(policy.can_scale_to(app, current - 1), current=current)
            plan = {"unit": unit, "app": app, "current": current}
            d = policy.evaluate_dry_run(tool, dry_run)
            if d.effective:
                return _dry_run_plan(tool, plan, d.forced_by_policy)
            await a.destroy_units(unit)
        return _executed(tool, plan)

    return await arun_mutation(tool, args, body)


__all__ = [
    "juju_add_model",
    "juju_add_unit",
    "juju_config_set",
    "juju_deploy",
    "juju_integrate",
    "juju_remove_unit",
    "juju_resolved",
]
