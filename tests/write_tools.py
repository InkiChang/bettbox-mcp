"""Write-tool test using only reversible operations.

Run with:  uv run python tests/write_tools.py

Covered:
  * bettbox_set_mode              (change, then restore)
  * bettbox_select_proxy          (switch, then switch back)
  * bettbox_update_subscription   (validate_only=True -> writes nothing)
  * bettbox_close_connections     (only a specific id, never 'all')

NOT covered here because they restart the app or delete data:
  restart_app / stop_app / add_subscription / remove_subscription /
  sync_all_subscriptions without validate_only.
"""

from __future__ import annotations

import asyncio
import sys

from bettbox_mcp.models import (
    CloseConnectionInput,
    SelectProxyInput,
    SetModeInput,
    UpdateSubscriptionInput,
)
from bettbox_mcp.server import (
    bettbox_close_connections,
    bettbox_select_proxy,
    bettbox_set_mode,
    bettbox_update_subscription,
)

failures = 0


def check(name: str, output: str, *, expect_error: bool = False) -> None:
    global failures
    is_error = output.startswith("Error:")
    ok = is_error if expect_error else not is_error
    if not ok:
        failures += 1
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}")
    for line in output.splitlines()[:8]:
        print(f"        {line}")
    print()


async def main() -> int:
    # --- mode: rule -> global -> rule -------------------------------
    check("set_mode(global)", await bettbox_set_mode(SetModeInput(mode="global")))
    check("set_mode(rule)  [restore]", await bettbox_set_mode(SetModeInput(mode="rule")))

    # --- node switch: HK -> other -> back ---------------------------
    from bettbox_mcp.server import bettbox_list_proxies  # local import keeps deps tidy
    from bettbox_mcp.models import ListProxiesInput
    from bettbox_mcp.clash_api import ClashClient
    from bettbox_mcp.config import get_settings

    client = ClashClient(get_settings())
    proxies = await client.proxies()
    group = None
    for name, entry in proxies.items():
        if isinstance(entry, dict) and isinstance(entry.get("all"), list) and entry.get("all"):
            if entry.get("type") == "Selector":
                group = name
                break

    if group:
        entry = proxies[group]
        original = entry.get("now")
        other = next((n for n in entry["all"] if n != original), None)
        if other:
            check(
                f"select_proxy({group} -> {other})",
                await bettbox_select_proxy(SelectProxyInput(group=group, proxy=other)),
            )
            check(
                f"select_proxy({group} -> {original})  [restore]",
                await bettbox_select_proxy(SelectProxyInput(group=group, proxy=original)),
            )
        # Guard: a bogus node must be rejected, not silently applied.
        check(
            "select_proxy(bogus) should fail",
            await bettbox_select_proxy(SelectProxyInput(group=group, proxy="does-not-exist")),
            expect_error=True,
        )
    else:
        print("skipped node switching: no selectable group with members\n")

    # --- subscription dry run: must not touch disk ------------------
    check(
        "update_subscription(ReShare, validate_only)",
        await bettbox_update_subscription(
            UpdateSubscriptionInput(profiles=["ReShare"], validate_only=True)
        ),
    )

    # mihomo's DELETE /connections/{id} is idempotent and answers 204 even for
    # unknown ids, so a bogus id must NOT be treated as a failure here.
    check(
        "close_connections(unknown id) is idempotent",
        await bettbox_close_connections(CloseConnectionInput(connection_id="nope")),
    )

    print("FAILURES: none" if not failures else f"FAILURES: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
