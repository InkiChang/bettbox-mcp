"""FastMCP entrypoint: all Bettbox tools live here.

Design rules baked into this server
-----------------------------------
* Runtime control goes through the mihomo REST API (hot, no restart).
* Persistent control goes through ``config.json`` and always inside a restart
  window, because Bettbox rewrites that file on graceful exit.
* ``profiles/*.yaml`` is *not* rewritten on exit, so subscription refreshes need
  no restart unless metadata in ``config.json`` must also change.
* Every write tool reports whether it restarted the app, so an agent never has
  to guess why its connection dropped.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__, app_control, store
from .clash_api import ClashApiError, ClashClient
from .config import BettboxNotConfigured, Settings, get_settings
from .models import (
    AddSubscriptionInput,
    CloseConnectionInput,
    ListConnectionsInput,
    ListProxiesInput,
    ListProxyGroupsInput,
    ListSubscriptionsInput,
    RemoveSubscriptionInput,
    ResponseFormat,
    RestartAppInput,
    SelectProxyInput,
    SetModeInput,
    SetTunInput,
    StatusInput,
    StopAppInput,
    SyncAllSubscriptionsInput,
    TestDelayInput,
    UpdateSubscriptionInput,
)

mcp = MCPServer(
    "bettbox_mcp",
    version=__version__,
    instructions=(
        "Controls the Bettbox desktop client (mihomo core) on this machine. "
        "Read-only tools are safe to call freely. Hot writes (select_proxy, "
        "set_mode, set_tun) apply immediately and are not persisted. Tools that "
        "edit config.json (add/remove subscription, sync_metadata=true) restart "
        "the app once and briefly drop the proxy."
    ),
)

def _ann(
    title: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    open_world: bool = False,
    idempotent: bool = False,
) -> ToolAnnotations:
    """Build tool annotations with consistent Bettbox defaults."""
    return ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _settings() -> Settings:
    return get_settings()


def _client() -> ClashClient:
    return ClashClient(_settings())


def _fail(message: str) -> str:
    return f"Error: {message}"


def _human_bytes(value: int | float | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _mask_url(url: str) -> str:
    """Hide the account token that most providers embed in the URL path."""
    if not url:
        return ""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return "***"
    host, slash, _ = rest.partition("/")
    token = rest[len(host) + len(slash):]
    tail = token[-4:] if len(token) > 8 else ""
    return f"{scheme}://{host}/{'***' + tail if token else ''}"


def _split_groups(proxies: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (group names, leaf node names) from a /proxies payload."""
    groups, leaves = [], []
    for name, entry in proxies.items():
        if isinstance(entry, dict) and isinstance(entry.get("all"), list):
            groups.append(name)
        else:
            leaves.append(name)
    return groups, leaves


def _resolve_target(ident: str, config: dict[str, Any]) -> dict[str, Any]:
    return store.find_profile(config, ident)


