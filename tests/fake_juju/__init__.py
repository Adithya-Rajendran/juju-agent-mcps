"""Fake Juju controller for tests.

A standalone in-process WebSocket server that speaks just enough of the
Juju RPC protocol to make python-libjuju work without a real Juju
controller. Tests connect through the standard
:mod:`juju_ops.juju_client` path; the only test-specific hook is the
``juju_ops_env`` pytest fixture, which boots the server and points the
``JUJU_*`` env vars at it.
"""

from __future__ import annotations

from .certs import FakeCerts, generate_certs
from .server import FakeJujuServer
from .state import (
    Application,
    FakeJujuState,
    Machine,
    Model,
    Relation,
    Unit,
    broad_state,
)

__all__ = [
    "Application",
    "FakeCerts",
    "FakeJujuServer",
    "FakeJujuState",
    "Machine",
    "Model",
    "Relation",
    "Unit",
    "broad_state",
    "generate_certs",
]
