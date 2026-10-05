"""Juju RPC facade handlers for the fake controller.

Each handler is registered with ``@_handler("<Facade>.<Method>")``. It
receives the dispatcher (state + watcher registry), the request
``params``, the current model UUID and the AllWatcher target id, and
returns the response payload that will be wrapped as
``{"request-id": N, "response": <payload>}``.

We implement just enough of the Juju API to make python-libjuju's
``Controller`` / ``Model`` / ``Application`` / ``Unit`` work for the
operations juju-ops uses. Unimplemented methods raise a JSON-RPC error
to surface gaps loudly.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import yaml

from .state import Application, FakeJujuState, Machine, Relation, Unit

# Facade versions we advertise in the Admin.Login response. Client-side
# (python-libjuju) intersects these with its own client_facade_versions
# (juju/client/facade_versions.py) and uses the max common version.
ADVERTISED_FACADES = [
    {"name": "Admin", "versions": [3]},
    {"name": "AllWatcher", "versions": [3]},
    {"name": "Application", "versions": [17, 19, 20]},
    {"name": "Charms", "versions": [6]},
    {"name": "Client", "versions": [6, 7, 8]},
    {"name": "Controller", "versions": [11, 12]},
    {"name": "ModelConfig", "versions": [3]},
    {"name": "ModelManager", "versions": [9, 10]},
    {"name": "Pinger", "versions": [1]},
]


class FacadeError(Exception):
    """Raised by a handler to send a JSON-RPC error response to the client."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Watcher registry — per-connection AllWatcher streams
# ---------------------------------------------------------------------------

class AllWatcher:
    """A single AllWatcher's state.

    First Next() returns the snapshot of the model. Subsequent Next() blocks
    until at least one delta is pushed (by a mutation handler) or the
    watcher is stopped.
    """

    def __init__(self, model_uuid: str) -> None:
        self.id = str(uuid4())
        self.model_uuid = model_uuid
        self.snapshot_sent = False
        self.stopped = asyncio.Event()
        self.queue: asyncio.Queue[list[Any]] = asyncio.Queue()


class WatcherRegistry:
    def __init__(self) -> None:
        self.by_id: dict[str, AllWatcher] = {}

    def create(self, model_uuid: str) -> AllWatcher:
        w = AllWatcher(model_uuid)
        self.by_id[w.id] = w
        return w

    def get(self, watcher_id: str) -> AllWatcher | None:
        return self.by_id.get(watcher_id)

    def drop(self, watcher_id: str) -> None:
        w = self.by_id.pop(watcher_id, None)
        if w:
            w.stopped.set()

    def push(self, model_uuid: str, delta: list[Any]) -> None:
        """Broadcast a single delta to every live watcher on this model."""
        for w in self.by_id.values():
            if w.model_uuid == model_uuid:
                w.queue.put_nowait(delta)


# ---------------------------------------------------------------------------
# Delta encoding
# ---------------------------------------------------------------------------

def _model_delta(m: Any) -> dict[str, Any]:
    return {
        "model-uuid": m.uuid,
        "name": m.name,
        "life": m.life,
        "owner": m.owner,
        "controller-uuid": "",
        "is-controller": m.is_controller,
        "config": {},
        "status": {"current": "available", "message": "", "since": ""},
        "constraints": {},
        "sla": {"level": "unsupported", "owner": ""},
        "cloud-tag": m.cloud_tag,
        "cloud-region": m.region,
        "credential-tag": "",
        "default-series": "jammy",
        "type": m.type,
    }


def _application_delta(model_uuid: str, a: Application) -> dict[str, Any]:
    return {
        "model-uuid": model_uuid,
        "name": a.name,
        "exposed": a.exposed,
        "charm-url": a.charm_url,
        "life": a.life,
        "min-units": a.min_units,
        "constraints": a.constraints,
        "config": a.config,
        "subordinate": a.subordinate,
        "status": {
            "current": a.status,
            "message": a.status_message,
            "since": "",
            "version": "",
        },
        "workload-version": "",
    }


