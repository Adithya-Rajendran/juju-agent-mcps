"""Shared mutation pipeline for juju-ops tools.

Every mutation tool has the same shape: validate inputs, run policy
gates, dispatch via the python-libjuju API, audit, return. A failed
validator or gate must short-circuit to the standard refusal dict and
audit ``ok=False``.

The dispatcher exposes:

* ``Refused`` — exception carrying a reason and any extra dict-shaped
  context (e.g. ``current=N`` for scale refusals).
* ``Guard`` — passed to the mutation body. ``.check(Check)`` raises
  ``Refused`` on a failed gate; ``.fail(reason, **extra)`` does the same
  explicitly.
* ``mutate(tool, args)`` — context manager yielding a ``Guard``. On clean
  exit it audits ``ok=True``; on any exception it audits nothing and
  re-raises.
* ``arun_mutation(tool, args, body)`` — runs ``body(guard)`` inside
  ``mutate()``. A ``Refused`` becomes the standard refusal dict via
  ``audit.refuse``; any other exception (a failed libjuju call, a lost
  connection) becomes a refusal whose reason names the error.

``mutate()`` audits the success path and ``audit.refuse`` audits every
failure path, so the audit log gets exactly one record per call.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from ._results import Check
from .audit import audit_mutation, refuse


class Refused(Exception):
    """Short-circuit signal raised inside a ``mutate()`` block.

    Carries the human-readable refusal reason plus optional extra context
    (e.g. ``current=N``, ``target=M``) that the caller wants surfaced in
    the refusal dict.
    """

    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


class Guard:
    """Yielded by :func:`mutate`. Raises :class:`Refused` on a failed check."""

    @staticmethod
    def check(c: Check, **extra: Any) -> None:
        """Raise ``Refused`` with ``c.reason`` if ``c`` is not ok."""
        if not c.ok:
            raise Refused(c.reason or "policy refused", **extra)

    @staticmethod
    def fail(reason: str, **extra: Any) -> None:
        """Raise ``Refused`` with an explicit reason."""
        raise Refused(reason, **extra)


@contextlib.contextmanager
def mutate(tool: str, args: dict[str, Any]) -> Iterator[Guard]:
    """Bracket a mutation. See module docstring."""
    yield Guard()
    audit_mutation(tool, args, ok=True)


async def arun_mutation(
    tool: str,
    args: dict[str, Any],
    body: Callable[[Guard], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run a mutation body; every outcome is audited exactly once."""
    try:
        with mutate(tool, args) as g:
            return await body(g)
    except Refused as r:
        return refuse(tool, args, r.reason, **r.extra)
    except Exception as e:
        return refuse(tool, args, f"juju API error: {type(e).__name__}: {e}")


__all__ = ["Guard", "Refused", "arun_mutation", "mutate"]
