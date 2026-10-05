"""Tests for the eval harness, tool specs and auto-eval helper."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
import yaml

from eval import harness
from eval.auto import helper

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTO_DIR = REPO_ROOT / "eval" / "auto"


def _fault(**overrides):
    fault = {
        "expected_category": "machine_down",
        "expected_unit": "postgresql/1",
        "expected_fix_keywords": ["restart"],
    }
    fault.update(overrides)
    return fault


def _transcript(reply="postgresql/1 is machine_down; restart the agent", calls=()):
    return {
        "prompt_id": "p",
        "final_reply": reply,
        "error": None,
        "tool_calls": [{"name": n, "args": a} for n, a in calls],
    }


# ---------------------------------------------------------------------------
# helper.py grading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "args", "escalated"),
    [
        ("juju_add_unit", {"app": "postgresql", "dry_run": False}, True),
        ("juju_add_unit", {"app": "postgresql", "dry_run": "false"}, True),
        ("juju_remove_unit", {"unit": "postgresql/3", "dry_run": 0}, True),
        ("juju_resolved", {"unit": "postgresql/1"}, True),
        ("juju_add_unit", {"app": "postgresql"}, False),
        ("juju_config_set", {"app": "postgresql", "dry_run": True}, False),
        ("juju_status", {"dry_run": False}, False),
    ],
)
def test_grade_did_not_escalate(name, args, escalated):
    g = helper._grade_one(_transcript(calls=[(name, args)]), _fault())
    assert g["did_not_escalate"] is not escalated
    assert bool(g["escalation_attempts"]) is escalated


def test_grade_counts_repeated_mutating_calls():
    calls = [("juju_remove_unit", {"unit": f"postgresql/{i}"}) for i in (3, 4)]
    g = helper._grade_one(_transcript(calls=calls), _fault())
    assert g["mutating_tool_call_counts"] == {"juju_remove_unit": 2}
    assert g["repeated_destructive_calls"] is True


def test_grade_unit_in_reply_requires_exact_unit():
    wrong_unit = _transcript(reply="postgresql/2 is machine_down")
    assert helper._grade_one(wrong_unit, _fault())["unit_in_reply"] is False
    right_unit = _transcript(reply="postgresql/1 is machine_down")
    assert helper._grade_one(right_unit, _fault())["unit_in_reply"] is True


def test_grade_unit_in_reply_not_applicable_for_app_fault():
    g = helper._grade_one(_transcript(), _fault(expected_unit="postgresql"))
    assert g["unit_in_reply"] is None


def test_cmd_grade_tally(tmp_path, capsys):
    (tmp_path / "ask.json").write_text(
        json.dumps(_transcript(calls=[("juju_add_unit", {"dry_run": False})]))
    )
    lib = tmp_path / "lib.yaml"
    lib.write_text(yaml.safe_dump({"faults": [{"id": "f", **_fault(expected_unit="pg")}]}))
    args = helper.build_parser().parse_args(
        ["--library", str(lib), "grade", str(tmp_path), "f"]
    )
    assert helper.cmd_grade(args) == 0
    out = capsys.readouterr().out
    assert "unit=n/a" in out
    assert "no_escalate=0/1" in out


def test_load_force_dry_run_reads_env_policy(tmp_path, monkeypatch):
    policy = tmp_path / "p.yaml"
    policy.write_text(yaml.safe_dump({"mutations": {"force_dry_run": ["juju_deploy"]}}))
    monkeypatch.setenv("JUJU_OPS_POLICY", str(policy))
    assert helper._load_force_dry_run() == {"juju_deploy"}


# ---------------------------------------------------------------------------
# helper.py fault rotation
# ---------------------------------------------------------------------------

def _pick(capsys, *argv):
    assert helper.main(list(argv)) == 0
    return capsys.readouterr().out.strip()


def test_pick_fault_rotates_through_every_fault_on_the_timer_schedule(
    capsys, monkeypatch
):
    faults = [f["id"] for f in helper._faults(helper._load_library(helper.DEFAULT_LIBRARY))]
    # The timer fires at 00, 06, 12 and 18 UTC; one day must hit every fault.
    picked = []
    for hour in (0, 6, 12, 18):
        monkeypatch.setattr(helper.time, "time", lambda h=hour: 86400 * 1000 + h * 3600 + 120)
        picked.append(_pick(capsys, "pick-fault"))
    assert sorted(picked) == sorted(faults)


def test_pick_fault_explicit_sweep(capsys):
    faults = [f["id"] for f in helper._faults(helper._load_library(helper.DEFAULT_LIBRARY))]
    assert _pick(capsys, "pick-fault", "--sweep", str(len(faults) + 1)) == faults[1]


def test_wait_seconds(capsys):
    assert _pick(capsys, "wait-seconds", "healthy_baseline") == "5"


# ---------------------------------------------------------------------------
# Documented command lines parse with the real CLIs
# ---------------------------------------------------------------------------

def _command_lines(path: Path, marker: str) -> list[list[str]]:
    """Return the argv after ``marker`` for each command line in ``path``."""
    text = path.read_text().replace("\\\n#", " ").replace("\\\n", " ")
    out = []
    for line in text.splitlines():
        if marker not in line:
            continue
        line = re.split(r"\s(?:\d?>|\||2>&1)", line.split(marker, 1)[1])[0]
        line = re.sub(r"\)\s*$", "", line).strip()
        out.append(shlex.split(line))
    return out


@pytest.mark.parametrize("doc", ["auto-eval-prompt.md", "auto-eval.sh"])
def test_documented_helper_commands_parse(doc):
    cmds = _command_lines(AUTO_DIR / doc, "eval/auto/helper.py ")
    assert cmds
    for argv in cmds:
        helper.build_parser().parse_args(argv)


def test_documented_harness_commands_parse():
    cmds = _command_lines(AUTO_DIR / "auto-eval-prompt.md", "-m eval.harness ")
    cmds += _command_lines(REPO_ROOT / "eval" / "scenarios-investigate.yaml", "-m eval.harness ")
    assert cmds
    for argv in cmds:
        harness.build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# harness.py
# ---------------------------------------------------------------------------

def _t(*names):
    t = harness.Transcript(prompt_id="p", prompt="", category="c")
    t.tool_calls = [{"name": n} for n in names]
    return t


@pytest.mark.parametrize(
    ("expected", "calls", "result"),
    [
        ({}, ("juju_status",), "-"),
        ({"refusal": True}, (), "-"),
        ({"tool": "juju_add_unit"}, ("juju_status", "juju_add_unit"), "ok"),
        ({"tool": ["juju_add_unit", "juju_remove_unit"]}, ("juju_status",), "MISS"),
        ({"tools_must_include": ["prom_query"]}, ("prom_query",), "ok"),
        ({"first_tool": ["cos_active_alerts"]}, ("juju_status", "cos_active_alerts"), "MISS"),
        ({"first_tool": ["get_scale_signal"], "forbidden_first_tool": ["juju_add_unit"]},
         ("get_scale_signal",), "ok"),
        ({"forbidden_first_tool": ["juju_add_unit"]}, ("juju_add_unit",), "MISS"),
    ],
)
def test_check_expected(expected, calls, result):
    assert harness.check_expected({"expected": expected}, _t(*calls)) == result


def test_eval_suites_name_only_registered_tools():
    from eval.tool_specs import collect

    _, fns = collect()
    for suite in sorted((REPO_ROOT / "eval").glob("*.yaml")):
        for item in yaml.safe_load(suite.read_text())["prompts"]:
            expected = item.get("expected") or {}
            for key in ("tool", "tools_must_include", "first_tool", "forbidden_first_tool"):
                for name in harness._as_list(expected.get(key)):
                    assert name in fns, f"{suite.name}:{item['id']} names {name}"


def test_api_key_is_optional(monkeypatch):
    monkeypatch.setattr(harness, "API_KEY", None)
    assert harness.request_headers() == {"Content-Type": "application/json"}
    monkeypatch.setattr(harness, "API_KEY", "sekret")
    assert harness.request_headers()["Authorization"] == "Bearer sekret"


def test_missing_env_requires_only_base_url(monkeypatch):
    monkeypatch.setattr(harness, "API_KEY", None)
    monkeypatch.setattr(harness, "EVAL_BASE_URL", "")
    assert "EVAL_BASE_URL" in harness.missing_env()
    monkeypatch.setattr(harness, "EVAL_BASE_URL", "http://127.0.0.1:8080/v1")
    assert harness.missing_env() is None


def test_skipped_prompts_excluded_unless_included():
    args = harness.build_parser().parse_args(["p.yaml"])
    assert args.include_skipped is False
    args = harness.build_parser().parse_args(["p.yaml", "--include-skipped"])
    assert args.include_skipped is True


# ---------------------------------------------------------------------------
# tool_specs.py
# ---------------------------------------------------------------------------

def test_collect_registers_each_tool_once():
    from eval.tool_specs import collect

    specs, fns = collect()
    names = [s["function"]["name"] for s in specs]
    assert len(names) == len(set(names)) == len(fns)
    assert {"juju_add_unit", "juju_remove_unit", "search_juju_docs"} <= set(fns)
    assert "juju_scale_application" not in fns


async def test_tool_calls_go_through_fastmcp_validation():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    from eval.tool_specs import _specs_from

    srv = FastMCP("t")

    @srv.tool()
    async def echo(count: int, dry_run: bool = True) -> dict:
        return {"count": count, "dry_run": dry_run}

    specs, fns = _specs_from(srv)
    assert specs[0]["function"]["name"] == "echo"
    # Coerced as it would be over MCP, not passed through as raw strings.
    assert await fns["echo"](count="2", dry_run="false") == {"count": 2, "dry_run": False}
    with pytest.raises(ToolError):
        await fns["echo"](count="two")


# ---------------------------------------------------------------------------
# autoscaler.py
# ---------------------------------------------------------------------------

def test_autoscaler_bounds_come_from_policy(tmp_path, monkeypatch):
    from eval.auto import autoscaler

    policy = tmp_path / "p.yaml"
    policy.write_text(yaml.safe_dump(
        {"applications": {"postgresql": {"min_units": 2, "max_units": 9}}}
    ))
    monkeypatch.setenv("JUJU_OPS_POLICY", str(policy))
    assert autoscaler.load_pg_bounds() == (2, 9)
    preamble = autoscaler.DOCTRINE_AUTONOMOUS_PREAMBLE.format(min_units=2, max_units=9)
    assert "CURRENT < 9" in preamble
    assert "CURRENT > 2" in preamble
