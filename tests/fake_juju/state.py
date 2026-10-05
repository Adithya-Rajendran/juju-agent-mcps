"""In-memory cluster state for the fake Juju controller.

The state is the single source of truth that the facade handlers in
:mod:`tests.fake_juju.facades` read from and mutate. Tests grab the
``FakeJujuState`` instance off the server fixture and either inspect it
(``model.applications["postgresql"].set_config_calls``) or pre-seed it
(``model.add_unit("postgresql")``) before calling a tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass
class Machine:
    id: str
    agent_status: str = "started"
    agent_status_message: str = ""
    machine_status: str = "running"
    machine_status_message: str = ""
    instance_id: str = ""
    series: str = "jammy"
    base: dict[str, str] = field(
        default_factory=lambda: {"name": "ubuntu", "channel": "22.04/stable"}
    )
    hostname: str = ""
    dns_name: str = ""
    addresses: list[dict[str, str]] = field(default_factory=list)
    hardware_characteristics: dict[str, Any] = field(default_factory=dict)
    jobs: list[str] = field(default_factory=lambda: ["JobHostUnits"])
    supported_containers: list[str] = field(default_factory=list)
    has_vote: bool = False
    wants_vote: bool = False


@dataclass
class Unit:
    name: str
    application: str
    series: str = "jammy"
    machine_id: str = ""
    workload_status: str = "active"
    workload_status_message: str = ""
    agent_status: str = "idle"
    agent_status_message: str = ""
    charm_url: str = ""
    public_address: str = ""
    private_address: str = ""
    subordinate: bool = False
    ports: list[dict[str, Any]] = field(default_factory=list)
    port_ranges: list[dict[str, Any]] = field(default_factory=list)
    # Recorded for test assertions
    resolved_calls: list[bool] = field(default_factory=list)


@dataclass
class Application:
    name: str
    charm_url: str
    series: str = "jammy"
    base: dict[str, str] = field(
        default_factory=lambda: {"name": "ubuntu", "channel": "22.04/stable"}
    )
    exposed: bool = False
    life: str = "alive"
    min_units: int = 0
    constraints: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    subordinate: bool = False
    status: str = "active"
    status_message: str = ""
    units: list[str] = field(default_factory=list)  # unit names
    # Recorded for assertions
    add_units_calls: list[dict[str, Any]] = field(default_factory=list)
    destroy_units_calls: list[tuple[str, ...]] = field(default_factory=list)
    set_config_calls: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Relation:
    id: int
    key: str
    endpoints: list[dict[str, str]]  # [{application, relation: {name, role, interface}}]


@dataclass
class Model:
    name: str
    uuid: str
    owner: str = "admin"
    type: str = "iaas"
    region: str = "default"
    cloud: str = "fake-cloud"
    cloud_tag: str = "cloud-fake-cloud"
    life: str = "alive"
    is_controller: bool = False
    applications: dict[str, Application] = field(default_factory=dict)
    units: dict[str, Unit] = field(default_factory=dict)
    machines: dict[str, Machine] = field(default_factory=dict)
    relations: dict[int, Relation] = field(default_factory=dict)
    next_machine_id: int = 0
    next_relation_id: int = 0
    deploy_calls: list[dict[str, Any]] = field(default_factory=list)
    add_relation_calls: list[tuple[str, str]] = field(default_factory=list)

    def add_machine(self, **kwargs: Any) -> Machine:
        mid = str(self.next_machine_id)
        self.next_machine_id += 1
        m = Machine(id=mid, instance_id=f"i-{mid:0>8}", **kwargs)
        self.machines[mid] = m
        return m

    def add_unit(self, application: str, machine_id: str | None = None, **kwargs: Any) -> Unit:
        app = self.applications[application]
        # next index for this app
        existing_idx = [
            int(n.split("/", 1)[1]) for n in app.units if "/" in n
        ]
        idx = max(existing_idx, default=-1) + 1
        name = f"{application}/{idx}"
        if machine_id is None:
            machine_id = self.add_machine().id
        u = Unit(
            name=name,
            application=application,
            machine_id=machine_id,
            charm_url=app.charm_url,
            **kwargs,
        )
        self.units[name] = u
        app.units.append(name)
        return u


@dataclass
class FakeJujuState:
    """Top-level cluster state.

    The controller owns one or more models; each model owns its applications,
    units, machines, and relations. The ``broad_state`` factory below seeds
    the default test layout (postgresql / api / grafana-agent).
    """

    controller_uuid: str = field(default_factory=lambda: str(uuid4()))
    server_version: str = "3.6.0"
    username: str = "admin"
    password: str = "fake-juju-password"
    models: dict[str, Model] = field(default_factory=dict)
    create_model_calls: list[str] = field(default_factory=list)

    def get_model(self, name_or_uuid: str) -> Model | None:
        for m in self.models.values():
            if m.name == name_or_uuid or m.uuid == name_or_uuid:
                return m
        return None

    def add_model(self, name: str) -> Model:
        m = Model(name=name, uuid=str(uuid4()))
        self.models[m.uuid] = m
        return m


# ---------------------------------------------------------------------------
# Default test layout
# ---------------------------------------------------------------------------

def broad_state(model_name: str = "sandbox") -> FakeJujuState:
    """Return a state populated with the broad test fixture:

    * postgresql: 3 units, machines 0/1/2 — pg/0 healthy, pg/1 blocked on
      config, pg/2 on a downed machine.
    * api: 2 units, machines 3/4 — api/0 stuck on a hook, api/1 unknown.
    * grafana-agent: subordinate, 0 units.
    """
    s = FakeJujuState()
    m = s.add_model(model_name)

    # Machines; machine 2 is "down".
    for i in range(5):
        kwargs: dict[str, Any] = {
            "hostname": f"machine-{i}",
            "dns_name": f"10.42.0.{10 + i}",
            "addresses": [{"value": f"10.42.0.{10 + i}", "type": "ipv4", "scope": "local-cloud"}],
        }
        if i == 2:
            kwargs["agent_status"] = "down"
            kwargs["agent_status_message"] = "agent is not communicating"
        m.add_machine(**kwargs)

    # Applications
    m.applications = {
        "postgresql": Application(
            name="postgresql",
            charm_url="ch:amd64/jammy/postgresql-351",
            status="blocked",
            status_message="1/3 units blocked",
        ),
        "api": Application(
            name="api",
            charm_url="ch:amd64/jammy/api-12",
            status="error",
        ),
        "grafana-agent": Application(
            name="grafana-agent",
            charm_url="ch:amd64/jammy/grafana-agent-7",
            subordinate=True,
            status="active",
        ),
    }

    # Units
    m.add_unit(
        "postgresql", machine_id="0",
        workload_status="active", workload_status_message="Live master",
        public_address="10.42.0.10",
    )
    m.add_unit(
        "postgresql", machine_id="1",
        workload_status="blocked",
        workload_status_message="config max_connections=1 invalid: must be >= 50",
        public_address="10.42.0.11",
    )
    m.add_unit("postgresql", machine_id="2", public_address="10.42.0.12")
    m.add_unit(
        "api", machine_id="3",
        workload_status="error", workload_status_message="hook failed: install",
        agent_status="error", agent_status_message="hook failed: install",
        public_address="10.42.0.13",
    )
    m.add_unit(
        "api", machine_id="4", agent_status="executing", public_address="10.42.0.14",
    )
    return s


__all__ = [
    "Application",
    "FakeJujuState",
    "Machine",
    "Model",
    "Relation",
    "Unit",
    "broad_state",
]
