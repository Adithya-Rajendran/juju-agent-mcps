"""Connect to a Juju controller and model via python-libjuju.

This is the single seam through which every tool talks to the Juju API.
Tests point ``JUJU_CONTROLLER_ENDPOINT`` at the in-process fake controller
in ``tests/fake_juju`` (see ``tests/conftest.py``).

Controller selection (first match wins):

1. ``JUJU_CONTROLLER_ENDPOINT`` plus optional ``JUJU_USERNAME``,
   ``JUJU_PASSWORD``, ``JUJU_CACERT`` — direct connect to the API endpoint.
2. ``JUJU_CONTROLLER`` — a named controller from the local CLI files
   (``~/.local/share/juju/controllers.yaml``).
3. otherwise — the "current" controller from the local CLI files.

Model selection: tools pass the model name explicitly (the caller's
``model`` argument or ``policy.default_model``). With ``JUJU_CONTROLLER``
set, a bare model name is qualified as ``<controller>:<model>``.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

from juju.controller import Controller
from juju.model import Model


def _read_cacert() -> str | None:
    """Resolve ``JUJU_CACERT``: PEM literal or path to a PEM file."""
    raw = os.environ.get("JUJU_CACERT")
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        return p.read_text()
    return raw


def _qualified_model_name(name: str) -> str:
    """Prefix ``JUJU_CONTROLLER`` onto a bare model name for CLI-file lookups."""
    controller = os.environ.get("JUJU_CONTROLLER")
    if controller and ":" not in name:
        return f"{controller}:{name}"
    return name


async def _disconnect(conn: Controller | Model) -> None:
    """Disconnect without letting a teardown error mask the call's outcome.

    By the time we disconnect, the tool's API call has either succeeded or
    already raised; a failure here must not turn a completed mutation into
    a reported error.
    """
    with contextlib.suppress(Exception):
        await conn.disconnect()


async def _connect_controller() -> Controller:
    controller = Controller()
    endpoint = os.environ.get("JUJU_CONTROLLER_ENDPOINT")
    if endpoint:
        await controller.connect(
            endpoint=endpoint,
            username=os.environ.get("JUJU_USERNAME"),
            password=os.environ.get("JUJU_PASSWORD"),
            cacert=_read_cacert(),
        )
    elif name := os.environ.get("JUJU_CONTROLLER"):
        await controller.connect(controller_name=name)
    else:
        await controller.connect()
    return controller


@contextlib.asynccontextmanager
async def juju_controller() -> AsyncIterator[Controller]:
    """Yield a connected :class:`juju.controller.Controller`."""
    c = await _connect_controller()
    try:
        yield c
    finally:
        await _disconnect(c)


@contextlib.asynccontextmanager
async def juju_model(model_name: str) -> AsyncIterator[Model]:
    """Yield a connected :class:`juju.model.Model` for ``model_name``.

    When ``JUJU_CONTROLLER_ENDPOINT`` is set, we go through the controller
    to look the model up by name (python-libjuju can't direct-connect a
    Model without a UUID). Otherwise we use python-libjuju's own CLI-file
    resolution.
    """
    if os.environ.get("JUJU_CONTROLLER_ENDPOINT"):
        c = await _connect_controller()
        try:
            model = await c.get_model(model_name)
            try:
                yield model
            finally:
                await _disconnect(model)
        finally:
            await _disconnect(c)
    else:
        model = Model()
        await model.connect(model_name=_qualified_model_name(model_name))
        try:
            yield model
        finally:
            await _disconnect(model)


__all__ = ["juju_controller", "juju_model"]