def _unit_delta(model_uuid: str, u: Unit) -> dict[str, Any]:
    return {
        "model-uuid": model_uuid,
        "name": u.name,
        "application": u.application,
        "series": u.series,
        "charm-url": u.charm_url,
        "public-address": u.public_address,
        "private-address": u.private_address,
        "machine-id": u.machine_id,
        "ports": u.ports,
        "port-ranges": u.port_ranges,
        "subordinate": u.subordinate,
        "workload-status": {
            "current": u.workload_status,
            "message": u.workload_status_message,
            "since": "",
            "version": "",
        },
        "agent-status": {
            "current": u.agent_status,
            "message": u.agent_status_message,
            "since": "",
            "version": "",
        },
    }


def _machine_delta(model_uuid: str, mac: Machine) -> dict[str, Any]:
    return {
        "model-uuid": model_uuid,
        "id": mac.id,
        "instance-id": mac.instance_id,
        "agent-status": {
            "current": mac.agent_status,
            "message": mac.agent_status_message,
            "since": "",
            "version": "",
        },
        "instance-status": {
            "current": mac.machine_status,
            "message": mac.machine_status_message,
            "since": "",
            "version": "",
        },
        "life": "alive",
        "series": mac.series,
        "base": f"{mac.base['name']}@{mac.base['channel']}",
        "supported-containers": mac.supported_containers,
        "supported-containers-known": True,
        "hardware-characteristics": mac.hardware_characteristics,
        "jobs": mac.jobs,
        "addresses": mac.addresses,
        "has-vote": mac.has_vote,
        "wants-vote": mac.wants_vote,
        "hostname": mac.hostname,
    }


def _snapshot_deltas(model: Any) -> list[list[Any]]:
    deltas: list[list[Any]] = []
    deltas.append(["model", "change", _model_delta(model)])
    for a in model.applications.values():
        deltas.append(["application", "change", _application_delta(model.uuid, a)])
    for u in model.units.values():
        deltas.append(["unit", "change", _unit_delta(model.uuid, u)])
    for mac in model.machines.values():
        deltas.append(["machine", "change", _machine_delta(model.uuid, mac)])
    return deltas


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------

class FacadeDispatcher:
    """Routes (facade, method) -> handler. One per server."""

    def __init__(self, state: FakeJujuState) -> None:
        self.state = state
        self.watchers = WatcherRegistry()
        # Set by the server once it knows its port; served as the model's
        # charmhub-url so Model.deploy's Charmhub lookup comes back here.
        self.charmhub_url = ""

    async def dispatch(
        self,
        facade: str,
        method: str,
        params: dict[str, Any],
        *,
        model_uuid: str | None,
        target_id: str | None = None,
    ) -> Any:
        key = f"{facade}.{method}"
        handler = _HANDLERS.get(key)
        if handler is None:
            raise FacadeError(
                f"facade {facade!r} method {method!r} not implemented by fake juju",
                code="not implemented",
            )
        return await handler(self, params, model_uuid, target_id)


# Each handler signature:
#   async (dispatcher, params, model_uuid, target_id) -> response payload
# ``target_id`` is the top-level "Id" field that AllWatcher.{Next,Stop}
# carry as the watcher's identifier (set by overrides.AllWatcherFacade.rpc).
_HANDLERS: dict[str, Any] = {}


