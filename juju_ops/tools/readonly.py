"""Tier 1+2 read-only tools backed by the python-libjuju API.

Each tool opens a fresh Model connection via
:func:`juju_ops.juju_client.juju_model`, reads what it needs, and returns
a JSON-serializable dict. The categorizer in :func:`_categorize` is pure
Python — it consumes the normalized unit/machine dict produced by
:func:`_unit_to_dict` / :func:`_machine_to_dict`.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import ssl
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar
from urllib.parse import urlencode

import websockets

from .._responses import (
    ApplicationResponse,
    DiagnosisResponse,
    LogResponse,
    MachinesResponse,
    RefusalResponse,
    StatusResponse,
    UnitResponse,
)
from ..juju_client import juju_model
from ..policy import Policy

# The levels Juju's debug-log endpoint accepts (it rejects anything above
# ERROR); python-libjuju's client-side filter uses the same list.
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR")

# Seconds to wait for each debug-log frame before treating the stream as done.
_LOG_FRAME_TIMEOUT_S = 5


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _api_errors_as_refusal(
    fn: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R | RefusalResponse]]:
    """Turn a failed controller call (unreachable controller, unknown model,
    libjuju error) into ``{ok: False, reason}``, the same shape mutations use
    via :func:`juju_ops._dispatch.arun_mutation`, so every tool result has
    ``ok``."""

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R | RefusalResponse:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            return {"ok": False, "reason": f"juju API error: {type(e).__name__}: {e}"}

    return wrapper


def _safe_data(entity: Any) -> dict[str, Any]:
    """Return the raw delta dict that backs a python-libjuju ModelEntity."""
    data = getattr(entity, "safe_data", None)
    if isinstance(data, dict):
        return data
    return {}


def _unit_to_dict(unit: Any) -> dict[str, Any]:
    """Project a juju.unit.Unit into our wire format."""
    data = _safe_data(unit)
    workload_status = data.get("workload-status") or {}
    agent_status = data.get("agent-status") or {}
    return {
        "workload-status": {
            "current": workload_status.get("current"),
            "message": workload_status.get("message") or "",
        },
        "juju-status": {
            "current": agent_status.get("current"),
            "message": agent_status.get("message") or "",
        },
        "machine": data.get("machine-id"),
        "leader": bool(data.get("leader", False)),
        "public-address": data.get("public-address") or None,
        "application": data.get("application") or unit.entity_id.split("/", 1)[0],
    }


def _machine_to_dict(machine: Any) -> dict[str, Any]:
    data = _safe_data(machine)
    agent = data.get("agent-status") or {}
    instance = data.get("instance-status") or {}
    return {
        "id": data.get("id") or getattr(machine, "entity_id", None),
        "juju-status": {
            "current": agent.get("current"),
            "message": agent.get("message") or "",
        },
        "machine-status": {
            "current": instance.get("current"),
            "message": instance.get("message") or "",
        },
        "dns-name": next(
            (a.get("value") for a in data.get("addresses") or []),
            None,
        ),
        "hostname": data.get("hostname"),
        "series": data.get("series"),
    }


@_api_errors_as_refusal
async def juju_status(
    model: str | None, policy: Policy
) -> StatusResponse | RefusalResponse:
    gate = policy.model_allowed(model)
    if not gate.ok:
        return {"ok": False, "reason": gate.reason}
    async with juju_model(model or policy.default_model) as m:
        status = await m.get_status()
        # FullStatus.serialize() is shallow (nested statuses stay libjuju
        # objects); to_json() encodes the whole tree.
        return {"ok": True, "status": json.loads(status.to_json())}


@_api_errors_as_refusal
async def juju_show_unit(unit: str, policy: Policy) -> UnitResponse | RefusalResponse:
    async with juju_model(policy.default_model) as m:
        if unit not in m.units:
            return {"ok": False, "reason": f"unit {unit!r} not found"}
        return {"ok": True, "unit": unit, "detail": _unit_to_dict(m.units[unit])}


@_api_errors_as_refusal
async def juju_show_application(
    app: str, policy: Policy
) -> ApplicationResponse | RefusalResponse:
    async with juju_model(policy.default_model) as m:
        if app not in m.applications:
            return {"ok": False, "reason": f"application {app!r} not found"}
        a = m.applications[app]
        units = {u.entity_id: _unit_to_dict(u) for u in a.units}
        detail = {
            "charm": getattr(a, "charm_name", None) or getattr(a, "charm_url", None),
            "exposed": bool(getattr(a, "exposed", False)),
            "units": units,
            "application-status": {
                "current": getattr(a, "status", None),
                "message": getattr(a, "status_message", "") or "",
            },
            "scale": len(units),
        }
        return {"ok": True, "application": app, "detail": detail}


@_api_errors_as_refusal
async def juju_machines(
    model: str | None, policy: Policy
) -> MachinesResponse | RefusalResponse:
    gate = policy.model_allowed(model)
    if not gate.ok:
        return {"ok": False, "reason": gate.reason}
    async with juju_model(model or policy.default_model) as m:
        return {
            "ok": True,
            "machines": [_machine_to_dict(machine) for machine in m.machines.values()],
        }


class DebugLogUnavailable(Exception):
    """The debug-log stream could not be opened or returned an error."""


def _parse_log_entry(raw: str | bytes) -> dict[str, Any]:
    """Normalize a single debug-log frame from the controller wire.

    Each frame from /model/<uuid>/log is a JSON object with keys
    ``tag``, ``ts``, ``sev``, ``mod``, ``msg``.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        obj = json.loads(raw)
    except ValueError:
        return {"raw": raw}
    if not isinstance(obj, dict):
        return {"raw": raw}
    return {
        "entity": obj.get("tag") or "",
        "timestamp": obj.get("ts") or "",
        "level": (obj.get("sev") or "").upper(),
        "module": obj.get("mod") or "",
        "message": obj.get("msg") or "",
    }


