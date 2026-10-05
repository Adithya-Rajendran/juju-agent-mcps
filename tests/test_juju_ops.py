"""Integration tests for juju-ops against the in-process fake Juju controller.

The ``juju_ops_env`` fixture (in conftest.py) resets the controller's
state for each test and points the ``JUJU_*`` env vars and the policy at
``tests/policy/juju-ops.test.yaml``. juju-ops tools open real
python-libjuju WebSocket connections — there is no monkeypatching inside
``juju_ops``.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from juju_ops import server as srv
from juju_ops import tools
from juju_ops.juju_client import _qualified_model_name
from juju_ops.policy import load_policy

from .fake_juju import FakeJujuState

pytestmark = pytest.mark.usefixtures("juju_ops_env")

_MUTATION_KEYS = {"ok", "tool", "dry_run", "dry_run_forced_by_policy", "plan", "result"}


def _model(state: FakeJujuState):
    return next(iter(state.models.values()))


def _unforced_policy():
    return dataclasses.replace(load_policy(), force_dry_run=frozenset())


# ---------------------------------------------------------------------------
# Connection selection
# ---------------------------------------------------------------------------

def test_juju_controller_qualifies_bare_model_names(monkeypatch) -> None:
    monkeypatch.setenv("JUJU_CONTROLLER", "prod-ctrl")
    assert _qualified_model_name("sandbox") == "prod-ctrl:sandbox"
    assert _qualified_model_name("other:sandbox") == "other:sandbox"
    monkeypatch.delenv("JUJU_CONTROLLER")
    assert _qualified_model_name("sandbox") == "sandbox"


# ---------------------------------------------------------------------------
# Tier 1 — read-only
# ---------------------------------------------------------------------------

async def test_juju_status_returns_plain_json(fake_juju_state: FakeJujuState) -> None:
    out = await srv.juju_status()
    assert out["ok"] is True
    s = out["status"]
    # Nested sections must be plain dicts, not libjuju objects, so the
    # payload survives MCP's JSON encoding unchanged.
    assert json.loads(json.dumps(s)) == s
    assert type(s["applications"]["postgresql"]) is dict
    assert s["model"]["name"] == "sandbox"
    assert set(s["applications"]) == {"postgresql", "api", "grafana-agent"}


async def test_juju_status_rejects_disallowed_model(fake_juju_state) -> None:
    out = await srv.juju_status(model="nonexistent")
    assert out["ok"] is False
    assert "allowed_models" in out["reason"]


@pytest.mark.parametrize("call", [tools.juju_status, tools.juju_machines])
async def test_read_tools_report_api_errors_as_refusals(fake_juju_state, call) -> None:
    # With an empty allowed_models the gate passes, so the controller is
    # what rejects the unknown model; the tool still returns `ok`.
    policy = dataclasses.replace(load_policy(), allowed_models=())
    out = await call("no-such-model", policy)
    assert out["ok"] is False
    assert out["reason"].startswith("juju API error: ")


async def test_juju_show_unit_returns_detail(fake_juju_state) -> None:
    out = await srv.juju_show_unit("postgresql/0")
    assert out["ok"] is True
    assert out["unit"] == "postgresql/0"
    detail = out["detail"]
    assert detail["application"] == "postgresql"
    assert detail["machine"] == "0"
    assert detail["workload-status"]["current"] == "active"


async def test_juju_show_unit_missing_unit(fake_juju_state) -> None:
    out = await srv.juju_show_unit("nope/99")
    assert out["ok"] is False
    assert "not found" in out["reason"]


async def test_juju_show_application_returns_units(fake_juju_state) -> None:
    out = await srv.juju_show_application("postgresql")
    assert out["ok"] is True
    assert out["application"] == "postgresql"
    assert set(out["detail"]["units"]) == {
        "postgresql/0", "postgresql/1", "postgresql/2",
    }
    assert out["detail"]["scale"] == 3


async def test_juju_show_application_missing(fake_juju_state) -> None:
    out = await srv.juju_show_application("ghost")
    assert out["ok"] is False
    assert "not found" in out["reason"]


async def test_juju_machines_lists_all(fake_juju_state) -> None:
    out = await srv.juju_machines()
    assert out["ok"] is True
    ids = sorted(m["id"] for m in out["machines"])
    assert ids == ["0", "1", "2", "3", "4"]


async def test_juju_debug_log_filters_by_level(fake_juju_state) -> None:
    out = await srv.juju_debug_log(lines=100, level="warning")
    assert out["ok"] is True
    assert out["level"] == "WARNING"
    # The fake logs one line per unit: INFO for healthy ones, WARNING for
    # the blocked postgresql/1 and ERROR for the failed api/0.
    assert {(e["entity"], e["level"]) for e in out["entries"]} == {
        ("unit-postgresql-1", "WARNING"),
        ("unit-api-0", "ERROR"),
    }


async def test_juju_debug_log_returns_most_recent_lines(fake_juju_state) -> None:
    out = await srv.juju_debug_log(lines=1, level="INFO")
    assert out["ok"] is True
    assert [e["entity"] for e in out["entries"]] == ["unit-api-1"]


async def test_juju_debug_log_lines_bounds(fake_juju_state) -> None:
    out = await srv.juju_debug_log(lines=0)
    assert out["ok"] is False


@pytest.mark.parametrize("level", ["CRITICAL", "warning&noTail=false", ""])
async def test_juju_debug_log_rejects_unknown_level(fake_juju_state, level) -> None:
    out = await srv.juju_debug_log(level=level)
    assert out["ok"] is False
    assert "level must be one of" in out["reason"]


async def test_juju_debug_log_reports_unreadable_stream(
    fake_juju_server, fake_juju_state
) -> None:
    fake_juju_server.refuse_debug_log = True
    out = await srv.juju_debug_log()
    assert out["ok"] is False
    assert out["reason"].startswith("debug-log unavailable")
    assert "403" in out["reason"]


# ---------------------------------------------------------------------------
# Tier 2 — diagnosis
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unit,expected",
    [
        ("postgresql/0", "healthy"),
        ("postgresql/1", "blocked_config"),
        ("postgresql/2", "machine_down"),
        ("api/0", "stuck_hook"),
        ("api/1", "unknown"),
    ],
)
async def test_diagnose_unit_covers_every_category(
    fake_juju_state, unit, expected
) -> None:
    out = await srv.diagnose_unit(unit)
    assert out["ok"] is True, out
    assert out["unit"] == unit
    assert out["category"] == expected, (
        f"{unit} expected {expected} got {out['category']}: {out['evidence']}"
    )
    assert out["suggested_action"]


async def test_diagnose_unit_evidence_is_scoped_to_the_unit(fake_juju_state) -> None:
    out = await srv.diagnose_unit("api/0")
    ev = out["evidence"]
    assert ev["agent_status"]["current"] == "error"
    assert "log_error" not in ev
    assert ev["recent_log"]
    assert {e["entity"] for e in ev["recent_log"]} == {"unit-api-0"}
    assert any("hook failed: install" in e["message"] for e in ev["recent_log"])


async def test_diagnose_unit_reports_machine_status(fake_juju_state) -> None:
    out = await srv.diagnose_unit("postgresql/2")
    assert out["evidence"]["machine_status"]["current"] == "down"


async def test_diagnose_unit_surfaces_log_errors(
    fake_juju_server, fake_juju_state
) -> None:
    fake_juju_server.refuse_debug_log = True
    out = await srv.diagnose_unit("api/0")
    assert out["ok"] is True
    assert out["category"] == "stuck_hook"
    assert out["evidence"]["recent_log"] == []
    assert out["evidence"]["log_error"].startswith("debug-log unavailable")


async def test_diagnose_unit_missing_unit_returns_not_ok(fake_juju_state) -> None:
    out = await srv.diagnose_unit("nope/99")
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# Tier 3 — low-risk mutations
# ---------------------------------------------------------------------------

async def test_juju_resolved_on_known_unit_calls_api(fake_juju_state) -> None:
    out = await srv.juju_resolved("api/0")
    assert set(out) == _MUTATION_KEYS
    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["plan"] == {"unit": "api/0", "no_retry": False}
    # Default no_retry=False -> retry=True.
    assert _model(fake_juju_state).units["api/0"].resolved_calls == [True]


async def test_juju_resolved_passes_no_retry_flag(fake_juju_state) -> None:
    await srv.juju_resolved("api/0", no_retry=True)
    assert _model(fake_juju_state).units["api/0"].resolved_calls == [False]


async def test_juju_resolved_honors_force_dry_run(fake_juju_state) -> None:
    p = dataclasses.replace(load_policy(), force_dry_run=frozenset({"juju_resolved"}))
    out = await tools.juju_resolved("api/0", no_retry=False, policy=p)
    assert out["dry_run"] is True
    assert out["dry_run_forced_by_policy"] is True
    assert _model(fake_juju_state).units["api/0"].resolved_calls == []


async def test_juju_resolved_unknown_app_rejected(fake_juju_state) -> None:
    out = await srv.juju_resolved("rogue/0")
    assert out["ok"] is False
    assert "not in the policy" in out["reason"]


async def test_juju_resolved_invalid_unit_format(fake_juju_state) -> None:
    out = await srv.juju_resolved("not-a-unit")
    assert out["ok"] is False


async def test_juju_config_set_in_allowlist_returns_dry_run_plan(fake_juju_state) -> None:
    out = await srv.juju_config_set("postgresql", "max_connections", "200")
    assert set(out) == _MUTATION_KEYS
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["plan"] == {"app": "postgresql", "key": "max_connections", "value": "200"}
    assert _model(fake_juju_state).applications["postgresql"].set_config_calls == []


async def test_juju_config_set_outside_allowlist_rejected(fake_juju_state) -> None:
    out = await srv.juju_config_set("postgresql", "shutdown_now", "true")
    assert out["ok"] is False
    assert "allowlist" in out["reason"]


async def test_juju_config_set_unknown_app_rejected(fake_juju_state) -> None:
    out = await srv.juju_config_set("rogue", "x", "y")
    assert out["ok"] is False
    assert "rogue" in out["reason"]


async def test_juju_config_set_undeployed_app_refused_even_when_forced(fake_juju_state) -> None:
    # redis is in the policy but not in the model; the forced dry-run must
    # not hand back a plan for an app that doesn't exist.
    out = await srv.juju_config_set("redis", "maxmemory", "1gb")
    assert out["ok"] is False
    assert "not found" in out["reason"]


async def test_juju_config_set_dry_run_false_overridden_by_policy(fake_juju_state) -> None:
    out = await srv.juju_config_set(
        "postgresql", "max_connections", "200", dry_run=False
    )
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["dry_run_forced_by_policy"] is True
    assert _model(fake_juju_state).applications["postgresql"].set_config_calls == []


async def test_juju_config_set_without_force_calls_api(fake_juju_state) -> None:
    out = await tools.juju_config_set(
        "postgresql", "max_connections", "200", dry_run=False, policy=_unforced_policy(),
    )
    assert out["ok"] is True
    assert out["dry_run"] is False
    assert _model(fake_juju_state).applications["postgresql"].set_config_calls == [
        {"max_connections": "200"},
    ]


# ---------------------------------------------------------------------------
# Tier 4 — scale ops
# ---------------------------------------------------------------------------

async def test_juju_add_unit_within_bounds_dry_run(fake_juju_state) -> None:
    out = await srv.juju_add_unit("postgresql", count=2)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["plan"]["target"] == 5
    assert out["plan"]["current"] == 3
    assert _model(fake_juju_state).applications["postgresql"].add_units_calls == []


async def test_juju_add_unit_executed_calls_api(fake_juju_state) -> None:
    out = await srv.juju_add_unit("postgresql", count=2, dry_run=False)
    assert out["ok"] is True
    assert out["dry_run"] is False
    model = _model(fake_juju_state)
    assert model.applications["postgresql"].add_units_calls == [
        {"count": 2, "placement": None},
    ]
    assert len(model.applications["postgresql"].units) == 5


async def test_juju_add_unit_with_placement(fake_juju_state) -> None:
    await srv.juju_add_unit("postgresql", count=1, to="lxd:0", dry_run=False)
    calls = _model(fake_juju_state).applications["postgresql"].add_units_calls
    assert len(calls) == 1
    assert calls[0]["count"] == 1
    [placement] = calls[0]["placement"]
    assert (placement["scope"], placement["directive"]) == ("lxd", "0")


async def test_juju_add_unit_exceeds_max_rejected(fake_juju_state) -> None:
    out = await srv.juju_add_unit("postgresql", count=5)
    assert out["ok"] is False
    assert "max_units" in out["reason"]
    assert out["current"] == 3


@pytest.mark.parametrize("count", [0, -1, "2", True])
async def test_juju_add_unit_rejects_bad_count(fake_juju_state, count) -> None:
    out = await tools.juju_add_unit(
        "postgresql", count, None, dry_run=True, policy=load_policy()
    )
    assert out["ok"] is False
    assert out["reason"].startswith("count must be")


async def test_juju_add_unit_undeployed_app_rejected(fake_juju_state) -> None:
    out = await srv.juju_add_unit("redis")
    assert out["ok"] is False
    assert "not found" in out["reason"]


# ---------------------------------------------------------------------------
# Tier 5 — destructive
# ---------------------------------------------------------------------------

async def test_juju_remove_unit_dry_run_default(fake_juju_state) -> None:
    # api: min=1, current=2 -> removing one leaves it at 1, still in bounds.
    out = await srv.juju_remove_unit("api/1")
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["plan"] == {"unit": "api/1", "app": "api", "current": 2}
    assert _model(fake_juju_state).applications["api"].destroy_units_calls == []


async def test_juju_remove_unit_executed_calls_api(fake_juju_state) -> None:
    out = await srv.juju_remove_unit("api/1", dry_run=False)
    assert out["ok"] is True
    assert out["dry_run"] is False
    assert _model(fake_juju_state).applications["api"].destroy_units_calls == [("api/1",)]


async def test_juju_remove_unit_below_min_rejected(fake_juju_state) -> None:
    # postgresql: min=3, current=3 -> removing one would drop to 2 < min.
    out = await srv.juju_remove_unit("postgresql/2", dry_run=False)
    assert out["ok"] is False
    assert "min_units" in out["reason"]
    assert _model(fake_juju_state).applications["postgresql"].destroy_units_calls == []


async def test_juju_remove_unit_missing_unit_rejected(fake_juju_state) -> None:
    out = await srv.juju_remove_unit("api/9")
    assert out["ok"] is False
    assert "not found" in out["reason"]


async def test_juju_remove_unit_unknown_app_rejected(fake_juju_state) -> None:
    out = await srv.juju_remove_unit("rogue/0")
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# Tier 4 provisioning — deploy / add_model / integrate
# ---------------------------------------------------------------------------

async def test_juju_deploy_returns_dry_run_plan_for_new_charm(fake_juju_state) -> None:
    out = await srv.juju_deploy("nginx", num_units=2, channel="stable")
    assert set(out) == _MUTATION_KEYS
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["dry_run_forced_by_policy"] is True
    assert out["plan"]["charm"] == "nginx"
    assert out["plan"]["app_name"] == "nginx"
    assert out["plan"]["num_units"] == 2
    assert _model(fake_juju_state).deploy_calls == []


async def test_juju_deploy_uses_app_name_when_provided(fake_juju_state) -> None:
    out = await srv.juju_deploy("nginx-ingress-integrator", app_name="ingress")
    assert out["ok"] is True
    assert out["plan"]["app_name"] == "ingress"


async def test_juju_deploy_accepts_ch_prefix(fake_juju_state) -> None:
    out = await srv.juju_deploy("ch:nginx")
    assert out["ok"] is True
    assert out["plan"]["charm"] == "nginx"
    assert out["plan"]["app_name"] == "nginx"


@pytest.mark.parametrize("charm", ["local:/srv/charm", "./my-charm", "/etc", ""])
async def test_juju_deploy_rejects_non_charmhub_charms(fake_juju_state, charm) -> None:
    out = await srv.juju_deploy(charm)
    assert out["ok"] is False
    assert "charm name" in out["reason"]


async def test_juju_deploy_rejects_invalid_app_name(fake_juju_state) -> None:
    out = await srv.juju_deploy("postgresql", app_name="-bad-name-")
    assert out["ok"] is False
    assert "valid juju identifier" in out["reason"]


@pytest.mark.parametrize(
    "num_units,reason",
    [(-1, "num_units must be >= 0"), (999, "num_units > 50"), ("2", "num_units must be an int")],
)
async def test_juju_deploy_rejects_bad_num_units(fake_juju_state, num_units, reason) -> None:
    out = await tools.juju_deploy(
        "nginx", None, num_units, "stable", None, None, dry_run=True, policy=load_policy(),
    )
    assert out["ok"] is False
    assert out["reason"].startswith(reason)


async def test_juju_deploy_zero_units_allowed_for_subordinates(fake_juju_state) -> None:
    out = await srv.juju_deploy("ssh-keys", num_units=0)
    assert out["ok"] is True
    assert out["plan"]["num_units"] == 0


async def test_juju_deploy_existing_app_refused_even_when_forced(fake_juju_state) -> None:
    out = await srv.juju_deploy("postgresql")
    assert out["ok"] is False
    assert "already exists" in out["reason"]


async def test_juju_deploy_when_executed_calls_api(fake_juju_state) -> None:
    out = await tools.juju_deploy(
        "nginx", "edge", 2, "stable", {"foo": "bar"}, None,
        dry_run=False, policy=_unforced_policy(),
    )
    assert out["ok"] is True, out
    assert out["dry_run"] is False
    model = _model(fake_juju_state)
    [call] = model.deploy_calls
    assert call["ApplicationName"] == "edge"
    assert "nginx" in call["CharmName"]
    assert call["num-units"] == 2
    assert call["channel"] == "stable"
    app = model.applications["edge"]
    assert app.config == {"foo": "bar"}
    assert len(app.units) == 2


@pytest.mark.parametrize("trust", [True, "false", False])
async def test_juju_deploy_refuses_trust_in_config(fake_juju_state, trust) -> None:
    out = await tools.juju_deploy(
        "nginx", None, 1, "stable", {"trust": trust}, None,
        dry_run=False, policy=_unforced_policy(),
    )
    assert out["ok"] is False
    assert "trust" in out["reason"]
    assert _model(fake_juju_state).deploy_calls == []


@pytest.mark.parametrize("dry_run", [True, False])
async def test_juju_deploy_refuses_bundles(fake_juju_state, dry_run) -> None:
    out = await tools.juju_deploy(
        "cos-lite", None, 1, "stable", None, None,
        dry_run=dry_run, policy=_unforced_policy(),
    )
    assert out["ok"] is False
    assert "bundle" in out["reason"]
    assert _model(fake_juju_state).deploy_calls == []


async def test_juju_add_model_valid_name(fake_juju_state) -> None:
    out = await srv.juju_add_model("staging")
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["dry_run_forced_by_policy"] is True
    assert out["plan"]["name"] == "staging"
    assert fake_juju_state.create_model_calls == []


async def test_juju_add_model_rejects_uppercase(fake_juju_state) -> None:
    out = await srv.juju_add_model("Staging")
    assert out["ok"] is False
    assert "valid juju identifier" in out["reason"]


async def test_juju_add_model_existing_model_refused(fake_juju_state) -> None:
    out = await srv.juju_add_model("sandbox")
    assert out["ok"] is False
    assert "already exists" in out["reason"]


async def test_juju_add_model_when_executed_calls_controller(fake_juju_state) -> None:
    out = await tools.juju_add_model("staging", dry_run=False, policy=_unforced_policy())
    assert out["ok"] is True
    assert fake_juju_state.create_model_calls == ["staging"]
    assert fake_juju_state.get_model("staging").owner == "admin"


async def test_juju_integrate_two_apps_dry_run(fake_juju_state) -> None:
    out = await srv.juju_integrate("postgresql", "api")
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["dry_run_forced_by_policy"] is True
    assert _model(fake_juju_state).add_relation_calls == []


async def test_juju_integrate_with_endpoints(fake_juju_state) -> None:
    out = await srv.juju_integrate("postgresql:db", "api:database")
    assert out["ok"] is True
    assert out["plan"]["app_a"] == "postgresql:db"


@pytest.mark.parametrize(
    "app_a,app_b", [("postgresql", "postgresql"), ("postgresql:db", "postgresql:replicas")]
)
async def test_juju_integrate_rejects_self_relation(fake_juju_state, app_a, app_b) -> None:
    out = await srv.juju_integrate(app_a, app_b)
    assert out["ok"] is False
    assert "itself" in out["reason"]


async def test_juju_integrate_missing_app_refused_even_when_forced(fake_juju_state) -> None:
    out = await srv.juju_integrate("postgresql", "ghost:db")
    assert out["ok"] is False
    assert "'ghost' not found" in out["reason"]


async def test_juju_integrate_rejects_invalid_endpoint(fake_juju_state) -> None:
    out = await srv.juju_integrate("Bad", "api")
    assert out["ok"] is False
    assert "endpoints must be valid" in out["reason"]


async def test_juju_integrate_when_executed_calls_api(fake_juju_state) -> None:
    out = await tools.juju_integrate(
        "postgresql", "api", dry_run=False, policy=_unforced_policy()
    )
    assert out["ok"] is True
    assert _model(fake_juju_state).add_relation_calls == [("postgresql", "api")]


# ---------------------------------------------------------------------------
# Failure handling + force_dry_run override
# ---------------------------------------------------------------------------

async def test_failed_api_call_is_an_audited_refusal(
    fake_juju_state, monkeypatch, tmp_path
) -> None:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("JUJU_OPS_AUDIT_LOG", str(log))
    monkeypatch.delenv("JUJU_OPS_AUDIT")
    monkeypatch.setenv("JUJU_PASSWORD", "wrong")
    out = await srv.juju_add_unit("postgresql", dry_run=False)
    assert out["ok"] is False
    assert out["reason"] == (
        "juju API error: JujuAPIError: invalid entity name or password"
    )
    rec = json.loads(log.read_text(encoding="utf-8"))
    assert rec["tool"] == "juju_add_unit"
    assert rec["ok"] is False
    assert rec["reason"] == out["reason"]
    assert _model(fake_juju_state).applications["postgresql"].add_units_calls == []


async def test_force_dry_run_overrides_caller_for_listed_tool(fake_juju_state) -> None:
    out = await srv.juju_deploy(charm="newapp", num_units=1, dry_run=False)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["dry_run_forced_by_policy"] is True


# ---------------------------------------------------------------------------
# Schema sanity — every registered tool has a docstring + signature
# ---------------------------------------------------------------------------

def test_all_tools_registered_have_descriptions() -> None:
    tm = getattr(srv.mcp, "_tool_manager", None)
    assert tm is not None
    registered = list(tm._tools.values())  # noqa: SLF001
    names = {t.name for t in registered}
    expected = {
        "juju_status",
        "juju_show_unit",
        "juju_show_application",
        "juju_machines",
        "juju_debug_log",
        "diagnose_unit",
        "juju_resolved",
        "juju_config_set",
        "juju_add_unit",
        "juju_deploy",
        "juju_add_model",
        "juju_integrate",
        "juju_remove_unit",
    }
    assert names == expected
    assert "juju_scale_application" not in names
    for t in registered:
        assert t.description, f"{t.name} missing description"
