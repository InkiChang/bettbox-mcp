"""Smoke test: exercise every read-only tool against a live Bettbox.

Run with:  uv run python tests/smoke.py

Destructive tools are intentionally NOT called here; they need a human
supervising because they restart the app or rewrite config.json.
"""

from __future__ import annotations

import asyncio
import sys

from bettbox_mcp.models import (
    ListConnectionsInput,
    ListProxiesInput,
    ListProxyGroupsInput,
    ListSubscriptionsInput,
    StatusInput,
    TestDelayInput,
)
from bettbox_mcp.server import (
    bettbox_get_status,
    bettbox_list_connections,
    bettbox_list_proxies,
    bettbox_list_proxy_groups,
    bettbox_list_subscriptions,
    bettbox_test_delay,
    mcp,
)


async def main() -> int:
    tools = await mcp.list_tools()
    print(f"registered tools ({len(tools)}):")
    for tool in tools:
        print(f"  - {tool.name}")
    print()

    failures = 0

    def show(name: str, output: str) -> None:
        nonlocal failures
        bad = output.startswith("Error:")
        if bad:
            failures += 1
        banner = "FAIL" if bad else "ok  "
        print(f"[{banner}] {name}")
        for line in output.splitlines()[:12]:
            print(f"        {line}")
        print()

    show("bettbox_get_status", await bettbox_get_status(StatusInput()))
    show("bettbox_list_subscriptions", await bettbox_list_subscriptions(ListSubscriptionsInput()))

    groups_out = await bettbox_list_proxy_groups(ListProxyGroupsInput())
    show("bettbox_list_proxy_groups", groups_out)

    # Pick a real group for the follow-up calls.
    group_name = None
    for line in groups_out.splitlines():
        if line.startswith("|") and "Group" not in line and "---" not in line:
            group_name = line.split("|")[1].strip()
            break

    if group_name:
        show(
            f"bettbox_list_proxies({group_name})",
            await bettbox_list_proxies(ListProxiesInput(group=group_name)),
        )

    show("bettbox_test_delay(Proxies)", await bettbox_test_delay(TestDelayInput(name="Proxies")))
    show(
        "bettbox_list_connections",
        await bettbox_list_connections(ListConnectionsInput(limit=3)),
    )

    print(f"{'FAILURES: ' + str(failures) if failures else 'all read-only tools OK'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
