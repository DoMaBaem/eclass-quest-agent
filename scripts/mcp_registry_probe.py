"""한 stdio MCP의 Tool 이름만 출력하는 격리 probe."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def probe(module: str) -> int:
    parameters = StdioServerParameters(command=sys.executable, args=["-m", module], cwd=PROJECT_ROOT)
    try:
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
        print(json.dumps(tools, separators=(",", ":")))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"mcp_server.server", "document_mcp_server.server"}:
        raise SystemExit(64)
    raise SystemExit(asyncio.run(probe(sys.argv[1])))
