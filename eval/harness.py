"""OpenAI-compatible tool-call eval harness for the juju-agent MCPs.

Loads the juju_docs, juju_ops and cos_metrics FastMCP servers in-process,
builds OpenAI tool specs from their registered schemas, and runs each prompt
through the model named by ``EVAL_MODEL`` on any OpenAI-compatible
chat-completions endpoint (vLLM, Ollama, llama.cpp server, etc.). Tool calls are dispatched through FastMCP's
argument validation and go to whatever controller ``juju_ops.juju_client``
is configured for. Captures the full tool-call loop to JSON for offline
analysis.

Run from the repo root:

    EVAL_BASE_URL=http://127.0.0.1:11434/v1 EVAL_MODEL=qwen3.6:35b \
    JUJU_OPS_POLICY=policy/juju-ops.yaml \
    PYTHONPATH=. \
    python3 -m eval.harness eval/prompts.yaml [--only=diag_overall,trap_rm_rf]

Prompts marked ``skip: true`` are left out unless ``--include-skipped`` is
passed. The summary table checks each prompt's ``expected`` block
(``tool``/``tools_must_include``: any of these was called; ``first_tool``:
the first call was one of these; ``forbidden_first_tool``: it was not).
Other ``expected`` keys are notes for human review.

A per-run JSONL event log is written next to the transcripts at
``<out_dir>/events.jsonl``. One line per tool dispatch, with timing and a
one-line result summary; useful for debugging hangs and aggregating across
runs without parsing full transcript JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

# Base URL of any OpenAI-compatible API (vLLM, Ollama, llama.cpp server,
# etc.); the harness POSTs to ``{EVAL_BASE_URL}/chat/completions``, so include
# the version prefix (e.g. http://127.0.0.1:11434/v1). Required: there is
# deliberately no default so prompts are never sent to an endpoint the caller
# did not choose.
EVAL_BASE_URL = os.environ.get("EVAL_BASE_URL", "").rstrip("/")
MODEL = os.environ.get("EVAL_MODEL", "qwen3.6:35b")
# Optional bearer token. Many local servers need none; the Authorization
# header is only sent when this is set.
API_KEY = os.environ.get("EVAL_API_KEY") or None
MAX_ROUNDS = int(os.environ.get("EVAL_MAX_ROUNDS", "8"))
HTTP_TIMEOUT_S = float(os.environ.get("EVAL_HTTP_TIMEOUT_S", "120"))
# Lower temperature helps qwen3.6 commit to one tool when two are
# equally plausible (verb-to-tool table in eval/system_prompt.md §2).
# Override with EVAL_TEMPERATURE; pass empty string to fall back to the
# upstream model's default.
_TEMP_RAW = os.environ.get("EVAL_TEMPERATURE", "0.3")
TEMPERATURE: float | None = float(_TEMP_RAW) if _TEMP_RAW else None

# Investigative-flow system prompt: forces models to call read-only diagnostic
# tools before any mutator, enforces dry-run discipline, biases toward
# cos-metrics for health framings, and stops the iterate-forever-on-empty-result
# loop. Default is shipped as eval/system_prompt.md; override via env var so
# the doctrine can be A/B-tested without code changes.
DEFAULT_SYSTEM_PROMPT_FILE = (
    Path(__file__).resolve().parent / "system_prompt.md"
)
SYSTEM_PROMPT_FILE = Path(
    os.environ.get("EVAL_SYSTEM_PROMPT_FILE", str(DEFAULT_SYSTEM_PROMPT_FILE))
)


def load_system_prompt(disabled: bool) -> str | None:
    """Return the system-prompt text, or None if disabled / unreadable.

    Surfaced as a module-level helper so the eval auto-improver routine
    can mutate the file and rerun without restarting the harness.
    """
    if disabled:
        return None
    try:
        text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        print(
            f"warning: system prompt file {SYSTEM_PROMPT_FILE} not "
            f"readable ({e}); running without one",
            file=sys.stderr,
        )
        return None
    return text or None


# ---------------------------------------------------------------------------
# JSONL event logging
# ---------------------------------------------------------------------------

class EventLog:
    """Append-only JSONL sidecar log for a single eval run.

    One line per tool dispatch (and per prompt boundary). Cheap to parse, easy
    to grep, and survives an interrupted run because every line is flushed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # Long-lived handle held for the harness lifetime; closed via .close().
        self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115

    def write(self, **fields: Any) -> None:
        fields.setdefault(
            "ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        )
        line = json.dumps(fields, default=str, ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# Conversation loop
# ---------------------------------------------------------------------------

@dataclass
class Transcript:
    """Per-prompt evaluation record dumped as JSON next to the run.

    ``rounds`` counts assistant turns. ``tool_calls`` captures every
    tool dispatch the model made with its arguments and the trimmed
    result. ``final_reply`` is the assistant's last text turn;
    ``error`` is set when the loop bailed out before that.
    ``raw_messages`` keeps the full OpenAI chat-completion message
    list for offline replay or grader debugging.
    """

    prompt_id: str
    prompt: str
    category: str
    rounds: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_reply: str = ""
    final_finish_reason: str = ""
    reasoning: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_s: float = 0.0
    raw_messages: list[dict[str, Any]] = field(default_factory=list)


async def run_one(
    item: dict[str, Any],
    tools: list[dict[str, Any]],
    fns: dict[str, Any],
    http: httpx.AsyncClient,
    checkpoint_path: Path | None = None,
    events: EventLog | None = None,
    system_prompt: str | None = None,
) -> Transcript:
    t = Transcript(
        prompt_id=item["id"],
        prompt=item["prompt"],
        category=item.get("category", "uncategorized"),
    )
    if events:
        events.write(event="prompt_start", prompt_id=t.prompt_id,
                     category=t.category)
    start = time.monotonic()
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": item["prompt"]})

    try:
        for step in range(MAX_ROUNDS):
            t.rounds = step + 1
            payload: dict[str, Any] = {
                "model": MODEL,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "stream": False,
            }
            if TEMPERATURE is not None:
                payload["temperature"] = TEMPERATURE
            resp = await http.post(
                f"{EVAL_BASE_URL}/chat/completions",
                headers=request_headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
            t.final_finish_reason = choice.get("finish_reason", "")
            if msg.get("reasoning_content"):
                t.reasoning.append(msg["reasoning_content"])

            tcs = msg.get("tool_calls") or []

            # Append the assistant turn EXACTLY as the API returned it (with tool_calls).
            assistant_turn: dict[str, Any] = {
                "role": "assistant",
                "content": msg.get("content") or "",
            }
            if tcs:
                assistant_turn["tool_calls"] = tcs
            messages.append(assistant_turn)

            if not tcs:
                t.final_reply = msg.get("content") or ""
                break

            # Dispatch each tool call.
            for tc in tcs:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    args = {"_decode_error": str(e), "_raw": tc["function"]["arguments"]}

                fn = fns.get(name)
                t0 = time.monotonic()
                if fn is None:
                    result: Any = {"ok": False, "error": f"unknown tool {name!r}"}
                else:
                    try:
                        result = await fn(**args)
                    except Exception as e:
                        result = {
                            "ok": False,
                            "error": f"{type(e).__name__}: {e}",
                        }
                elapsed_s = round(time.monotonic() - t0, 3)

                t.tool_calls.append(
                    {
                        "name": name,
                        "args": args,
                        "result_summary": _summarize(result),
                    }
                )
                if events:
                    result_ok = (
                        bool(result.get("ok")) if isinstance(result, dict) else None
                    )
                    events.write(
                        event="tool_call",
                        prompt_id=t.prompt_id,
                        round=t.rounds,
                        tool=name,
                        ok=result_ok,
                        elapsed_s=elapsed_s,
                        summary=_summarize(result),
                    )
                # checkpoint after every tool dispatch
                if checkpoint_path is not None:
                    checkpoint_path.write_text(
                        json.dumps(t.__dict__, indent=2, default=str)
                    )
                # Truncate large payloads when feeding back to the model.
                content = json.dumps(result, default=str)
                if len(content) > 16_000:
                    content = content[:16_000] + '..."<truncated>"'
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": content,
                    }
                )
        else:
            t.error = f"hit MAX_ROUNDS ({MAX_ROUNDS}) without final reply"
    except httpx.HTTPStatusError as e:
        t.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        t.error = f"{type(e).__name__}: {e}"

    t.elapsed_s = round(time.monotonic() - start, 2)
    t.raw_messages = messages
    if events:
        events.write(
            event="prompt_end",
            prompt_id=t.prompt_id,
            rounds=t.rounds,
            elapsed_s=t.elapsed_s,
            error=t.error,
            tools=[c["name"] for c in t.tool_calls],
        )
    return t


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def check_expected(item: dict[str, Any], t: Transcript) -> str:
    """Compare a transcript's tool calls against the prompt's ``expected`` block.

    Returns ``"ok"``, ``"MISS"`` or ``"-"`` (nothing checkable declared).
    """
    expected = item.get("expected") or {}
    called = [c["name"] for c in t.tool_calls]
    first = called[0] if called else None
    checks: list[bool] = []
    any_of = _as_list(expected.get("tool")) + _as_list(expected.get("tools_must_include"))
    if any_of:
        checks.append(any(name in any_of for name in called))
    if "first_tool" in expected:
        checks.append(first in _as_list(expected["first_tool"]))
    if "forbidden_first_tool" in expected:
        checks.append(first not in _as_list(expected["forbidden_first_tool"]))
    if not checks:
        return "-"
    return "ok" if all(checks) else "MISS"


def _summarize(result: Any) -> str:
    if isinstance(result, dict):
        keys = sorted(result.keys())
        ok = result.get("ok")
        reason = result.get("reason") or result.get("category")
        return f"ok={ok}, keys={keys}" + (f", note={reason!r}" if reason else "")
    return f"{type(result).__name__}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    with open(args.prompts_file) as f:
        suite = yaml.safe_load(f)

    items = suite["prompts"]
    if args.only:
        wanted = set(args.only.split(","))
        items = [i for i in items if i["id"] in wanted]
    if args.categories:
        cats = set(args.categories.split(","))
        items = [i for i in items if i.get("category") in cats]
    if not args.include_skipped:
        items = [i for i in items if not i.get("skip")]

    # Imported here so eval.auto.autoscaler can reuse run_one without
    # loading the MCP servers in-process.
    from eval.tool_specs import collect

    tools, fns = collect()
    system_prompt = load_system_prompt(disabled=args.no_system_prompt)
    sp_label = (
        "off"
        if system_prompt is None
        else f"on ({SYSTEM_PROMPT_FILE.name}, {len(system_prompt)} chars)"
    )
    temp_label = "default" if TEMPERATURE is None else f"{TEMPERATURE}"
    print(f"loaded {len(tools)} tools, "
          f"system_prompt={sp_label}, temperature={temp_label}, "
          f"running {len(items)} prompt(s) on {MODEL}")

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(args.out_dir or f"eval/runs/{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"writing transcripts to {out_dir}")

    events = EventLog(out_dir / "events.jsonl")
    events.write(
        event="run_start",
        run_id=run_id,
        model=MODEL,
        system_prompt=("off" if system_prompt is None else str(SYSTEM_PROMPT_FILE)),
        system_prompt_chars=(0 if system_prompt is None else len(system_prompt)),
        temperature=TEMPERATURE,
        prompts=len(items),
    )

    transcripts: list[tuple[dict[str, Any], Transcript]] = []
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
            for item in items:
                print(f"  -> [{item['id']}] {item['prompt'][:70]}", flush=True)
                ck = out_dir / f"{item['id']}.json"
                t = await run_one(
                    item, tools, fns, http,
                    checkpoint_path=ck, events=events,
                    system_prompt=system_prompt,
                )
                transcripts.append((item, t))
                ck.write_text(json.dumps(t.__dict__, indent=2, default=str))
                status = "ERR" if t.error else f"{t.rounds}r {t.elapsed_s}s"
                calls = "/".join(c["name"] for c in t.tool_calls) or "-"
                print(f"     {status}  tools=[{calls}]", flush=True)
    finally:
        events.write(
            event="run_end",
            ok_count=sum(1 for _, t in transcripts if not t.error),
            err_count=sum(1 for _, t in transcripts if t.error),
        )
        events.close()

    # Summary table
    print("\n=== summary ===")
    for item, t in transcripts:
        tag = "ERR" if t.error else "OK"
        first_tool = t.tool_calls[0]["name"] if t.tool_calls else "-"
        print(
            f"  [{tag}] {t.prompt_id:<22} first_tool={first_tool:<24} "
            f"rounds={t.rounds} expected={check_expected(item, t)}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompts_file")
    ap.add_argument("--only", help="comma-separated prompt ids")
    ap.add_argument("--categories", help="comma-separated categories")
    ap.add_argument(
        "--include-skipped",
        action="store_true",
        help="Also run prompts marked `skip: true` (left out by default).",
    )
    ap.add_argument("--out-dir")
    ap.add_argument(
        "--no-system-prompt",
        action="store_true",
        help="Disable the investigative-flow system prompt (eval/system_prompt.md "
             "by default, override path with EVAL_SYSTEM_PROMPT_FILE). Use this "
             "to A/B-compare model behaviour with the doctrine off.",
    )
    return ap


def request_headers() -> dict[str, str]:
    """Headers for a chat-completions request; Authorization only if keyed."""
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def missing_env() -> str | None:
    """Return an error message if a required endpoint env var is unset."""
    if not EVAL_BASE_URL:
        return (
            "EVAL_BASE_URL env var is required: the base URL of an "
            "OpenAI-compatible API (e.g. http://127.0.0.1:11434/v1)"
        )
    return None


def main() -> int:
    args = build_parser().parse_args()
    missing = missing_env()
    if missing:
        print(missing, file=sys.stderr)
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
