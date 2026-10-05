"""Response shapes for juju-ops tools.

These TypedDicts document — they don't enforce — the dict shapes that
the functions in :mod:`juju_ops.tools` return; each tool is annotated
with the shape(s) it can produce.

Schema rules of thumb:
* Every tool result has ``ok: bool``.
* On ``ok == False`` the result is :class:`RefusalResponse`-shaped.
* On ``ok == True`` mutating tools yield :class:`MutationResponse`;
  read-only tools yield one of the per-shape responses below.
"""

from __future__ import annotations

from typing import Any, TypedDict


class RefusalResponse(TypedDict, total=False):
    """The shape every refusal returns. ``ok`` is always False on this type;
    extra fields like ``current``, ``target`` may be attached by the tool.
    """

    ok: bool
    reason: str


class MutationResponse(TypedDict, total=False):
    """Successful mutation, dry-run or executed."""

    ok: bool
    tool: str
    dry_run: bool
    dry_run_forced_by_policy: bool
    plan: dict[str, Any]
    result: str


class StatusResponse(TypedDict, total=False):
    """``juju_status`` success."""

    ok: bool
    status: dict[str, Any]


class UnitResponse(TypedDict, total=False):
    """``juju_show_unit`` success."""

    ok: bool
    unit: str
    detail: dict[str, Any]


class ApplicationResponse(TypedDict, total=False):
    """``juju_show_application`` success."""

    ok: bool
    application: str
    detail: dict[str, Any]


class MachinesResponse(TypedDict, total=False):
    """``juju_machines`` success."""

    ok: bool
    machines: list[dict[str, Any]]


class LogResponse(TypedDict, total=False):
    """``juju_debug_log`` success."""

    ok: bool
    entries: list[dict[str, Any]]
    level: str


class DiagnosisResponse(TypedDict, total=False):
    """``diagnose_unit`` success."""

    ok: bool
    unit: str
    category: str
    evidence: dict[str, Any]
    suggested_action: str


__all__ = [
    "ApplicationResponse",
    "DiagnosisResponse",
    "LogResponse",
    "MachinesResponse",
    "MutationResponse",
    "RefusalResponse",
    "StatusResponse",
    "UnitResponse",
]
