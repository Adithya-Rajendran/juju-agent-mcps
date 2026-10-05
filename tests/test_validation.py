"""Tests for juju_ops.validation — shared input validators."""

from __future__ import annotations

import pytest

from juju_ops._results import Check
from juju_ops.validation import (
    validate_app_name,
    validate_endpoint,
    validate_juju_ident,
    validate_num_units,
    validate_unit_name,
)


def test_check_yes_no_constructors() -> None:
    assert Check.yes() == Check(ok=True, reason=None)
    assert Check.no("nope") == Check(ok=False, reason="nope")


@pytest.mark.parametrize(
    "name,ok",
    [
        ("postgresql", True),
        ("api-server", True),
        ("a", True),
        ("a1b2", True),
        ("", False),
        ("-leading", False),
        ("trailing-", False),
        ("UPPER", False),
        ("under_score", False),
        ("space here", False),
    ],
)
def test_validate_juju_ident(name, ok) -> None:
    got, _ = validate_juju_ident(name)
    assert got is ok


def test_validate_app_name_names_the_kind() -> None:
    ok, reason = validate_app_name("Bad")
    assert ok is False
    assert reason.startswith("application name 'Bad' is not a valid juju identifier")
    ok, reason = validate_app_name("")
    assert reason == "application name must be a non-empty string"


@pytest.mark.parametrize(
    "unit,ok",
    [
        ("postgresql/0", True),
        ("api-server/12", True),
        ("postgresql", False),
        ("postgresql/", False),
        ("postgresql/abc", False),
        ("/0", False),
        ("Bad/0", False),
    ],
)
def test_validate_unit_name(unit, ok) -> None:
    got, _ = validate_unit_name(unit)
    assert got is ok


def test_validate_num_units_zero_allowed_for_subordinates() -> None:
    ok, _ = validate_num_units(0, allow_zero=True)
    assert ok is True
    ok, _ = validate_num_units(0, allow_zero=False)
    assert ok is False


def test_validate_num_units_rejects_negative() -> None:
    ok, reason = validate_num_units(-1)
    assert ok is False
    assert reason == "num_units must be >= 0"
    _, reason = validate_num_units(0, allow_zero=False, name="count")
    assert reason == "count must be >= 1"


def test_validate_num_units_rejects_non_int() -> None:
    _, reason = validate_num_units("2")  # type: ignore[arg-type]
    assert reason == "num_units must be an int"


def test_validate_num_units_rejects_oversized() -> None:
    ok, reason = validate_num_units(999)
    assert ok is False
    assert "50" in reason


def test_validate_num_units_rejects_bool() -> None:
    # Sneaky: True is an int subclass. We don't want it accepted.
    ok, _ = validate_num_units(True)  # type: ignore[arg-type]
    assert ok is False


@pytest.mark.parametrize(
    "endpoint,ok",
    [
        ("postgresql", True),
        ("postgresql:db", True),
        ("api-server:database", True),
        ("Bad:endpoint", False),
        ("", False),
    ],
)
def test_validate_endpoint(endpoint, ok) -> None:
    got, _ = validate_endpoint(endpoint)
    assert got is ok