# --------------------------------------------------------------------------
# read-only tools
# --------------------------------------------------------------------------
@mcp.tool(name="bettbox_get_status", annotations=_ann("Get Bettbox status", read_only=True))
async def bettbox_get_status(params: StatusInput) -> str:
    """Report whether Bettbox is running and what the proxy is currently doing.

    Combines three independent sources so a partial failure is still useful:
    the OS process table, the on-disk config, and the mihomo REST API.

    Args:
        params (StatusInput): ``include_traffic`` adds cumulative byte counters.

    Returns:
        str: Markdown report. Key fields per section:
        - process: ``running`` (bool), ``executable`` (path or null)
        - config: ``data_dir``, ``current_profile``, ``controller``, ``mixed_port``
        - runtime: ``mode``, ``tun``, ``nodes``, ``groups``, ``controller_ok``
        Failure text starts with ``Error:``.
    """
    try:
        settings = _settings()
    except BettboxNotConfigured as exc:
        return _fail(str(exc))

    lines: list[str] = ["# Bettbox status", ""]

    running = app_control.is_process_running(settings)
    lines += [
        "## Process",
        f"- running: **{running}**",
        f"- executable: `{settings.executable or 'not found'}`",
        f"- data dir: `{settings.data_dir}`",
        "",
    ]

    try:
        config = store.read_config(settings)
    except store.StoreError as exc:
        return _fail(str(exc))

    profile_id = store.current_profile_id(config)
    label = "none"
    for profile in store.profiles(config):
        if str(profile.get("id")) == profile_id:
            label = str(profile.get("label") or profile_id)
            break

    lines += [
        "## Config",
        f"- subscriptions: {len(store.profiles(config))}",
        f"- active profile: **{label}** (`{profile_id}`)",
        f"- controller: `{settings.controller_addr()}`",
        f"- secret stored: {'yes' if settings.controller_secret() else 'no'}",
        f"- mixed port: {settings.mixed_port()}",
        "",
    ]

    client = _client()
    try:
        version = await client.version()
        configs = await client.configs()
        proxies = await client.proxies()
        groups, leaves = _split_groups(proxies)
        tun = configs.get("tun") or {}
        lines += [
            "## Runtime",
            "- controller reachable: **yes**",
            f"- core version: `{version.get('version') or '(not reported)'}`",
            f"- mode: **{configs.get('mode')}**",
            f"- tun (runtime): **{bool(tun.get('enable'))}**",
            f"- allow-lan: {configs.get('allow-lan')}",
            f"- groups: {len(groups)} | nodes: {len(leaves)}",
        ]
        if params.include_traffic:
            connections = await client.connections()
            lines += [
                f"- connections: {len(connections.get('connections') or [])}",
                f"- total up: {_human_bytes(connections.get('uploadTotal'))}",
                f"- total down: {_human_bytes(connections.get('downloadTotal'))}",
            ]
    except ClashApiError as exc:
        lines += [
            "## Runtime",
            "- controller reachable: **no**",
            f"- reason: {exc}",
        ]

    return "\n".join(lines)


@mcp.tool(
    name="bettbox_list_subscriptions",
    annotations=_ann("List subscriptions", read_only=True),
)
async def bettbox_list_subscriptions(params: ListSubscriptionsInput) -> str:
    """List every subscription configured in Bettbox, with quota and last update.

    Reads ``config.json`` only; the app does not need to be running.

    Args:
        params (ListSubscriptionsInput): ``response_format`` and ``reveal_urls``.

    Returns:
        str: Markdown table or JSON array. Per row: ``id``, ``label``,
        ``current`` (bool), ``auto_update`` (bool), ``last_update`` (str),
        ``used`` / ``total`` (bytes), ``expire`` (epoch seconds), ``url``.
    """
    try:
        settings = _settings()
        config = store.read_config(settings)
    except (BettboxNotConfigured, store.StoreError) as exc:
        return _fail(str(exc))

    active = store.current_profile_id(config)
    rows: list[dict[str, Any]] = []
    for profile in store.profiles(config):
        info = profile.get("subscriptionInfo") or {}
        url = profile.get("url") or ""
        rows.append(
            {
                "id": profile.get("id"),
                "label": profile.get("label"),
                "current": str(profile.get("id")) == active,
                "auto_update": bool(profile.get("autoUpdate")),
                "last_update": profile.get("lastUpdateDate"),
                "used": int(info.get("download") or 0) + int(info.get("upload") or 0),
                "total": int(info.get("total") or 0),
                "expire": int(info.get("expire") or 0),
                "url": url if params.reveal_urls else _mask_url(url),
            }
        )

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"count": len(rows), "subscriptions": rows}, ensure_ascii=False, indent=2)

    if not rows:
        return "No subscriptions configured."

    lines = [f"# Subscriptions ({len(rows)})", ""]
    lines.append("| | Label | ID | Last update | Quota | Auto |")
    lines.append("|---|---|---|---|---|---|")
    for row in rows:
        mark = "*" if row["current"] else ""
        quota = (
            f"{_human_bytes(row['used'])} / {_human_bytes(row['total'])}"
            if row["total"]
            else "n/a"
        )
        lines.append(
            f"| {mark} | {row['label']} | `{row['id']}` | {row['last_update'] or '-'} "
            f"| {quota} | {'on' if row['auto_update'] else 'off'} |"
        )
    lines += ["", "`*` marks the active profile."]
    return "\n".join(lines)


