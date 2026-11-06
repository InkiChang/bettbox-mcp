"""Preflight diagnostic for a Bettbox machine.

Run this on the target box (Linux, macOS or Windows) BEFORE wiring the MCP into
an agent. It is read-only by default and never touches Bettbox.

    uv run python tests/platform_probe.py

To also verify the destructive path (stop the app, confirm it stopped, start it
again) — which briefly drops the network — run:

    uv run python tests/platform_probe.py --apply

Exit code 0 means every check the probe attempted succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys

from bettbox_mcp import app_control
from bettbox_mcp.clash_api import ClashApiError, ClashClient
from bettbox_mcp.config import (
    BettboxNotConfigured,
    get_settings,
    looks_like_bettbox_config,
    platform_report,
)

OK = "  [ok  ]"
BAD = "  [FAIL]"
WARN = "  [warn]"

failures = 0


def check(label: str, passed: bool, detail: str = "") -> bool:
    global failures
    if passed:
        print(f"{OK} {label}{' — ' + detail if detail else ''}")
    else:
        failures += 1
        print(f"{BAD} {label}{' — ' + detail if detail else ''}")
    return passed


def warn(label: str, detail: str = "") -> None:
    print(f"{WARN} {label}{' — ' + detail if detail else ''}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="also test stop/start (DROPS THE NETWORK for a few seconds)",
    )
    args = parser.parse_args()

    print("=" * 66)
    print("Bettbox environment")
    print("=" * 66)
    print(platform_report())
    print()

    print("=" * 66)
    print("Checks")
    print("=" * 66)

    # --- platform capability ------------------------------------------
    has_unix = hasattr(socket, "AF_UNIX")
    if sys.platform == "win32":
        # CPython never exposes AF_UNIX on Windows; the CLI flags cover it,
        # but they cannot cross a sandbox boundary (see README).
        warn(
            "no Unix domain sockets on Windows",
            "control uses the --exit/--restart flags instead; those need the "
            "MCP to run OUTSIDE any process-restricting sandbox",
        )
    else:
        check(
            "Python can use Unix domain sockets",
            has_unix,
            "required on Linux/macOS for the control channel",
        )

    # --- discovery -----------------------------------------------------
    try:
        settings = get_settings(refresh=True)
    except BettboxNotConfigured as exc:
        check("Bettbox data directory found", False, "see the list above")
        print()
        print("Hint: export BETTBOX_HOME=/path/to/folder-containing-config.json")
        return 1

    check("config.json looks like Bettbox", looks_like_bettbox_config(settings.config_path))
    check("profiles directory exists", settings.profiles_dir.is_dir())

    # --- executable ----------------------------------------------------
    exe = settings.executable
    if check("Bettbox executable located", exe is not None and exe.exists(), str(exe)):
        check("executable is executable", exe.stat().st_mode & 0o111 != 0 if sys.platform != "win32" else True)

    # --- control channel ------------------------------------------------
    sock = settings.control_sock_path
    portfile = settings.control_port_path
    has_sock = sock.exists()
    has_port = portfile.is_file()
    check(
        "control channel present (socket or port file)",
        has_sock or has_port,
        f"sock={has_sock} port_file={has_port}",
    )

    connectable = False
    if hasattr(socket, "AF_UNIX") and has_sock:
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
            client.settimeout(1.5)
            client.connect(str(sock))
            client.close()
            connectable = True
        except OSError as exc:
            warn("socket exists but cannot connect", str(exc))

    running = app_control.is_process_running(settings)
    check(
        "app detected as running",
        running,
        "control socket reachable" if connectable else "detected via process scan"
        if running
        else "not running",
    )

    # --- REST API -------------------------------------------------------
    client = ClashClient(settings)
    api_ok = await client.is_running()
    check(
        "mihomo REST API reachable",
        api_ok,
        f"http://{settings.controller_addr()}",
    )
    if not api_ok:
        warn(
            "controller disabled?",
            "In Bettbox: Settings -> General -> 'External controller'. "
            "The MCP cannot enable it for you.",
        )
    else:
        configs = await client.configs()
        proxies = await client.proxies()
        groups = sum(1 for v in proxies.values() if isinstance(v, dict) and "all" in v)
        print(f"         mode={configs.get('mode')} tun={bool((configs.get('tun') or {}).get('enable'))} "
              f"groups={groups} nodes={len(proxies) - groups}")
        check("secret accepted by controller", True)

    # --- subscription fetch path ---------------------------------------
    if api_ok:
        try:
            config = settings.read_config()
            profiles = [p for p in (config.get("profiles") or []) if p.get("url")]
            if profiles:
                print(f"         {len(profiles)} subscription(s) with a URL")
                check("proxy port used for downloads", settings.mixed_port() > 0,
                      f"mixed-port={settings.mixed_port()}")
            else:
                warn("no subscriptions with a URL to test the download path")
        except Exception as exc:  # noqa: BLE001
            warn("could not inspect subscriptions", str(exc))

    # --- destructive round trip ----------------------------------------
    if args.apply:
        print()
        print("=" * 66)
        print("Stop / start round trip  (the network will drop briefly)")
        print("=" * 66)
        if not running:
            check("app was running before the test", False, "start Bettbox first")
        else:
            stopped = await app_control.exit_app(settings)
            check("app stopped", stopped)
            started = await app_control.start_app(settings)
            await asyncio.sleep(0)
            back = await asyncio.wait_for(_wait_api(client), timeout=90)
            check("app came back and the API answers", bool(started) or back, f"api={back}")
    else:
        print()
        warn("stop/start not tested", "re-run with --apply to verify (brief network drop)")

    print()
    print("=" * 66)
    if failures:
        print(f"RESULT: {failures} check(s) failed")
    else:
        print("RESULT: all checks passed")
    print("=" * 66)
    return 1 if failures else 0


async def _wait_api(client: ClashClient, tries: int = 60) -> bool:
    for _ in range(tries):
        if await client.is_running():
            return True
        await asyncio.sleep(1.0)
    return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
