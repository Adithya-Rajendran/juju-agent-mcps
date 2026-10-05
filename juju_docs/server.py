"""juju-docs MCP server: thin wrapper over the Juju documentation search API.

Exposes a single tool, ``search_juju_docs``, that queries the Read the Docs
search backend serving https://documentation.ubuntu.com/juju/3.6/.

The site is hosted under the Read the Docs project slug ``canonical-juju``;
the search endpoint is ``/_/api/v2/search/`` on the same domain. Each result
carries a ``juju_version_hint`` derived from the docs version (e.g. "3.6"
becomes "juju-3") and a ``category`` derived from the Diataxis section in the
URL path ("tutorial" | "howto" | "reference" | "explanation").

No vector embeddings, no custom RAG -- just thin search.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DOCS_BASE = os.environ.get("JUJU_DOCS_BASE", "https://documentation.ubuntu.com")
DOCS_PROJECT = os.environ.get("JUJU_DOCS_PROJECT", "canonical-juju")
DOCS_VERSION = os.environ.get("JUJU_DOCS_VERSION", "3.6")
USER_AGENT = (
    "juju-docs-mcp/0.0.1 (+https://github.com/Adithya-Rajendran/juju-agent-mcps)"
)
HTTP_TIMEOUT_S = float(os.environ.get("JUJU_DOCS_HTTP_TIMEOUT_S", "10"))

SEARCH_PATH = "/_/api/v2/search/"

# Match the leading integer of a version string ("3.6" -> "3", "4" -> "4").
_VERSION_RE = re.compile(r"^(\d+)(?:\.|$)")
# Pull the Diataxis section out of "/juju/3.6/<section>/...".
_CATEGORY_RE = re.compile(r"^/[^/]+/[^/]+/([^/]+)/")
# Strip Read the Docs' highlight <span>...</span> wrapper so excerpts are plain text.
_HIGHLIGHT_TAG_RE = re.compile(r"</?span>")

mcp = FastMCP("juju-docs")


def _extract_version_hint(version: str | None) -> str | None:
    """Map an RTD version string (e.g. "3.6") to ``"juju-N"``.

    Returns ``None`` for non-numeric versions like ``"latest"`` or ``"stable"``.
    """
    if not version:
        return None
    m = _VERSION_RE.match(version)
    return f"juju-{m.group(1)}" if m else None


def _extract_category(path: str | None) -> str | None:
    """Pull the Diataxis section out of a docs path.

    ``/juju/3.6/howto/manage-applications/`` -> ``"howto"``
    ``/juju/3.6/reference/relation/``        -> ``"reference"``

    Returns ``None`` if the path doesn't fit the project/version/section
    pattern (e.g. the project landing page).
    """
    if not path:
        return None
    m = _CATEGORY_RE.match(path)
    return m.group(1) if m else None


def _excerpt_from_blocks(blocks: list[dict[str, Any]]) -> str:
    """Pick the best excerpt from a result's ``blocks`` array.

    Prefers the first highlighted content snippet (RTD wraps matches in
    ``<span>`` tags, which we strip), falling back to raw block content.
    Returns an empty string if nothing usable is present.
    """
    if not blocks:
        return ""
    first = blocks[0] or {}
    highlights = (first.get("highlights") or {}).get("content") or []
    excerpt = highlights[0] if highlights else first.get("content", "")
    return _HIGHLIGHT_TAG_RE.sub("", excerpt or "")


def _shape_result(item: dict[str, Any]) -> dict[str, Any]:
    """Map a Read the Docs search result to our compact result shape."""
    title = item.get("title", "")
    path = item.get("path", "")
    domain = item.get("domain") or DOCS_BASE
    url = f"{domain}{path}" if path else ""
    excerpt = _excerpt_from_blocks(item.get("blocks") or []) or title
    return {
        "title": title,
        "url": url,
        "excerpt": excerpt,
        "category": _extract_category(path),
        "juju_version_hint": _extract_version_hint(item.get("version")),
    }


@mcp.tool()
async def search_juju_docs(
    query: str,
    category: str | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """Search the Juju documentation at documentation.ubuntu.com/juju/.

    Args:
        query: Free-text query (e.g. "add a relation between two charms").
        category: Optional Diataxis-section filter -- case-insensitive
            substring match against ``"tutorial"``, ``"howto"``,
            ``"reference"``, or ``"explanation"``.
        max_results: Cap on the number of returned results. Defaults to 5.

    Returns:
        ``{ok: true, results: [...]}`` where each result is a
        ``{title, url, excerpt, category, juju_version_hint}`` dict, ordered
        by the search backend's relevance ranking. An empty ``results`` list
        means nothing matched. ``juju_version_hint`` is derived from the
        configured docs version (e.g. ``"juju-3"`` for the 3.x docs) and is
        ``None`` when the version is non-numeric (e.g. ``"latest"``).

        ``{ok: false, reason}`` when the query is invalid or the docs search
        is unreachable -- report the outage rather than rephrasing and
        retrying.
    """
    if not query or not query.strip():
        return {"ok": False, "reason": "query is required"}
    if max_results < 1:
        return {"ok": False, "reason": "max_results must be >= 1"}

    params = {
        "q": query.strip(),
        "project": DOCS_PROJECT,
        "version": DOCS_VERSION,
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            resp = await client.get(
                f"{DOCS_BASE}{SEARCH_PATH}", params=params, headers=headers
            )
    except httpx.HTTPError as e:
        return {"ok": False, "reason": f"docs search: {type(e).__name__}: {e}"}

    if resp.status_code != 200:
        return {"ok": False, "reason": f"docs search: HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"ok": False, "reason": "docs search: invalid JSON response"}

    items = data.get("results") or []
    results = [_shape_result(it) for it in items]

    if category:
        needle = category.lower()
        results = [
            r for r in results
            if r["category"] and needle in r["category"].lower()
        ]

    return {"ok": True, "results": results[:max_results]}


# FastMCP enables DNS-rebinding protection (a loopback-only Host allowlist)
# only when constructed with one of these hosts; see main().
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="juju-docs-mcp",
        description="juju-docs MCP server: Juju documentation search.",
    )
    ap.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio (default) for a client that spawns the server, or "
        "streamable-http to serve MCP at http://HOST:PORT/mcp",
    )
    ap.add_argument(
        "--host", default="127.0.0.1",
        help="bind address for streamable-http (default: %(default)s)",
    )
    ap.add_argument(
        "--port", type=int, default=8001,
        help="listen port for streamable-http (default: %(default)s)",
    )
    return ap


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint: serve over stdio or streamable HTTP."""
    args = build_parser().parse_args(argv)
    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if args.host not in _LOOPBACK_HOSTS:
            # Match what FastMCP(host=...) would do: the loopback-only Host
            # allowlist set at construction would reject every remote client.
            mcp.settings.transport_security = None
            print(
                f"juju-docs-mcp: warning: listening on {args.host}:{args.port} "
                "without authentication",
                file=sys.stderr,
            )
    mcp.run(transport=args.transport)


if __name__ == "__main__":  # pragma: no cover
    main()
