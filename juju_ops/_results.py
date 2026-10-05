"""Result types for validators and policy gates.

``Check`` is what every validator and policy gate returns: ``ok`` plus a
human-readable ``reason`` when not ok. Tools feed it to the mutation guard
(``g.check(...)``), which turns a failed check into a refusal.

``DryRun`` carries the resolved dry-run state from
``Policy.evaluate_dry_run``. The two booleans are semantically distinct
(``effective`` is the dry-run flag the tool should obey; ``forced_by_policy``
records *why* it's true) and bundling them here keeps callers from getting
the order wrong.
"""

from __future__ import annotations

from typing import NamedTuple


class Check(NamedTuple):
    """Outcome of a validator or policy gate.

    ``ok`` is True iff the input passed; ``reason`` is None when ok and a
    human-readable refusal message otherwise. Construct via ``Check.yes()``
    / ``Check.no(reason)`` for clarity.
    """

    ok: bool
    reason: str | None = None

    @classmethod
    def yes(cls) -> Check:
        return cls(True, None)

    @classmethod
    def no(cls, reason: str) -> Check:
        return cls(False, reason)


class DryRun(NamedTuple):
    """Resolved dry-run state for a mutation tool call.

    ``effective`` is what the tool should actually do; ``forced_by_policy``
    is True when the policy's ``mutations.force_dry_run`` list overrode the
    caller's request.
    """

    effective: bool
    forced_by_policy: bool


__all__ = ["Check", "DryRun"]