def _handler(key: str):
    def decorate(fn: Any) -> Any:
        _HANDLERS[key] = fn
        return fn
    return decorate


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@_handler("Admin.Login")
async def _login(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    auth_tag = params.get("auth-tag", "")
    credentials = params.get("credentials", "")
    # auth-tag comes through as "user-admin"
    expected_user_tag = f"user-{d.state.username}"
    if auth_tag != expected_user_tag or credentials != d.state.password:
        raise FacadeError("invalid entity name or password", code="unauthorized access")
    response: dict[str, Any] = {
        "controller-tag": f"controller-{d.state.controller_uuid}",
        "user-info": {
            "display-name": d.state.username,
            "identity": expected_user_tag,
            "controller-access": "superuser",
            "model-access": "admin" if model_uuid else "",
            "last-connection": "",
        },
        "facades": ADVERTISED_FACADES,
        "server-version": d.state.server_version,
        "public-dns-name": "",
        "servers": [],
    }
    if model_uuid:
        response["model-tag"] = f"model-{model_uuid}"
    return response


# ---------------------------------------------------------------------------
# Pinger
# ---------------------------------------------------------------------------

@_handler("Pinger.Ping")
async def _ping(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    return {}


# ---------------------------------------------------------------------------
# ModelConfig (Model._after_connect calls get_config())
# ---------------------------------------------------------------------------

@_handler("ModelConfig.ModelGet")
async def _model_get(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    return {
        "config": {
            "name": {"value": _model(d, model_uuid).name, "source": "controller"},
            "type": {"value": "iaas", "source": "controller"},
            "default-base": {"value": "", "source": "default"},
            "charmhub-url": {"value": d.charmhub_url, "source": "default"},
        }
    }


@_handler("ModelConfig.GetModelConstraints")
async def _get_model_constraints(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    _model(d, model_uuid)
    return {"constraints": {}}


# ---------------------------------------------------------------------------
# Charms (Model.deploy resolves and adds the charm before deploying it)
# ---------------------------------------------------------------------------

# Charmhub names the fake resolves as bundles rather than charms.
FAKE_BUNDLES = frozenset({"cos-lite"})


@_handler("Charms.ResolveCharms")
async def _resolve_charms(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    results = []
    for req in params.get("resolve") or []:
        name = req["reference"].removeprefix("ch:")
        requested = req.get("charm-origin") or {}
        results.append({
            "url": f"ch:{name}",
            "charm-origin": {
                "source": "charm-hub",
                "type": "bundle" if name in FAKE_BUNDLES else "charm",
                "id": f"fake-{name}-id",
                "risk": requested.get("risk") or "stable",
                "track": requested.get("track") or "latest",
                "revision": 1,
                "architecture": requested.get("architecture") or "amd64",
                "base": {"name": "ubuntu", "channel": "22.04"},
            },
            "supported-bases": [{"name": "ubuntu", "channel": "22.04"}],
            "supported-series": ["jammy"],
        })
    return {"results": results}


@_handler("Charms.AddCharm")
async def _add_charm(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    _model(d, model_uuid)
    return {"charm-origin": params.get("charm-origin") or {}}


# ---------------------------------------------------------------------------
# Client (FullStatus, WatchAll, etc.)
# ---------------------------------------------------------------------------

@_handler("Client.WatchAll")
async def _watch_all(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    w = d.watchers.create(m.uuid)
    return {"watcher-id": w.id}


@_handler("Client.FullStatus")
async def _full_status(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    applications: dict[str, Any] = {}
    for a in m.applications.values():
        applications[a.name] = {
            "charm": a.charm_url,
            "series": a.series,
            "exposed": a.exposed,
            "life": a.life,
            "application-status": {
                "status": a.status,
                "info": a.status_message,
                "since": "",
            },
            "units": {
                u: {
                    "workload-status": {
                        "status": m.units[u].workload_status,
                        "info": m.units[u].workload_status_message,
                        "since": "",
                    },
                    "agent-status": {
                        "status": m.units[u].agent_status,
                        "info": m.units[u].agent_status_message,
                        "since": "",
                    },
                    "machine": m.units[u].machine_id,
                    "public-address": m.units[u].public_address,
                }
                for u in a.units
            },
            "charm-version": "",
            "relations": {},
            "subordinate-to": [],
        }
    machines: dict[str, Any] = {}
    for mac in m.machines.values():
        machines[mac.id] = {
            "agent-status": {
                "status": mac.agent_status,
                "info": mac.agent_status_message,
                "since": "",
            },
            "instance-status": {
                "status": mac.machine_status,
                "info": mac.machine_status_message,
                "since": "",
            },
            "dns-name": mac.dns_name,
            "instance-id": mac.instance_id,
            "series": mac.series,
            "id": mac.id,
            "containers": {},
            "hardware": "",
            "ip-addresses": [a["value"] for a in mac.addresses],
            "jobs": mac.jobs,
            "has-vote": mac.has_vote,
            "wants-vote": mac.wants_vote,
        }
    return {
        "model": {
            "name": m.name,
            "type": m.type,
            "cloud-tag": m.cloud_tag,
            "region": m.region,
            "version": d.state.server_version,
            "model-status": {"status": "available", "info": "", "since": ""},
            "available-version": "",
            "controller-timestamp": "",
            "sla": "unsupported",
        },
        "machines": machines,
        "applications": applications,
        "remote-applications": {},
        "offers": {},
        "relations": [],
        "controller-timestamp": "",
        "branches": {},
    }


# ---------------------------------------------------------------------------
# AllWatcher
# ---------------------------------------------------------------------------

@_handler("AllWatcher.Next")
async def _all_watcher_next(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    w = d.watchers.get(target_id) if target_id else None
    if w is None:
        raise FacadeError(f"unknown watcher {target_id!r}", code="not found")
    if not w.snapshot_sent:
        w.snapshot_sent = True
        m = _model_by_uuid(d, w.model_uuid)
        return {"deltas": _snapshot_deltas(m)}
    # Block until the first pending delta lands, or the watcher stops.
    queue_task = asyncio.create_task(w.queue.get())
    stop_task = asyncio.create_task(w.stopped.wait())
    done, pending = await asyncio.wait(
        [queue_task, stop_task], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    if stop_task in done and queue_task in pending:
        return {"deltas": []}
    deltas: list[list[Any]] = [queue_task.result()]
    # Drain any other ready deltas to coalesce into one Next response.
    while not w.queue.empty():
        deltas.append(w.queue.get_nowait())
    return {"deltas": deltas}


@_handler("AllWatcher.Stop")
async def _all_watcher_stop(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    if target_id:
        d.watchers.drop(target_id)
    return {}


# ---------------------------------------------------------------------------
# ModelManager
# ---------------------------------------------------------------------------

@_handler("ModelManager.ListModelSummaries")
async def _list_model_summaries(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    return {
        "results": [
            {
                "result": {
                    "name": m.name,
                    "uuid": m.uuid,
                    "type": m.type,
                    "controller-uuid": d.state.controller_uuid,
                    "is-controller": False,
                    "provider-type": "fake",
                    "default-series": "jammy",
                    "cloud-tag": m.cloud_tag,
                    "cloud-region": m.region,
                    "owner-tag": f"user-{m.owner}",
                    "life": m.life,
                    "status": {"status": "available", "info": "", "since": ""},
                    "user-access": "admin",
                    "last-connection": "",
                    "counts": [],
                }
            }
            for m in d.state.models.values()
        ]
    }


@_handler("ModelManager.ModelInfo")
async def _model_info(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    entities = params.get("entities") or []
    results = []
    for ent in entities:
        tag = ent.get("tag", "")
        uuid_part = tag.split("-", 1)[1] if tag.startswith("model-") else tag
        m = _model_by_uuid(d, uuid_part)
        if m is None:
            results.append({"error": {"message": f"model {uuid_part!r} not found", "code": "not found"}})
            continue
        results.append({"result": _model_info_payload(d, m)})
    return {"results": results}


@_handler("ModelManager.CreateModel")
async def _create_model(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    name = params.get("name")
    if not name:
        raise FacadeError("model name required", code="bad request")
    if d.state.get_model(name) is not None:
        raise FacadeError(f"model {name!r} already exists", code="already exists")
    m = d.state.add_model(name)
    d.state.create_model_calls.append(name)
    return _model_info_payload(d, m)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

@_handler("Application.DeployFromRepository")
async def _deploy_from_repository(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    results = []
    for spec in params.get("Args") or []:
        name = spec.get("ApplicationName")
        if name in m.applications:
            results.append({
                "Errors": [{"message": f"application {name!r} already exists", "code": "already exists"}],
                "PendingResourceUploads": [],
            })
            continue
        config_yaml = yaml.safe_load(spec.get("ConfigYAML") or "") or {}
        a = Application(
            name=name,
            charm_url=spec.get("CharmName") or f"ch:{name}",
            config=dict(config_yaml.get(name) or {}),
        )
        m.applications[name] = a
        m.deploy_calls.append(dict(spec))
        d.watchers.push(m.uuid, ["application", "change", _application_delta(m.uuid, a)])
        for _ in range(spec.get("num-units") or 0):
            _push_new_unit(d, m, m.add_unit(name))
        results.append({"Errors": [], "PendingResourceUploads": []})
    return {"Results": results}


@_handler("Application.AddUnits")
async def _add_units(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    app_name = params.get("application")
    a = m.applications.get(app_name)
    if a is None:
        raise FacadeError(f"application {app_name!r} not found", code="not found")
    n = params.get("num-units") or params.get("numUnits") or 1
    placement = params.get("placement")
    a.add_units_calls.append({"count": n, "placement": placement})
    new = []
    for _ in range(n):
        u = m.add_unit(app_name)
        new.append(u.name)
        _push_new_unit(d, m, u)
    return {"units": new}


@_handler("Application.DestroyUnit")
async def _destroy_unit(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    unit_specs = params.get("units") or []
    results = []
    removed_by_app: dict[str, list[str]] = {}
    for spec in unit_specs:
        # Wire format: each spec is {"unit-tag": "unit-postgresql-0", ...}.
        tag = spec.get("unit-tag") or ""
        if not tag.startswith("unit-"):
            results.append({"error": {"message": f"bad unit-tag {tag!r}"}})
            continue
        name = _unit_name_from_tag(tag)
        u = m.units.pop(name, None)
        if u is None:
            results.append({"error": {"message": f"unit {name!r} not found", "code": "not found"}})
            continue
        app = m.applications[u.application]
        if name in app.units:
            app.units.remove(name)
        removed_by_app.setdefault(u.application, []).append(name)
        # Push a removal delta so the python-libjuju watcher state stays
        # in sync.
        d.watchers.push(m.uuid, ["unit", "remove", _unit_delta(m.uuid, u)])
        results.append({"info": {}})
    for app_name, names in removed_by_app.items():
        m.applications[app_name].destroy_units_calls.append(tuple(names))
    return {"results": results}


@_handler("Application.SetConfigs")
async def _set_configs(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    # Wire schema is ConfigSetArgs._toSchema = {"args": "Args"}; the
    # incoming key is capital "Args".
    specs = params.get("Args") or params.get("args") or []
    results = []
    for spec in specs:
        app_name = spec.get("application")
        cfg = spec.get("config") or {}
        a = m.applications.get(app_name)
        if a is None:
            results.append({"error": {"message": f"application {app_name!r} not found", "code": "not found"}})
            continue
        a.config.update({k: str(v) for k, v in cfg.items()})
        a.set_config_calls.append({k: str(v) for k, v in cfg.items()})
        d.watchers.push(m.uuid, ["application", "change", _application_delta(m.uuid, a)])
        results.append({})
    return {"results": results}


@_handler("Application.AddRelation")
async def _add_relation(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    endpoints = params.get("endpoints") or []
    if len(endpoints) != 2:
        raise FacadeError("expected exactly 2 endpoints", code="bad request")
    rid = m.next_relation_id
    m.next_relation_id += 1
    endpoint_records = []
    for i, e in enumerate(endpoints):
        app, _, name = e.partition(":")
        endpoint_records.append({
            "application-name": app,
            "relation": {
                "name": name or "default",
                "role": "provider" if i == 0 else "requirer",
                "interface": "fake",
                "optional": False,
                "limit": 0,
                "scope": "global",
            },
        })
    rel = Relation(
        id=rid,
        key=" ".join(endpoints),
        endpoints=endpoint_records,
    )
    m.relations[rid] = rel
    m.add_relation_calls.append((endpoints[0], endpoints[1]))
    # Push a relation delta so model.integrate's block_until on
    # _find_relation completes.
    d.watchers.push(m.uuid, ["relation", "change", {
        "model-uuid": m.uuid,
        "id": rid,
        "key": rel.key,
        "endpoints": endpoint_records,
    }])
    response_endpoints = {}
    for er in endpoint_records:
        response_endpoints[er["application-name"]] = {
            "name": er["relation"]["name"],
            "role": er["relation"]["role"],
            "interface": er["relation"]["interface"],
            "optional": False,
            "limit": 0,
            "scope": "global",
        }
    return {"endpoints": response_endpoints}


@_handler("Application.ResolveUnitErrors")
async def _resolve_unit_errors(d: FacadeDispatcher, params: dict[str, Any], model_uuid: str | None, target_id: str | None):
    m = _model(d, model_uuid)
    retry = params.get("retry", False)
    tag_list = (params.get("tags") or {}).get("entities") or []
    results = []
    for ent in tag_list:
        tag = ent.get("tag", "")
        unit_name = _unit_name_from_tag(tag) if tag.startswith("unit-") else tag
        u = m.units.get(unit_name)
        if u is None:
            results.append({"error": {"message": f"unit {unit_name!r} not found", "code": "not found"}})
            continue
        u.resolved_calls.append(bool(retry))
        u.workload_status = "active"
        u.workload_status_message = ""
        u.agent_status = "idle"
        u.agent_status_message = ""
        d.watchers.push(m.uuid, ["unit", "change", _unit_delta(m.uuid, u)])
        results.append({})
    return {"results": results}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_name_from_tag(tag: str) -> str:
    """``unit-grafana-agent-0`` -> ``grafana-agent/0`` (the last hyphen splits)."""
    app, _, idx = tag.removeprefix("unit-").rpartition("-")
    return f"{app}/{idx}"


def _push_new_unit(d: FacadeDispatcher, m: Any, u: Unit) -> None:
    """Push a new unit (and its machine) so ``Model._wait_for_new`` unblocks."""
    mac = m.machines.get(u.machine_id)
    if mac is not None:
        d.watchers.push(m.uuid, ["machine", "change", _machine_delta(m.uuid, mac)])
    d.watchers.push(m.uuid, ["unit", "change", _unit_delta(m.uuid, u)])


def _model(d: FacadeDispatcher, model_uuid: str | None) -> Any:
    if model_uuid is None:
        raise FacadeError("operation requires a model connection", code="bad request")
    m = _model_by_uuid(d, model_uuid)
    if m is None:
        raise FacadeError(f"unknown model {model_uuid!r}", code="not found")
    return m


def _model_by_uuid(d: FacadeDispatcher, uuid: str) -> Any:
    return d.state.models.get(uuid)


def _model_info_payload(d: FacadeDispatcher, m: Any) -> dict[str, Any]:
    return {
        "name": m.name,
        "uuid": m.uuid,
        "type": m.type,
        "controller-uuid": d.state.controller_uuid,
        "is-controller": False,
        "provider-type": "fake",
        "default-series": "jammy",
        "default-base": "ubuntu@22.04/stable",
        "cloud-tag": m.cloud_tag,
        "cloud-region": m.region,
        "owner-tag": f"user-{m.owner}",
        "life": m.life,
        "status": {"status": "available", "info": "", "since": ""},
        "users": [
            {"user": d.state.username, "display-name": d.state.username,
             "access": "admin", "last-connection": ""}
        ],
        "machines": [],
        "sla": {"level": "unsupported"},
        "agent-version": d.state.server_version,
    }


__all__ = ["FacadeDispatcher", "FacadeError"]
