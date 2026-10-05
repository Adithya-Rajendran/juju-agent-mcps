"""Unit tests for juju_ops._dispatch — the mutation pipeline helpers."""

from __future__ import annotations

import json

import pytest

from juju_ops._dispatch import Guard, Refused, arun_mutation, mutate
from juju_ops._results import Check


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("JUJU_OPS_AUDIT_LOG", str(log))
    monkeypatch.delenv("JUJU_OPS_AUDIT", raising=False)
    return log


def _records(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_mutate_audits_on_clean_exit(audit_log) -> None:
    with mutate("test_tool", {"a": 1}):
        pass
    [rec] = _records(audit_log)
    assert rec["tool"] == "test_tool"
    assert rec["ok"] is True
    assert rec["args"] == {"a": 1}


def test_mutate_does_not_audit_on_refused(audit_log) -> None:
    with pytest.raises(Refused):
        with mutate("test_tool", {"a": 1}) as g:
            g.fail("nope")
    # mutate() leaves the failure path to refuse(), which arun_mutation
    # calls; caught directly like this, nothing is written.
    assert not audit_log.exists()


async def test_arun_mutation_audits_success(audit_log) -> None:
    async def body(g: Guard) -> dict:
        g.check(Check.yes())
        return {"ok": True, "tool": "test_tool"}

    out = await arun_mutation("test_tool", {"a": 1}, body)
    assert out == {"ok": True, "tool": "test_tool"}
    [rec] = _records(audit_log)
    assert rec["ok"] is True


async def test_arun_mutation_converts_refused_to_dict(audit_log) -> None:
    async def body(g: Guard) -> dict:
        g.fail("nope", current=3)
        return {"ok": True}

    out = await arun_mutation("test_tool", {"a": 1}, body)
    assert out == {"ok": False, "reason": "nope", "current": 3}
    [rec] = _records(audit_log)
    assert rec["ok"] is False
    assert rec["reason"] == "nope"


async def test_arun_mutation_failed_check_short_circuits(audit_log) -> None:
    body_continued = False

    async def body(g: Guard) -> dict:
        nonlocal body_continued
        g.check(Check.no("policy refused"))
        body_continued = True
        return {"ok": True}

    out = await arun_mutation("test_tool", {"a": 1}, body)
    assert out == {"ok": False, "reason": "policy refused"}
    assert body_continued is False
    [rec] = _records(audit_log)
    assert rec["ok"] is False


async def test_arun_mutation_extra_kwargs_in_refusal(audit_log) -> None:
    async def body(g: Guard) -> dict:
        g.check(Check.no("below min"), current=2, target=1)
        return {"ok": True}

    out = await arun_mutation("test_tool", {"a": 1}, body)
    assert out == {"ok": False, "reason": "below min", "current": 2, "target": 1}


async def test_arun_mutation_turns_api_errors_into_audited_refusals(audit_log) -> None:
    async def body(g: Guard) -> dict:
        raise ConnectionRefusedError("controller unreachable")

    out = await arun_mutation("test_tool", {"a": 1}, body)
    expected = "juju API error: ConnectionRefusedError: controller unreachable"
    assert out == {"ok": False, "reason": expected}
    [rec] = _records(audit_log)
    assert rec["ok"] is False
    assert rec["reason"] == expected
