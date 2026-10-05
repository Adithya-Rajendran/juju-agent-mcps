"""Tests for juju_ops.audit — server-side mutation audit log."""

from __future__ import annotations

import json
from pathlib import Path

from juju_ops.audit import audit_mutation, refuse


def test_writes_to_file_when_path_set(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("JUJU_OPS_AUDIT_LOG", str(log))
    monkeypatch.delenv("JUJU_OPS_AUDIT", raising=False)

    audit_mutation("juju_resolved", {"unit": "api/0"}, ok=True)
    audit_mutation(
        "juju_config_set",
        {"app": "rogue", "key": "x", "value": "y"},
        ok=False,
        reason="application 'rogue' is not in the policy",
    )

    lines = log.read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["tool"] == "juju_resolved"
    assert rec["ok"] is True
    assert "ts" in rec
    rec2 = json.loads(lines[1])
    assert rec2["ok"] is False
    assert "rogue" in rec2["reason"]


def test_off_switch_disables_writes(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("JUJU_OPS_AUDIT_LOG", str(log))
    monkeypatch.setenv("JUJU_OPS_AUDIT", "off")

    audit_mutation("juju_resolved", {"unit": "api/0"}, ok=True)
    assert not log.exists()


def test_audit_is_resilient_to_oserror(tmp_path: Path, monkeypatch) -> None:
    # Point at a path inside a non-existent directory; the writer must swallow.
    monkeypatch.setenv(
        "JUJU_OPS_AUDIT_LOG", str(tmp_path / "no" / "such" / "dir" / "audit.jsonl"),
    )
    monkeypatch.delenv("JUJU_OPS_AUDIT", raising=False)

    # Must not raise.
    audit_mutation("juju_resolved", {"unit": "api/0"}, ok=True)


def test_default_destination_is_stderr(monkeypatch, capsys) -> None:
    monkeypatch.delenv("JUJU_OPS_AUDIT_LOG", raising=False)
    monkeypatch.delenv("JUJU_OPS_AUDIT", raising=False)
    audit_mutation("juju_resolved", {"unit": "api/0"}, ok=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    rec = json.loads(captured.err.strip())
    assert rec["tool"] == "juju_resolved"


def test_refuse_audits_and_returns_refusal(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("JUJU_OPS_AUDIT_LOG", str(log))
    monkeypatch.delenv("JUJU_OPS_AUDIT", raising=False)

    out = refuse("juju_add_unit", {"app": "api"}, "too many", current=6)
    assert out == {"ok": False, "reason": "too many", "current": 6}
    rec = json.loads(log.read_text())
    assert (rec["tool"], rec["ok"], rec["reason"]) == ("juju_add_unit", False, "too many")
