"""Shared pytest setup for juju-agent-mcps.

Provides an in-process fake Juju controller (see :mod:`tests.fake_juju`),
started once per session in a background thread the first time a test
asks for it. The ``juju_ops_env`` fixture points the ``JUJU_*`` env vars
and the juju-ops policy at it; juju-ops tools then open real
python-libjuju WebSocket connections against the fake, so there is no
test-only code path inside ``juju_ops``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path

import pytest

from tests.fake_juju import FakeJujuServer, broad_state
from tests.fake_juju.certs import generate_certs

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_POLICY = Path(__file__).resolve().parent / "policy" / "juju-ops.test.yaml"


class _ServerThread:
    """Run a :class:`FakeJujuServer` in its own thread + event loop.

    The server lifecycle has to be independent of pytest-asyncio's per-test
    event loops, so we give it a dedicated loop pinned to a daemon thread.
    """

    def __init__(self, server: FakeJujuServer) -> None:
        self.server = server
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._started = threading.Event()
        self._error: BaseException | None = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.start())
        except BaseException as e:
            self._error = e
            self._started.set()
            return
        self._started.set()
        self.loop.run_forever()

    def start(self) -> None:
        self.thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("fake Juju controller did not start within 10s")
        if self._error is not None:
            raise RuntimeError("fake Juju controller failed to start") from self._error

    def stop(self) -> None:
        async def _shutdown() -> None:
            await self.server.stop()
        future = asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
        with contextlib.suppress(Exception):
            future.result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)


@pytest.fixture(scope="session")
def fake_juju_server(tmp_path_factory):
    """Session-scoped fake Juju controller. Returns the server object."""
    certs = generate_certs(tmp_path_factory.mktemp("fake_juju_certs"))
    server = FakeJujuServer(broad_state(), certs)
    runner = _ServerThread(server)
    runner.start()
    yield server
    runner.stop()


@pytest.fixture
def fake_juju_state(fake_juju_server):
    """Per-test reset: replace the shared server's state with a fresh layout."""
    fresh = broad_state()
    fake_juju_server.state = fresh
    fake_juju_server.dispatcher.state = fresh
    fake_juju_server.dispatcher.watchers.by_id.clear()
    fake_juju_server.refuse_debug_log = False
    return fresh


@pytest.fixture
def juju_ops_env(
    monkeypatch: pytest.MonkeyPatch,
    fake_juju_server: FakeJujuServer,
    fake_juju_state,
) -> None:
    """Point juju-ops at the fake controller and the test policy."""
    monkeypatch.setenv("JUJU_CONTROLLER_ENDPOINT", fake_juju_server.endpoint)
    monkeypatch.setenv("JUJU_USERNAME", fake_juju_server.state.username)
    monkeypatch.setenv("JUJU_PASSWORD", fake_juju_server.state.password)
    monkeypatch.setenv("JUJU_CACERT", str(fake_juju_server.certs.ca_path))
    # Model.deploy fetches Charmhub metadata with `requests`; the fake
    # serves that endpoint too, behind the same test CA.
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(fake_juju_server.certs.ca_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("JUJU_CONTROLLER", raising=False)
    monkeypatch.delenv("JUJU_OPS_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("JUJU_OPS_POLICY", str(TEST_POLICY))
    monkeypatch.setenv("JUJU_OPS_AUDIT", "off")

    from juju_ops import server as srv

    monkeypatch.setattr(srv, "_POLICY", None)
