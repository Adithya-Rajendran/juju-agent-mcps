"""Load and enforce the juju-ops policy YAML.

The policy controls which applications can be mutated, which config keys may
be set, and what scale bounds apply. It is loaded once at server start and
**enforced server-side**. The LLM never sees the policy file — it only sees
the effect (e.g. a tool that returns ``{"ok": false, "reason": "..."}``).

Tools listed in ``mutations.force_dry_run`` always return a dry-run plan,
whatever the caller passes as ``dry_run``. Scale tools are bounded by each
application's ``min_units`` / ``max_units`` instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ._results import Check, DryRun

DEFAULT_POLICY_PATH = "policy/juju-ops.yaml"
SUPPORTED_SCHEMA_VERSION = 1


class PolicyError(ValueError):
    """Raised when the policy file is missing required fields or malformed."""


@dataclass(frozen=True)
class AppPolicy:
    """Per-application bounds drawn from the policy YAML.

    ``config_allowlist`` is the set of config keys callers may set;
    ``min_units`` / ``max_units`` define the inclusive scale envelope.
    """

    name: str
    config_allowlist: tuple[str, ...]
    min_units: int
    max_units: int


@dataclass(frozen=True)
class Policy:
    """Loaded juju-ops policy. The single source of truth for what tools
    are allowed to do.

    Constructed by :func:`load_policy`. The LLM never sees this object;
    it only sees the effect (refusals carry the ``reason`` text).
    """

    default_model: str
    allowed_models: tuple[str, ...]
    apps: dict[str, AppPolicy]
    force_dry_run: frozenset[str]

    # ------------------------------------------------------------------
    # Lookups + checks. Each gate returns a Check; reason is None when ok
    # is True. Tools fold these into the structured {ok, reason} dict.
    # ------------------------------------------------------------------

    def model_allowed(self, model: str | None) -> Check:
        """Check whether ``model`` is one of the policy's allowed models."""
        m = model or self.default_model
        if not self.allowed_models:
            return Check.yes()
        if m in self.allowed_models:
            return Check.yes()
        return Check.no(f"model {m!r} is not in the policy's allowed_models")

    def can_mutate(self, app: str) -> Check:
        """Check whether mutation tools may target ``app``.

        Apps without an entry in ``policy.applications`` are always refused.
        """
        if app in self.apps:
            return Check.yes()
        return Check.no(
            f"application {app!r} is not in the policy; mutations refused"
        )

    def can_set_config(self, app: str, key: str) -> Check:
        """Check whether ``key`` may be set on ``app`` per the allowlist."""
        gate = self.can_mutate(app)
        if not gate.ok:
            return gate
        ap = self.apps[app]
        if key in ap.config_allowlist:
            return Check.yes()
        return Check.no(
            f"config key {key!r} is not in the allowlist for {app!r} "
            f"(allowed: {sorted(ap.config_allowlist)})"
        )

    def can_scale_to(self, app: str, target: int) -> Check:
        """Check whether scaling ``app`` to ``target`` units is in bounds."""
        gate = self.can_mutate(app)
        if not gate.ok:
            return gate
        ap = self.apps[app]
        if target < ap.min_units:
            return Check.no(
                f"target {target} below min_units={ap.min_units} for {app!r}"
            )
        if target > ap.max_units:
            return Check.no(
                f"target {target} above max_units={ap.max_units} for {app!r}"
            )
        return Check.yes()

    def evaluate_dry_run(self, tool_name: str, requested: bool) -> DryRun:
        """Resolve the effective dry-run state for a mutating tool call.

        Single source of truth for "should this tool actually run as a
        dry-run regardless of what the caller asked for?".
        """
        if tool_name in self.force_dry_run:
            return DryRun(effective=True, forced_by_policy=True)
        return DryRun(effective=bool(requested), forced_by_policy=False)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _coerce_app(name: str, body: dict[str, Any]) -> AppPolicy:
    if not isinstance(body, dict):
        raise PolicyError(f"applications.{name} must be a mapping")
    allowlist = body.get("config_allowlist", [])
    if not isinstance(allowlist, list):
        raise PolicyError(f"applications.{name}.config_allowlist must be a list")
    min_u = body.get("min_units", 0)
    max_u = body.get("max_units", 0)
    if not isinstance(min_u, int) or not isinstance(max_u, int):
        raise PolicyError(
            f"applications.{name}.min_units / max_units must be ints"
        )
    if min_u > max_u:
        raise PolicyError(
            f"applications.{name}: min_units ({min_u}) > max_units ({max_u})"
        )
    return AppPolicy(
        name=name,
        config_allowlist=tuple(str(k) for k in allowlist),
        min_units=int(min_u),
        max_units=int(max_u),
    )


def load_policy(path: str | os.PathLike[str] | None = None) -> Policy:
    """Load and validate a juju-ops policy YAML.

    Resolution order for the path:
      1. explicit ``path`` argument
      2. ``$JUJU_OPS_POLICY`` env var
      3. ``./policy/juju-ops.yaml`` relative to the current working directory

    ``$JUJU_OPS_DEFAULT_MODEL`` overrides the file's ``default_model``.
    """
    p = Path(
        path
        or os.environ.get("JUJU_OPS_POLICY")
        or DEFAULT_POLICY_PATH
    )
    if not p.exists():
        raise PolicyError(f"policy file not found at {p}")

    with p.open() as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise PolicyError(f"policy file {p} must be a YAML mapping at top level")

    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
        raise PolicyError(
            f"schema_version is required and must be {SUPPORTED_SCHEMA_VERSION} "
            f"(got {schema_version!r})"
        )

    default_model = raw.get("default_model")
    if not default_model or not isinstance(default_model, str):
        raise PolicyError("policy.default_model is required and must be a string")

    allowed_models = raw.get("allowed_models") or []
    if not isinstance(allowed_models, list):
        raise PolicyError("policy.allowed_models must be a list")

    apps_raw = raw.get("applications") or {}
    if not isinstance(apps_raw, dict):
        raise PolicyError("policy.applications must be a mapping")
    apps = {name: _coerce_app(name, body) for name, body in apps_raw.items()}

    mutations = raw.get("mutations") or {}
    if not isinstance(mutations, dict):
        raise PolicyError("policy.mutations must be a mapping")
    force = mutations.get("force_dry_run") or []
    if not isinstance(force, list):
        raise PolicyError("policy.mutations.force_dry_run must be a list")

    default_model = os.environ.get("JUJU_OPS_DEFAULT_MODEL") or default_model
    allowed = tuple(str(m) for m in allowed_models)
    # Mutations and per-unit tools always target default_model, so it has to
    # pass the same gate as an explicit model argument.
    if allowed and default_model not in allowed:
        raise PolicyError(
            f"default_model {default_model!r} is not in allowed_models {list(allowed)}"
        )

    return Policy(
        default_model=default_model,
        allowed_models=allowed,
        apps=apps,
        force_dry_run=frozenset(str(t) for t in force),
    )


__all__ = ["AppPolicy", "Policy", "PolicyError", "load_policy"]
