"""Tool implementations for the juju-ops MCP server.

All tools talk to a Juju controller through the python-libjuju API via
:mod:`juju_ops.juju_client`. Tools are organized by tier:

  Tier 1 — read-only:        juju_status, juju_show_unit, juju_show_application,
                             juju_machines, juju_debug_log
  Tier 2 — diagnosis:        diagnose_unit (pure-Python categorization)
  Tier 3 — low-risk mutate:  juju_resolved, juju_config_set
  Tier 4 — scale up:         juju_add_unit (bounded by policy max_units)
  Tier 4 — provisioning:     juju_deploy, juju_add_model, juju_integrate
  Tier 5 — destructive:      juju_remove_unit (bounded by policy min_units)

Any mutating tool listed in the policy's ``mutations.force_dry_run`` only
ever returns a dry-run plan.

Read-only / diagnosis tools live in :mod:`juju_ops.tools.readonly`.
Mutating tools live in :mod:`juju_ops.tools.mutate` and share the
pipeline in :mod:`juju_ops._dispatch`.
"""

from __future__ import annotations

from .mutate import (
    juju_add_model,
    juju_add_unit,
    juju_config_set,
    juju_deploy,
    juju_integrate,
    juju_remove_unit,
    juju_resolved,
)
from .readonly import (
    diagnose_unit,
    juju_debug_log,
    juju_machines,
    juju_show_application,
    juju_show_unit,
    juju_status,
)

__all__ = [
    "diagnose_unit",
    "juju_add_model",
    "juju_add_unit",
    "juju_config_set",
    "juju_debug_log",
    "juju_deploy",
    "juju_integrate",
    "juju_machines",
    "juju_remove_unit",
    "juju_resolved",
    "juju_show_application",
    "juju_show_unit",
    "juju_status",
]