@mcp.tool(
    name="bettbox_list_proxy_groups",
    annotations=_ann("List proxy groups", read_only=True),
)
async def bettbox_list_proxy_groups(params: ListProxyGroupsInput) -> str:
    """List proxy groups with their currently selected member and member counts.

    Args:
        params (ListProxyGroupsInput): ``only_selectable`` filters out
        non-selectable groups; ``response_format`` picks the layout.

    Returns:
        str: Markdown table or JSON. Per group: ``name``, ``type``, ``now``,
        ``members`` (int), ``hidden`` (bool), ``has_icon`` (bool).
    """
    try:
        proxies = await _client().proxies()
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(str(exc))

    groups = []
    for name, entry in proxies.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("all"), list):
            continue
        group_type = str(entry.get("type") or "")
        selectable = "Selector" in group_type or group_type in ("Fallback", "URLTest", "LoadBalance")
        if params.only_selectable and not selectable:
            continue
        groups.append(
            {
                "name": name,
                "type": group_type,
                "now": entry.get("now"),
                "members": len(entry.get("all") or []),
                "hidden": bool(entry.get("hidden")),
                "has_icon": bool(entry.get("icon")),
            }
        )

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"count": len(groups), "groups": groups}, ensure_ascii=False, indent=2)

    if not groups:
        return "No proxy groups matched. The core may not be running."

    lines = [f"# Proxy groups ({len(groups)})", ""]
    lines.append("| Group | Type | Selected | Members |")
    lines.append("|---|---|---|---|")
    for group in groups:
        lines.append(
            f"| {group['name']} | {group['type']} | {group['now'] or '-'} | {group['members']} |"
        )
    return "\n".join(lines)


@mcp.tool(name="bettbox_list_proxies", annotations=_ann("List nodes in a group", read_only=True))
async def bettbox_list_proxies(params: ListProxiesInput) -> str:
    """List the member nodes of one proxy group, marking the active one.

    Args:
        params (ListProxiesInput): ``group`` is the exact group name from
        ``bettbox_list_proxy_groups``.

    Returns:
        str: Markdown list or JSON array. Per node: ``name``, ``type``,
        ``active`` (bool), ``delay`` (int ms or null).
    """
    try:
        client = _client()
        proxies = await client.proxies()
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(str(exc))

    group = proxies.get(params.group)
    if not isinstance(group, dict) or not isinstance(group.get("all"), list):
        available = ", ".join(sorted(n for n, e in proxies.items() if isinstance(e, dict) and "all" in e))
        return _fail(
            f"{params.group!r} is not a proxy group. Available groups: {available or 'none'}"
        )

    current = group.get("now")
    nodes = []
    for name in group["all"]:
        entry = proxies.get(name) or {}
        nodes.append(
            {
                "name": name,
                "type": entry.get("type"),
                "active": name == current,
                "delay": (entry.get("history") or [{}])[-1].get("delay")
                if entry.get("history")
                else None,
            }
        )

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(
            {"group": params.group, "now": current, "count": len(nodes), "nodes": nodes},
            ensure_ascii=False,
            indent=2,
        )

    lines = [f"# {params.group} ({len(nodes)} nodes)", "", f"Selected: **{current or '-'}**", ""]
    for node in nodes:
        mark = "*" if node["active"] else "-"
        delay = f"{node['delay']} ms" if node["delay"] else "-"
        lines.append(f"{mark} {node['name']} ({node['type']}, {delay})")
    return "\n".join(lines)


