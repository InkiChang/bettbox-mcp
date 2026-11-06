"""Protocol-level test: launch the server over stdio and talk MCP to it.

This is the closest thing to how a real client (Claude Desktop, DSH, Cursor)
sees the server, so it catches transport and handshake problems that the
in-process tests cannot.

Run with:  uv run python tests/stdio_protocol.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bettbox_mcp"],
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"server: {init.server_info.name} v{init.server_info.version}")
            print("protocol:", getattr(init, "protocol_version", getattr(init, "protocolVersion", "?")))

            tools = await session.list_tools()
            print(f"tools: {len(tools.tools)}")
            for tool in tools.tools:
                hints = tool.annotations
                flags = []
                if hints:
                    if hints.read_only_hint:
                        flags.append("read-only")
                    if hints.destructive_hint:
                        flags.append("destructive")
                print(f"  - {tool.name} [{', '.join(flags) or 'write'}]")

            print("\ncalling bettbox_get_status through the protocol...")
            result = await session.call_tool("bettbox_get_status", {"params": {}})
            for block in result.content:
                text = getattr(block, "text", "")
                for line in text.splitlines()[:10]:
                    print(f"    {line}")
            failed = bool(getattr(result, "is_error", getattr(result, "isError", False)))
            print(f"\n{'FAILED' if failed else 'stdio transport OK'}")
            return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


