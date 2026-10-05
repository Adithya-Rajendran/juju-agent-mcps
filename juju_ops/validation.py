"""Shared input validation for juju-ops tools.

Every validator returns a :class:`Check` so callers can fold the result into
the structured ``{"ok": False, "reason": ...}`` dict that all tools surface.
Keeping the rules in one place means every tool rejects the same garbage
with the same message before anything reaches the Juju API.
"""

from __future__ import annotations

from ._results import Check

# Lowercase alphanumerics + hyphens. Loose match — juju itself does the
# strict check, this just keeps obvious junk away from the controller.
_IDENT_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

# Conservative cap; juju has no fixed limit but anything past this is
# almost certainly a typo or a model hallucinating.
_MAX_NUM_UNITS = 50


def validate_juju_ident(s: str, *, kind: str = "identifier") -> Check:
    """Validate a juju identifier (model or app name).

    ``kind`` names the thing being validated in the refusal message.
    """
    if not s or not isinstance(s, str):
        return Check.no(f"{kind} must be a non-empty string")
    if s[0] == "-" or s[-1] == "-":
        return Check.no(
            f"{kind} {s!r} is not a valid juju identifier "
            "(no leading or trailing hyphens)"
        )
    if not all(c in _IDENT_OK for c in s):
        return Check.no(
            f"{kind} {s!r} is not a valid juju identifier "
            "(lowercase alphanumeric and hyphens only)"
        )
    return Check.yes()


def validate_app_name(s: str) -> Check:
    """Validate an application name. Same rules as juju idents."""
    return validate_juju_ident(s, kind="application name")


def validate_unit_name(s: str) -> Check:
    """Validate a unit name like ``postgresql/0``.

    Format: ``<app>/<index>`` where ``<app>`` is a juju ident and ``<index>``
    is a non-negative integer.
    """
    if not s or not isinstance(s, str):
        return Check.no("unit name must be a non-empty string")
    if "/" not in s:
        return Check.no(
            f"unit name {s!r} must be of the form <app>/<index> "
            "(e.g. 'postgresql/0')"
        )
    app, _, idx = s.partition("/")
    app_check = validate_app_name(app)
    if not app_check.ok:
        return app_check
    if not idx.isdigit():
        return Check.no(f"unit index in {s!r} must be a non-negative integer")
    return Check.yes()


def validate_num_units(
    n: int, *, allow_zero: bool = True, name: str = "num_units"
) -> Check:
    """Validate a unit-count argument.

    Args:
        n: Caller-supplied count.
        allow_zero: True when zero units make sense (e.g. subordinate deploy);
            False for ``add_unit`` where zero is meaningless.
        name: Argument name used in the refusal message.
    """
    if not isinstance(n, int) or isinstance(n, bool):
        return Check.no(f"{name} must be an int")
    lower = 0 if allow_zero else 1
    if n < lower:
        return Check.no(f"{name} must be >= {lower}")
    if n > _MAX_NUM_UNITS:
        return Check.no(f"{name} > {_MAX_NUM_UNITS} refused")
    return Check.yes()


def validate_endpoint(s: str) -> Check:
    """Validate an integration endpoint (``app`` or ``app:endpoint``)."""
    if not s or not isinstance(s, str):
        return Check.no("endpoint must be a non-empty string")
    base = s.split(":", 1)[0]
    if not validate_juju_ident(base).ok:
        return Check.no(
            "endpoints must be valid juju app names, optionally suffixed "
            "with :endpoint (e.g. 'postgresql:db')"
        )
    return Check.yes()


__all__ = [
    "validate_app_name",
    "validate_endpoint",
    "validate_juju_ident",
    "validate_num_units",
    "validate_unit_name",
]