@mcp.tool(name="bettbox_test_delay", annotations=_ann("Test node latency", read_only=True))
async def bettbox_test_delay(params: TestDelayInput) -> str:
    """Measure latency for one node or every member of a group.

    Args:
        params (TestDelayInput): ``name``, ``kind`` ('auto' by default) and
        ``timeout_ms``.

    Returns:
        str: Markdown table. Group mode returns one row per member; a failed
        node reports ``timeout``.
    """
    try:
        client = _client()
        proxies = await client.proxies()
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(str(exc))

    entry = proxies.get(params.name)
    is_group = isinstance(entry, dict) and isinstance(entry.get("all"), list)
    kind = params.kind
    if kind == "auto":
        kind = "group" if is_group else "proxy"
    if kind == "group" and not is_group:
        return _fail(f"{params.name!r} is not a group; use kind='proxy'.")

    if kind == "proxy":
        delay = await client.proxy_delay(params.name, params.timeout_ms)
        return (
            f"**{params.name}**: {delay} ms"
            if delay
            else f"**{params.name}**: timeout or unreachable"
        )

    delays = await client.group_delay(params.name, params.timeout_ms)
    if not delays:
        return _fail(f"Delay test for group {params.name!r} returned no results.")

    ordered = sorted(delays.items(), key=lambda kv: (kv[1] is None, kv[1] or 0))
    ok = sum(1 for _, value in ordered if value)
    lines = [f"# Delay: {params.name}", "", f"{ok}/{len(ordered)} reachable", ""]
    lines.append("| Node | Delay |")
    lines.append("|---|---|")
    for name, value in ordered:
        lines.append(f"| {name} | {f'{value} ms' if value else 'timeout'} |")
    return "\n".join(lines)


