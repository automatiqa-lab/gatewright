"""Minimal synchronous MCP client call over stdio. Used by MCP-backed adapters.

Connection config: `{command, args, [cwd]}` over stdio. The server runs in `cwd` (default: the
schedule folder), never in Gatewright's own working directory.
"""

from __future__ import annotations

import json
from typing import Any

import anyio


def call_tool(config: dict[str, Any], tool: str, args: dict[str, Any]) -> Any:
    """Open a stdio session, call one tool, return its JSON (or text) result."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = config.get("command")
    if not command:
        raise RuntimeError("MCP connection needs config.command (stdio); this client speaks stdio only")
    from pathlib import Path

    cwd = Path(str(config.get("cwd") or config.get("folder") or Path.home())).resolve()
    params = StdioServerParameters(command=command, args=[str(a) for a in config.get("args") or []], cwd=str(cwd))

    async def run() -> Any:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            if getattr(result, "is_error", None) or getattr(result, "isError", None):
                raise RuntimeError(f"{tool}: {result.content}")
            sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
            if sc:
                return sc.get("result", sc) if isinstance(sc, dict) else sc
            text = "".join(getattr(c, "text", "") for c in result.content)
            try:
                return json.loads(text)
            except ValueError:
                return text

    return anyio.run(run)