def _ssl_context(cacert: str | None) -> ssl.SSLContext:
    """TLS context for the controller, matching python-libjuju's own.

    With an explicit CA, hostname checks are off: self-bootstrapped
    controllers are reached by IP, which their certs don't name.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=cacert)
    if cacert:
        ctx.check_hostname = False
    return ctx


async def _collect_log(
    m: Any, *, lines: int, level: str, entity: str | None = None
) -> list[dict[str, Any]]:
    """Return the most recent ``lines`` debug-log entries at or above ``level``.

    python-libjuju's ``Model.debug_log()`` doesn't return entries — it
    swaps the model's WebSocket for a streaming debug-log connection
    that writes formatted lines to a target. To keep our model usable,
    we open the debug-log WebSocket ourselves using the same connection
    params and read structured entries straight off the wire.

    The query parameters are the ones Juju's debug-log handler reads:
    ``backlog`` replays the last N matching lines, ``noTail`` closes the
    stream after the replay, ``maxLines`` caps what is sent, and
    ``includeEntity`` filters server-side.

    Raises :class:`DebugLogUnavailable` when the stream can't be read.
    """
    params = m.connection().connect_params()
    query: dict[str, Any] = {
        "noTail": "true",
        "backlog": lines,
        "maxLines": lines,
        "level": level,
    }
    if entity:
        query["includeEntity"] = entity
    uuid = params.get("uuid") or m.uuid
    url = f"wss://{params['endpoint']}/model/{uuid}/log?{urlencode(query)}"
    creds = f"user-{params.get('username')}:{params.get('password')}"
    auth = "Basic " + base64.b64encode(creds.encode()).decode()

    entries: list[dict[str, Any]] = []
    try:
        async with websockets.connect(
            url,
            ssl=_ssl_context(params.get("cacert")),
            additional_headers={"Authorization": auth},
            max_size=2**24,
        ) as ws:
            # Juju opens every stream with an error-result frame: "{}" on
            # success, {"error": {...}} when the request was rejected.
            first = await asyncio.wait_for(ws.recv(), timeout=_LOG_FRAME_TIMEOUT_S)
            initial = json.loads(first)
            if isinstance(initial, dict) and initial.get("error"):
                err = initial["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                raise DebugLogUnavailable(str(msg))
            if isinstance(initial, dict) and "tag" in initial:
                entries.append(_parse_log_entry(first))
            try:
                while len(entries) < lines:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=_LOG_FRAME_TIMEOUT_S
                    )
                    entries.append(_parse_log_entry(raw))
            except (TimeoutError, websockets.ConnectionClosed):
                pass
    except (OSError, TimeoutError, ValueError, websockets.WebSocketException) as e:
        raise DebugLogUnavailable(f"{type(e).__name__}: {e}") from e
    return entries


@_api_errors_as_refusal
async def juju_debug_log(
    model: str | None,
    lines: int,
    level: str,
    policy: Policy,
) -> LogResponse | RefusalResponse:
    gate = policy.model_allowed(model)
    if not gate.ok:
        return {"ok": False, "reason": gate.reason}
    if lines < 1 or lines > 10_000:
        return {"ok": False, "reason": "lines must be in [1, 10000]"}
    level = (level or "").upper()
    if level not in LOG_LEVELS:
        return {"ok": False, "reason": f"level must be one of {', '.join(LOG_LEVELS)}"}
    async with juju_model(model or policy.default_model) as m:
        try:
            entries = await _collect_log(m, lines=lines, level=level)
        except DebugLogUnavailable as e:
            return {"ok": False, "reason": f"debug-log unavailable: {e}"}
        return {"ok": True, "entries": entries, "level": level}


# ---------------------------------------------------------------------------
# Tier 2 — diagnosis (pure-Python categorization on API-derived dicts)
# ---------------------------------------------------------------------------

def _categorize(unit_detail: dict[str, Any], machine: dict[str, Any] | None) -> str:
    """Pure-Python categorizer. Order matters — most specific first."""
    workload = unit_detail.get("workload-status") or {}
    agent = unit_detail.get("juju-status") or {}
    w_cur = (workload.get("current") or "").lower()
    a_cur = (agent.get("current") or "").lower()
    a_msg = (agent.get("message") or "").lower()
    w_msg = (workload.get("message") or "").lower()

    if machine:
        m_cur = ((machine.get("juju-status") or {}).get("current") or "").lower()
        if m_cur == "down":
            return "machine_down"

    # Charms can't set "error" themselves; the agent sets it when a hook fails.
    if "error" in (a_cur, w_cur) or "hook failed" in a_msg or "hook failed" in w_msg:
        return "stuck_hook"

    if w_cur == "blocked" and "config" in w_msg:
        return "blocked_config"

    if w_cur == "active" and a_cur == "idle":
        return "healthy"

    return "unknown"


def _suggested_action(category: str) -> str:
    return {
        "stuck_hook": (
            "inspect the failed hook in evidence.recent_log (this unit's "
            "latest log lines; juju_debug_log returns model-wide logs); once "
            "the root cause is fixed, call juju_resolved on the unit"
        ),
        "blocked_config": (
            "review the unit's workload-status message — the offending config "
            "key is named there; fix with juju_config_set (subject to "
            "the per-app allowlist)"
        ),
        "machine_down": (
            "the unit's underlying machine is unreachable; investigate "
            "infrastructure (provider console, network) before any juju-side "
            "action — do not retry the unit"
        ),
        "healthy": "no action needed",
        "unknown": (
            "unit does not match any known failure pattern; gather more "
            "context via juju_show_unit and juju_debug_log"
        ),
    }[category]


@_api_errors_as_refusal
async def diagnose_unit(unit: str, policy: Policy) -> DiagnosisResponse | RefusalResponse:
    async with juju_model(policy.default_model) as m:
        if unit not in m.units:
            return {"ok": False, "reason": f"unit {unit!r} not found"}
        u = m.units[unit]
        detail = _unit_to_dict(u)
        machine = _machine_to_dict(u.machine) if getattr(u, "machine", None) else None

        category = _categorize(detail, machine)
        evidence: dict[str, Any] = {
            "workload_status": detail.get("workload-status"),
            "agent_status": detail.get("juju-status"),
            "machine_status": (machine or {}).get("juju-status"),
            "recent_log": [],
        }
        entity = f"unit-{unit.replace('/', '-')}"
        try:
            evidence["recent_log"] = await _collect_log(
                m, lines=20, level="INFO", entity=entity
            )
        except DebugLogUnavailable as e:
            evidence["log_error"] = f"debug-log unavailable: {e}"

        return {
            "ok": True,
            "unit": unit,
            "category": category,
            "evidence": evidence,
            "suggested_action": _suggested_action(category),
        }


__all__ = [
    "LOG_LEVELS",
    "diagnose_unit",
    "juju_debug_log",
    "juju_machines",
    "juju_show_application",
    "juju_show_unit",
    "juju_status",
]