@mcp.tool(name="bettbox_list_connections", annotations=_ann("List live connections", read_only=True))
async def bettbox_list_connections(params: ListConnectionsInput) -> str:
    """List active connections with host, rule, chain and transferred bytes.

    Args:
        params (ListConnectionsInput): ``limit``, ``sort_by``, ``response_format``.

    Returns:
        str: Markdown table or JSON. Per row: ``host``, ``rule``, ``chain``,
        ``download``, ``upload``, ``start``.
    """
    try:
        data = await _client().connections()
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(str(exc))

    raw = data.get("connections") or []
    rows = []
    for item in raw:
        metadata = item.get("metadata") or {}
        rows.append(
            {
                "id": item.get("id"),
                "host": metadata.get("host") or metadata.get("destinationIP") or "-",
                "rule": item.get("rule") or "-",
                "chain": " > ".join(item.get("chains") or []) or "-",
                "download": int(item.get("download") or 0),
                "upload": int(item.get("upload") or 0),
                "start": item.get("start"),
            }
        )
    key = {"download": "download", "upload": "upload", "start": "start"}[params.sort_by]
    rows.sort(key=lambda row: row[key] or 0, reverse=True)
    rows = rows[: params.limit]

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(
            {
                "total_connections": len(raw),
                "upload_total": data.get("uploadTotal"),
                "download_total": data.get("downloadTotal"),
                "connections": rows,
            },
            ensure_ascii=False,
            indent=2,
        )

    lines = [
        f"# Connections ({len(raw)} live, showing {len(rows)})",
        "",
        f"Total up: {_human_bytes(data.get('uploadTotal'))} | "
        f"Total down: {_human_bytes(data.get('downloadTotal'))}",
        "",
    ]
    lines.append("| Host | Rule | Chain | Down | Up |")
    lines.append("|---|---|---|---|---|")
    for row in rows:
        lines.append(
            f"| {row['host']} | {row['rule']} | {row['chain']} | "
            f"{_human_bytes(row['download'])} | {_human_bytes(row['upload'])} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# runtime write tools
# --------------------------------------------------------------------------
@mcp.tool(name="bettbox_select_proxy", annotations=_ann("Switch node"))
async def bettbox_select_proxy(params: SelectProxyInput) -> str:
    """Switch the active node of a group. Takes effect immediately.

    This does not restart the app and is not persisted to config.json by this
    call; Bettbox records the selection itself on the next state save.

    Args:
        params (SelectProxyInput): ``group`` and the target ``proxy``.

    Returns:
        str: Confirmation with the group's new active node.
    """
    try:
        client = _client()
        before = (await client.proxies()).get(params.group) or {}
        if params.proxy not in (before.get("all") or []):
            members = ", ".join((before.get("all") or [])[:10])
            return _fail(
                f"{params.proxy!r} is not a member of {params.group!r}. "
                f"First members: {members}"
            )
        await client.select_proxy(params.group, params.proxy)
        after = (await client.proxies()).get(params.group) or {}
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(str(exc))

    return (
        f"Switched **{params.group}**: {before.get('now') or '-'} -> "
        f"**{after.get('now') or params.proxy}** (applied immediately, no restart)"
    )


@mcp.tool(name="bettbox_set_mode", annotations=_ann("Set outbound mode"))
async def bettbox_set_mode(params: SetModeInput) -> str:
    """Change the outbound routing mode: rule / global / direct.

    Runtime-only. Bettbox rebuilds the config from ``config.json`` on the next
    start, so a persisted default still wins after a restart.

    Args:
        params (SetModeInput): ``mode``.

    Returns:
        str: Confirmation naming the previous and the new mode.
    """
    try:
        client = _client()
        previous = (await client.configs()).get("mode")
        await client.set_mode(params.mode)
        current = (await client.configs()).get("mode")
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(str(exc))

    note = ""
    if params.mode != previous:
        note = " Runtime change only; it reverts if the app rebuilds its config."
    return f"Mode: {previous} -> **{current}**.{note}"


@mcp.tool(name="bettbox_set_tun", annotations=_ann("Toggle TUN (runtime)"))
async def bettbox_set_tun(params: SetTunInput) -> str:
    """Enable or disable TUN for the current session only.

    Enabling TUN needs the core binary to already hold elevation. Bettbox grants
    that once (setuid on macOS/Linux, a helper service on Windows); this MCP
    never handles a password. If elevation is missing the core rejects the
    change and the tool reports the error.

    Args:
        params (SetTunInput): ``enable``.

    Returns:
        str: Confirmation plus a reminder that the change is not persisted.
    """
    try:
        result = await _client().set_tun(params.enable)
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(
            f"{exc}\nIf TUN cannot be enabled, open Bettbox once and toggle it "
            "in the UI so the core gets elevated, then retry."
        )
    return (
        f"TUN (runtime) set to **{bool(result.get('enable'))}**. "
        "Not persisted: Bettbox restores the saved value on the next start."
    )


@mcp.tool(
    name="bettbox_close_connections",
    annotations=_ann("Close connections", destructive=True),
)
async def bettbox_close_connections(params: CloseConnectionInput) -> str:
    """Close one live connection, or every connection when id is ``all``.

    Args:
        params (CloseConnectionInput): ``connection_id`` from
        ``bettbox_list_connections``, or the literal ``all``.

    Returns:
        str: Confirmation.
    """
    try:
        client = _client()
        if params.connection_id == "all":
            await client.close_all_connections()
            return "Closed all connections."
        await client.close_connection(params.connection_id)
        return f"Closed connection `{params.connection_id}`."
    except (BettboxNotConfigured, ClashApiError) as exc:
        return _fail(str(exc))


# --------------------------------------------------------------------------
# subscription tools
# --------------------------------------------------------------------------
@mcp.tool(
    name="bettbox_update_subscription",
    annotations=_ann("Refresh subscriptions", open_world=True),
)
async def bettbox_update_subscription(params: UpdateSubscriptionInput) -> str:
    """Download fresh configs for the named subscriptions and write them to disk.

    Mirrors the app's per-profile refresh: fetch the URL through the proxy,
    normalise tab indentation, validate the structure, then atomically replace
    ``profiles/<id>.yaml``. The app does **not** need to be stopped.

    Args:
        params (UpdateSubscriptionInput): ``profiles`` (ids or labels),
        ``sync_metadata`` to also refresh dates/quotas (needs a restart),
        ``validate_only`` to dry-run.

    Returns:
        str: Per-profile result: ``ok``, ``changed``, ``bytes``, ``nodes``,
        ``error``. Ends with whether a restart happened.
    """
    try:
        settings = _settings()
        config = store.read_config(settings)
    except (BettboxNotConfigured, store.StoreError) as exc:
        return _fail(str(exc))

    resolved = []
    for ident in params.profiles:
        try:
            resolved.append(_resolve_target(ident, config))
        except store.StoreError as exc:
            return _fail(str(exc))

    if params.validate_only:
        results = []
        for profile in resolved:
            url = profile.get("url")
            if not url:
                results.append(f"- {profile.get('label')}: no URL (local profile)")
                continue
            try:
                raw, _ = await store.download_subscription(settings, str(url))
                content = store.patch_yaml_config(raw.decode("utf-8"))
                document = store.validate_subscription(content)
                results.append(
                    f"- {profile.get('label')}: **valid** ({len(raw)} bytes, "
                    f"{store.count_nodes(document)} nodes)"
                )
            except (store.StoreError, UnicodeDecodeError) as exc:
                results.append(f"- {profile.get('label')}: **invalid** - {exc}")
        return "Dry run (nothing written):\n" + "\n".join(results)

    updates = [
        await store.refresh_subscription(settings, profile) for profile in resolved
    ]

    restarted = False
    if params.sync_metadata and any(u.ok and u.changed for u in updates):
        async with app_control.RestartWindow(settings):
            store.apply_metadata(settings, updates)
        restarted = True

    lines = ["# Subscription refresh", ""]
    lines.append("| Profile | Result | Size | Nodes |")
    lines.append("|---|---|---|---|")
    for update in updates:
        status = "updated" if update.changed else ("unchanged" if update.ok else f"**failed**: {update.error}")
        lines.append(
            f"| {update.label} | {status} | {update.bytes_written} B | {update.node_count} |"
        )
    lines += ["", f"Restarted app: **{restarted}**"]
    if not restarted:
        lines.append(
            "YAML was refreshed in place. Take effect when the profile is applied "
            "(switch to it, or restart the app)."
        )
    return "\n".join(lines)


@mcp.tool(
    name="bettbox_sync_all_subscriptions",
    annotations=_ann("Sync all subscriptions", open_world=True),
)
async def bettbox_sync_all_subscriptions(params: SyncAllSubscriptionsInput) -> str:
    """Refresh every subscription at once - the app's "sync all" button.

    Skips local-file profiles (they have no URL), runs the downloads
    concurrently, and reports one row per subscription.

    Args:
        params (SyncAllSubscriptionsInput): ``sync_metadata`` and ``concurrency``.

    Returns:
        str: Markdown table with ``ok`` / ``changed`` / ``size`` / ``error`` per
        profile, plus whether a restart happened.
    """
    try:
        settings = _settings()
    except BettboxNotConfigured as exc:
        return _fail(str(exc))

    updates = await store.refresh_all(settings, concurrency=params.concurrency)
    if not updates:
        return "No refreshtable subscriptions found (all are local files)."

    restarted = False
    if params.sync_metadata and any(u.ok and u.changed for u in updates):
        async with app_control.RestartWindow(settings):
            store.apply_metadata(settings, updates)
        restarted = True

    ok = sum(1 for u in updates if u.ok)
    changed = sum(1 for u in updates if u.changed)
    lines = [f"# Sync all: {ok}/{len(updates)} succeeded, {changed} changed", ""]
    lines.append("| Profile | Result | Size | Nodes |")
    lines.append("|---|---|---|---|")
    for update in updates:
        status = "updated" if update.changed else ("unchanged" if update.ok else f"failed: {update.error}")
        lines.append(f"| {update.label} | {status} | {update.bytes_written} B | {update.node_count} |")
    lines += ["", f"Restarted app: **{restarted}**"]
    return "\n".join(lines)


@mcp.tool(
    name="bettbox_add_subscription",
    annotations=_ann("Add subscription", open_world=True),
)
async def bettbox_add_subscription(params: AddSubscriptionInput) -> str:
    """Register a new subscription and optionally download it right away.

    Adding needs a restart because the profile list lives in ``config.json``,
    which Bettbox overwrites on exit; the tool therefore performs the whole
    exit -> edit -> start cycle atomically.

    Args:
        params (AddSubscriptionInput): ``url``, optional ``label`` and
        ``position``, and ``download``.

    Returns:
        str: The new profile id plus whether a restart happened.
    """
    try:
        settings = _settings()
    except BettboxNotConfigured as exc:
        return _fail(str(exc))

    try:
        async with app_control.RestartWindow(settings):
            entry = store.add_subscription(
                settings, url=params.url, label=params.label, position=params.position
            )
    except (store.StoreError, app_control.AppControlError) as exc:
        return _fail(str(exc))

    lines = [
        f"Added subscription **{entry['label']}** (`{entry['id']}`).",
        "App restarted: **True** (config.json edit requires a stopped app).",
    ]
    if params.download:
        update = await store.refresh_subscription(settings, entry)
        lines.append(
            f"Initial download: **{'ok' if update.ok else 'failed'}**"
            + (f" - {update.error}" if not update.ok else f", {update.node_count} nodes")
        )
        if update.ok:
            lines.append(
                "Switch to it with the app UI or restart again to apply; the "
                "profile list is already saved."
            )
    return "\n".join(lines)


@mcp.tool(
    name="bettbox_remove_subscription",
    annotations=_ann("Remove subscription", destructive=True),
)
async def bettbox_remove_subscription(params: RemoveSubscriptionInput) -> str:
    """Delete a subscription. Requires one app restart.

    Refuses to delete the currently active profile; switch away first.

    Args:
        params (RemoveSubscriptionInput): ``profile`` (id or label) and
        ``keep_files``.

    Returns:
        str: Confirmation with the removed label and restart flag.
    """
    try:
        settings = _settings()
    except BettboxNotConfigured as exc:
        return _fail(str(exc))

    try:
        async with app_control.RestartWindow(settings):
            removed = store.remove_subscription(settings, params.profile)
            yaml_path = settings.profile_path(str(removed.get("id")))
            if not params.keep_files and yaml_path.is_file():
                yaml_path.unlink()
    except (store.StoreError, app_control.AppControlError) as exc:
        return _fail(str(exc))

    return (
        f"Removed subscription **{removed.get('label')}** (`{removed.get('id')}`). "
        f"YAML kept on disk: **{params.keep_files}**. App restarted: **True**."
    )


# --------------------------------------------------------------------------
# process tools
# --------------------------------------------------------------------------
@mcp.tool(
    name="bettbox_restart_app",
    annotations=_ann("Restart Bettbox"),
)
async def bettbox_restart_app(params: RestartAppInput) -> str:
    """Restart the Bettbox app through its control socket.

    A restart briefly drops the proxy (the system proxy setting is restored on
    the way back up). Use it after changing anything in ``config.json``.

    Args:
        params (RestartAppInput): optional ``reason`` for the audit trail.

    Returns:
        str: Whether the restart completed and whether the controller is back.
    """
    try:
        settings = _settings()
        ok = await app_control.restart_app(settings)
    except (BettboxNotConfigured, app_control.AppControlError) as exc:
        return _fail(str(exc))

    if not ok:
        return _fail("Bettbox did not exit cleanly; the restart was aborted.")

    if await ClashClient(settings).is_running():
        return "Bettbox restarted; the controller is reachable again."
    return (
        "Bettbox restarted, but the controller is not reachable. If it was "
        "disabled, enable 'External controller' in the app settings."
    )


@mcp.tool(
    name="bettbox_stop_app",
    annotations=_ann("Stop Bettbox", destructive=True),
)
async def bettbox_stop_app(params: StopAppInput) -> str:
    """Quit Bettbox, which also stops proxying.

    Uses the graceful path, so system proxy, DNS and TUN are restored by the app
    itself. Force-killing would leave the system proxy pointing at a dead port.

    Args:
        params (StopAppInput): optional ``reason``.

    Returns:
        str: Whether the process exited.
    """
    try:
        settings = _settings()
        if not app_control.is_process_running(settings):
            return "Bettbox is not running."
        exited = await app_control.exit_app(settings)
    except (BettboxNotConfigured, app_control.AppControlError) as exc:
        return _fail(str(exc))

    if exited:
        return (
            "Bettbox exited. Proxying is stopped and the system proxy / DNS were "
            "restored by the app."
        )
    return _fail(
        "Bettbox did not exit within the timeout. It may be showing a dialog; "
        "close it manually and retry."
    )


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------
def main() -> None:
    """Run the server over stdio (the transport MCP clients expect locally)."""
    try:
        get_settings()
    except BettboxNotConfigured as exc:
        print(f"[bettbox-mcp] warning: {exc}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


