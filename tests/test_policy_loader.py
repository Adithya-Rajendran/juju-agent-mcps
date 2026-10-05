"""Tests for the juju-ops policy YAML loader and its gates.

The malformed-input branches make sure a typo in the operator's policy
file fails loudly at load time instead of with a confusing ``KeyError``
deep inside a tool call.
"""

from __future__ import annotations

import pytest

from juju_ops._results import DryRun
from juju_ops.policy import PolicyError, load_policy

from .conftest import REPO_ROOT

_HEADER = "schema_version: 1\ndefault_model: sandbox\n"


def _write(tmp_path, body: str):
    p = tmp_path / "juju-ops.yaml"
    p.write_text(body)
    return p


def test_shipped_policy_loads() -> None:
    policy = load_policy(REPO_ROOT / "policy" / "juju-ops.yaml")
    assert policy.default_model in policy.allowed_models
    # juju_deploy's execute path has only run against the fake controller.
    assert "juju_deploy" in policy.force_dry_run


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "missing.yaml")


def test_top_level_must_be_mapping(tmp_path) -> None:
    p = _write(tmp_path, "- not\n- a\n- mapping\n")
    with pytest.raises(PolicyError, match="must be a YAML mapping"):
        load_policy(p)


@pytest.mark.parametrize("version", [None, 0, 2, "1", True])
def test_schema_version_must_be_1(tmp_path, version) -> None:
    line = "" if version is None else f"schema_version: {version!r}\n"
    p = _write(tmp_path, line + "default_model: sandbox\n")
    with pytest.raises(PolicyError, match="schema_version"):
        load_policy(p)


def test_default_model_required(tmp_path) -> None:
    p = _write(tmp_path, "schema_version: 1\napplications: {}\n")
    with pytest.raises(PolicyError, match="default_model"):
        load_policy(p)


def test_allowed_models_must_be_list(tmp_path) -> None:
    p = _write(tmp_path, _HEADER + "allowed_models: not-a-list\napplications: {}\n")
    with pytest.raises(PolicyError, match="allowed_models"):
        load_policy(p)


def test_default_model_must_be_allowed(tmp_path) -> None:
    p = _write(
        tmp_path,
        "schema_version: 1\ndefault_model: prod\nallowed_models: [sandbox]\n",
    )
    with pytest.raises(PolicyError, match="not in allowed_models"):
        load_policy(p)


def test_env_override_must_be_allowed(tmp_path, monkeypatch) -> None:
    p = _write(tmp_path, _HEADER + "allowed_models: [sandbox]\n")
    monkeypatch.setenv("JUJU_OPS_DEFAULT_MODEL", "prod")
    with pytest.raises(PolicyError, match="'prod' is not in allowed_models"):
        load_policy(p)


def test_applications_must_be_mapping(tmp_path) -> None:
    p = _write(tmp_path, _HEADER + "applications: not-a-mapping\n")
    with pytest.raises(PolicyError, match="applications"):
        load_policy(p)


def test_app_body_must_be_mapping(tmp_path) -> None:
    p = _write(tmp_path, _HEADER + "applications:\n  postgresql: not-a-mapping\n")
    with pytest.raises(PolicyError, match="postgresql must be a mapping"):
        load_policy(p)


def test_config_allowlist_must_be_list(tmp_path) -> None:
    p = _write(
        tmp_path,
        _HEADER + "applications:\n  postgresql:\n    config_allowlist: not-a-list\n",
    )
    with pytest.raises(PolicyError, match="config_allowlist"):
        load_policy(p)


def test_unit_bounds_must_be_ints(tmp_path) -> None:
    p = _write(
        tmp_path,
        _HEADER
        + "applications:\n"
        "  postgresql:\n"
        "    min_units: '1'\n"
        "    max_units: 5\n",
    )
    with pytest.raises(PolicyError, match="min_units / max_units"):
        load_policy(p)


def test_min_units_above_max_units_rejected(tmp_path) -> None:
    p = _write(
        tmp_path,
        _HEADER
        + "applications:\n"
        "  postgresql:\n"
        "    min_units: 10\n"
        "    max_units: 1\n",
    )
    with pytest.raises(PolicyError, match="min_units .* max_units"):
        load_policy(p)


def test_mutations_must_be_mapping(tmp_path) -> None:
    p = _write(tmp_path, _HEADER + "mutations: not-a-mapping\napplications: {}\n")
    with pytest.raises(PolicyError, match="mutations"):
        load_policy(p)


def test_force_dry_run_must_be_list(tmp_path) -> None:
    p = _write(tmp_path, _HEADER + "mutations:\n  force_dry_run: not-a-list\n")
    with pytest.raises(PolicyError, match="force_dry_run"):
        load_policy(p)


def test_env_var_overrides_default_model(tmp_path, monkeypatch) -> None:
    p = _write(tmp_path, _HEADER + "allowed_models: [sandbox, prod]\napplications: {}\n")
    monkeypatch.setenv("JUJU_OPS_DEFAULT_MODEL", "prod")
    policy = load_policy(p)
    assert policy.default_model == "prod"


def test_model_allowed_passthrough_when_no_allowlist(tmp_path) -> None:
    policy = load_policy(_write(tmp_path, _HEADER + "applications: {}\n"))
    ok, reason = policy.model_allowed("anything")
    assert ok is True and reason is None


def test_can_set_config_rejects_for_unknown_app(tmp_path) -> None:
    policy = load_policy(_write(tmp_path, _HEADER + "applications: {}\n"))
    ok, reason = policy.can_set_config("ghost", "key")
    assert ok is False
    assert "not in the policy" in reason


def test_can_scale_to_below_min_and_above_max(tmp_path) -> None:
    p = _write(
        tmp_path,
        _HEADER
        + "applications:\n"
        "  api:\n"
        "    min_units: 2\n"
        "    max_units: 4\n",
    )
    policy = load_policy(p)
    ok, reason = policy.can_scale_to("api", 1)
    assert ok is False and "min_units" in reason
    ok, reason = policy.can_scale_to("api", 5)
    assert ok is False and "max_units" in reason
    ok, reason = policy.can_scale_to("api", 3)
    assert ok is True and reason is None


def test_evaluate_dry_run(tmp_path) -> None:
    policy = load_policy(
        _write(tmp_path, _HEADER + "mutations:\n  force_dry_run: [juju_deploy]\n")
    )
    assert policy.evaluate_dry_run("juju_deploy", False) == DryRun(
        effective=True, forced_by_policy=True
    )
    assert policy.evaluate_dry_run("juju_add_unit", True) == DryRun(
        effective=True, forced_by_policy=False
    )
    assert policy.evaluate_dry_run("juju_add_unit", False) == DryRun(
        effective=False, forced_by_policy=False
    )
