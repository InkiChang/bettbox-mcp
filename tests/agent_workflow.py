"""End-to-end composite workflow through the real MCP client.

Simulates what an agent would do for a realistic request:

    "看看当前状态，在香港分组里找出延迟最低的节点并切过去，然后确认生效。"

Every step is a real MCP tool call over stdio, so this exercises tool
composition, not just unit behaviour.

Run with:  uv run python tests/agent_workflow.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def text_of(result) -> str:
    return "\n".join(
        getattr(block, "text", "") for block in result.content if getattr(block, "text", None)
    )


async def call(session: ClientSession, tool: str, args: dict) -> str:
    print(f"\n>>> {tool}({json.dumps(args, ensure_ascii=False)})")
    result = await session.call_tool(tool, args)
    payload = text_of(result)
    for line in payload.splitlines()[:14]:
        print(f"    {line}")
    if len(payload.splitlines()) > 14:
        print(f"    ... ({len(payload.splitlines())} lines total)")
    return payload


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "bettbox_mcp"], cwd=str(ROOT)
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. Ground truth first.
            status = await call(session, "bettbox_get_status", {"params": {}})
            if status.startswith("Error:"):
                print("\nstatus failed; aborting")
                return 1

            # 2. Find a Hong Kong group.
            groups = await call(
                session, "bettbox_list_proxy_groups", {"params": {"response_format": "json"}}
            )
            parsed = json.loads(groups)
            hk = next(
                (g for g in parsed["groups"] if "HK" in g["name"] or "香港" in g["name"]),
                None,
            )
            if not hk:
                print("\nno HK-like group found")
                return 1
            print(f"\n--- target group: {hk['name']} (currently {hk['now']})")

            # 3. Measure every member; the reply is a markdown table.
            delay_report = await call(
                session,
                "bettbox_test_delay",
                {"params": {"name": hk["name"], "kind": "group", "timeout_ms": 5000}},
            )
            measured = []
            for line in delay_report.splitlines():
                match = re.match(r"\|\s*(.+?)\s*\|\s*(\d+)\s*ms\s*\|", line)
                if match:
                    measured.append((match.group(1), int(match.group(2))))
            if not measured:
                print("\ncould not parse any latency from the report")
                return 1
            fastest = min(measured, key=lambda item: item[1])
            print(f"\n--- fastest: {fastest[0]} ({fastest[1]} ms) of {len(measured)} measured")

            original = hk["now"]
            if fastest[0] == original:
                runner_up = next((m for m in measured if m[0] != original), None)
                if not runner_up:
                    print("\nalready on the only node; nothing to switch")
                    return 0
                fastest = runner_up
                print(f"--- already on the fastest; switching to {fastest[0]} to prove the path")

            # 4. Switch.
            switch = await call(
                session,
                "bettbox_select_proxy",
                {"params": {"group": hk["name"], "proxy": fastest[0]}},
            )
            if switch.startswith("Error:"):
                print("\nswitch failed")
                return 1

            # 5. Verify independently by re-reading the group.
            verify = await call(
                session,
                "bettbox_list_proxies",
                {"params": {"group": hk["name"], "response_format": "json"}},
            )
            now = json.loads(verify)["now"]
            print(f"\n--- verification: group now on {now!r}")
            ok = now == fastest[0]
            if not ok:
                print("!!! switch did not stick")

            # 6. Restore so the machine is left as it was found.
            print("\n=== restoring original node ===")
            await call(
                session,
                "bettbox_select_proxy",
                {"params": {"group": hk["name"], "proxy": original}},
            )

            print(f"\n{'WORKFLOW OK' if ok else 'WORKFLOW FAILED'}")
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
