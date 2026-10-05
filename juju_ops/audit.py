"""Server-side audit log for mutation tool invocations.

Every mutation tool emits a single JSON line per call:
``{ts, tool, args, ok, reason}``. The log is the only durable record of
what an LLM tried to do and whether policy let it through.

Destination resolution (first match wins):
  1. ``JUJU_OPS_AUDIT_LOG`` -- absolute path to a JSONL file (appended).
  2. otherwise, stderr.

Disable entirely with ``JUJU_OPS_AUDIT=off``. The function never raises;
audit failures are swallowed because they must never break a tool call.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from ._responses import RefusalResponse


def audit_mutation(
    tool: str,
    args: dict[str, Any],
    *,
    ok: bool,
    reason: str | None = None,
) -> None:
    """Append one audit record. Never raises."""
    if os.environ.get("JUJU_OPS_AUDIT", "on").lower() == "off":
        return
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool,
        "args": args,
        "ok": bool(ok),
        "reason": reason,
    }
    line = json.dumps(record, default=str, ensure_ascii=False)
    path = os.environ.get("JUJU_OPS_AUDIT_LOG")
    try:
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        else:
            print(line, file=sys.stderr, flush=True)
    except OSError:
        # Best-effort. Audit must never break a tool call.
        pass


def refuse(
    tool: str,
    args: dict[str, Any],
    reason: str,
    **extra: Any,
) -> RefusalResponse:
    """Audit a refused mutation and return the standard refusal dict.

    Any keyword arguments beyond the ones named here are folded into the
    returned dict, so callers can attach context like ``current=N``.
    """
    audit_mutation(tool, args, ok=False, reason=reason)
    return {"ok": False, "reason": reason, **extra}


__all__ = ["audit_mutation", "refuse"]
