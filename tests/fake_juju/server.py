"""Fake Juju controller — TLS WebSocket server that speaks Juju's RPC protocol.

Listens on a free localhost port and handles these URL paths:

* ``/api`` — controller-level connection (login, model management).
* ``/model/<uuid>/api`` — model-level connection (status, mutations,
  watcher).
* ``/model/<uuid>/log`` — debug-log stream (Basic auth, Juju's query
  parameters, initial error-result frame), closed after the replay.
* ``/v2/charms/info/<name>`` — the slice of the Charmhub HTTP API that
  python-libjuju's ``Model.deploy`` reads (the model's ``charmhub-url``
  points back at this server).

Each RPC message is a JSON dict ``{"type", "request", "version",
"request-id", "params"}``. The response is
``{"request-id": N, "response": <payload>}`` or
``{"request-id": N, "error": "...", "error-code": "...", "response": {}}``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import socket
import ssl
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .certs import FakeCerts
from .facades import FacadeDispatcher, FacadeError
from .state import FakeJujuState

log = logging.getLogger(__name__)

_HOST = "127.0.0.1"

# Match with or without a trailing query string.
_MODEL_PATH_RE = re.compile(r"^/model/([0-9a-fA-F-]+)/api(?:\?.*)?$")
_LOG_PATH_RE = re.compile(r"^/model/([0-9a-fA-F-]+)/log(?:\?.*)?$")
_CHARMHUB_INFO_PREFIX = "/v2/charms/info/"

# Juju's debug-log handler accepts TRACE..ERROR and rejects anything else.
_LOG_RANKS = {"TRACE": -2, "DEBUG": -1, "INFO": 0, "WARNING": 1, "ERROR": 2}


class FakeJujuServer:
    """In-process Juju controller fake.

    Use :meth:`start` / :meth:`stop`, or rely on the ``fake_juju_server``
    pytest fixture to manage lifetime.
    """

    def __init__(self, state: FakeJujuState, certs: FakeCerts) -> None:
        self.state = state
        self.certs = certs
        self.port: int = 0  # filled in on start
        self.dispatcher = FacadeDispatcher(state)
        # Tests flip this to make the debug-log endpoint answer 403.
        self.refuse_debug_log = False
        self._server: Any = None

    async def start(self) -> None:
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(
            certfile=str(self.certs.cert_path),
            keyfile=str(self.certs.key_path),
        )
        port = _pick_free_port()
        self._server = await serve(
            self._handle,
            _HOST,
            port,
            ssl=ssl_ctx,
            process_request=self._select_path,
            max_size=2**24,
        )
        self.port = port
        self.dispatcher.charmhub_url = f"https://{self.endpoint}"

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    @property
    def endpoint(self) -> str:
        return f"{_HOST}:{self.port}"

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def _select_path(self, connection: ServerConnection, request: Any) -> Any:
        """Answer plain-HTTP and unauthorized requests; stash the path otherwise."""
        path = request.path
        if path.startswith(_CHARMHUB_INFO_PREFIX):
            return connection.respond(HTTPStatus.OK, json.dumps(_charmhub_info()))
        if _LOG_PATH_RE.match(path):
            if self.refuse_debug_log:
                return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
            creds = f"user-{self.state.username}:{self.state.password}"
            expected = "Basic " + base64.b64encode(creds.encode()).decode()
            if request.headers.get("Authorization") != expected:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        connection._fake_path = path  # type: ignore[attr-defined]
        return None  # accept the handshake

    async def _handle(self, ws: ServerConnection) -> None:
        path = getattr(ws, "_fake_path", "/api")
        try:
            if _MODEL_PATH_RE.match(path) or path == "/api":
                m = _MODEL_PATH_RE.match(path)
                model_uuid = m.group(1) if m else None
                await self._handle_rpc(ws, model_uuid)
            elif (m := _LOG_PATH_RE.match(path)) is not None:
                await self._handle_debug_log(ws, m.group(1), path)
            else:
                await ws.close(code=1003, reason=f"unknown path {path!r}")
        except websockets.ConnectionClosed:
            return

    async def _handle_rpc(self, ws: ServerConnection, model_uuid: str | None) -> None:
        send_lock = asyncio.Lock()
        active: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # Spawn each request as its own task so a long-running
                # call (e.g. AllWatcher.Next blocking until Stop) doesn't
                # block subsequent requests on the same socket.
                task = asyncio.create_task(
                    self._dispatch_one(ws, msg, model_uuid, send_lock)
                )
                active.add(task)
                task.add_done_callback(active.discard)
        finally:
            for t in active:
                t.cancel()

    async def _dispatch_one(
        self,
        ws: ServerConnection,
        msg: dict[str, Any],
        model_uuid: str | None,
        send_lock: asyncio.Lock,
    ) -> None:
        request_id = msg.get("request-id", 0)
        facade = msg.get("type", "")
        method = msg.get("request", "")
        params = msg.get("params") or {}
        # The AllWatcher facade pattern puts the watcher id at the top
        # level of the message (see python-libjuju's
        # juju/client/overrides.py::AllWatcherFacade.rpc).
        target_id = msg.get("Id")
        try:
            response = await self.dispatcher.dispatch(
                facade, method, params,
                model_uuid=model_uuid, target_id=target_id,
            )
            payload = {"request-id": request_id, "response": response}
        except FacadeError as e:
            payload = {
                "request-id": request_id,
                "error": e.message,
                "error-code": e.code or "fake juju error",
                "response": {},
            }
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log.exception("fake juju: unhandled error in %s.%s", facade, method)
            payload = {
                "request-id": request_id,
                "error": f"fake juju: {type(e).__name__}: {e}",
                "error-code": "internal error",
                "response": {},
            }
        async with send_lock:
            with contextlib.suppress(websockets.ConnectionClosed):
                await ws.send(json.dumps(payload))

    async def _handle_debug_log(
        self, ws: ServerConnection, model_uuid: str, path: str
    ) -> None:
        """Replay debug-log entries for the model, then close.

        Reads the query parameters Juju's debug-log handler reads:
        ``level``, ``includeEntity``, ``backlog`` (replay the last N
        matching lines) and ``maxLines``. Like Juju, the first frame is an
        error result: ``{}`` when the request is accepted. The fake never
        tails, so ``noTail`` is implied.
        """
        qs = parse_qs(urlparse(path).query)
        level = (qs.get("level") or [""])[0].upper()
        if level and level not in _LOG_RANKS:
            await ws.send(json.dumps({"error": {"message": f"level value {level!r} is not valid"}}))
            await ws.close()
            return
        m = self.state.models.get(model_uuid)
        if m is None:
            await ws.send(json.dumps({"error": {"message": f"unknown model {model_uuid!r}"}}))
            await ws.close()
            return
        await ws.send("{}\n")

        entries = _synthesize_log(m)
        if level:
            floor = _LOG_RANKS[level]
            entries = [e for e in entries if _LOG_RANKS[e["sev"]] >= floor]
        include = qs.get("includeEntity")
        if include:
            entries = [e for e in entries if e["tag"] in include]
        backlog = int((qs.get("backlog") or ["0"])[0])
        if backlog:
            entries = entries[-backlog:]
        max_lines = int((qs.get("maxLines") or ["0"])[0])
        if max_lines:
            entries = entries[:max_lines]
        for entry in entries:
            await ws.send(json.dumps(entry))
        await ws.close()


def _synthesize_log(model: Any) -> list[dict[str, Any]]:
    """One log entry per unit, at a severity derived from its status."""
    out: list[dict[str, Any]] = []
    for u in model.units.values():
        entity = f"unit-{u.name.replace('/', '-')}"
        base = {"tag": entity, "ts": "2026-05-01T12:00:00Z", "mod": "juju.worker.uniter"}
        if u.workload_status == "error" or u.agent_status == "error":
            msg = u.workload_status_message or u.agent_status_message or "hook failed"
            out.append({**base, "sev": "ERROR", "msg": msg})
        elif u.workload_status == "blocked":
            out.append({**base, "sev": "WARNING", "msg": u.workload_status_message or "blocked"})
        else:
            out.append({**base, "sev": "INFO", "msg": "ran 'update-status' hook"})
    return out


def _charmhub_info() -> dict[str, Any]:
    """Charmhub ``/v2/charms/info`` body: every charm is a non-subordinate."""
    return {"default-release": {"revision": {"subordinate": False}}}


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_HOST, 0))
        return s.getsockname()[1]


__all__ = ["FakeJujuServer"]
