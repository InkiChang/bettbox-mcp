"""Bettbox's on-disk state: ``config.json`` plus ``profiles/*.yaml``.

The subscription refresh implemented here is a faithful port of the app's own
"sync all" button (``lib/views/profiles/profiles.dart`` ->
``appController.updateProfile`` -> ``Profile.update`` -> ``Profile.saveFile``).
The only capability that cannot be reproduced is the app's
``clashCore.validateConfig`` call, which is a Dart<->Go FFI function with no
REST surface; a structural YAML check plus a post-restart health probe is used
instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml

from .config import SUBSCRIPTION_USER_AGENT, Settings

# The app skips profiles that are plain local files (no URL to fetch).
LOCAL_PROFILE_TYPES = {"file"}

# A tab-indented YAML document is invalid for mihomo; the app rewrites leading
# tabs to two spaces before writing. Same regex, same replacement.
_TAB_INDENT = re.compile(r"^[ \t]*\t[ \t]*", re.MULTILINE)

_REQUIRED_KEYS = ("proxies", "proxy-groups", "proxy-providers", "rules")
_PORT_KEYS = ("mixed-port", "port", "socks-port")


class StoreError(RuntimeError):
    """Raised for invalid state or a rejected subscription payload."""


@dataclass
class SubscriptionUpdate:
    """Outcome of refreshing one subscription."""

    profile_id: str
    label: str
    ok: bool
    changed: bool = False
    bytes_written: int = 0
    node_count: int = 0
    upstream: int = 0
    userinfo: dict[str, int] | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# config.json helpers
# --------------------------------------------------------------------------
def read_config(settings: Settings) -> dict[str, Any]:
    try:
        return json.loads(settings.config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StoreError(f"config.json not found at {settings.config_path}") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"config.json is not valid JSON: {exc}") from exc


def write_config(settings: Settings, data: dict[str, Any]) -> None:
    """Atomic write; mirrors the app's temp-file-then-rename strategy."""
    payload = json.dumps(data, ensure_ascii=False, indent=None, separators=(",", ":"))
    _atomic_write_text(settings.config_path, payload)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    value = config.get("profiles")
    return value if isinstance(value, list) else []


def find_profile(config: dict[str, Any], ident: str) -> dict[str, Any]:
    """Resolve a profile by id or by (case-insensitive) label."""
    target = ident.strip().lower()
    for profile in profiles(config):
        if str(profile.get("id", "")) == ident:
            return profile
        label = profile.get("label")
        if isinstance(label, str) and label.strip().lower() == target:
            return profile
    raise StoreError(f"No subscription matches {ident!r} (tried id and label).")


def current_profile_id(config: dict[str, Any]) -> str | None:
    value = config.get("currentProfileId")
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------
# subscription payload handling
# --------------------------------------------------------------------------
def patch_yaml_config(content: str) -> str:
    """Port of ``utils.patchYamlConfig``: tabs -> two spaces, only if present."""
    if "\t" not in content:
        return content
    return _TAB_INDENT.sub(lambda m: m.group(0).replace("\t", "  "), content)


def validate_subscription(content: str) -> dict[str, Any]:
    """Structural guard replacing the FFI config validator.

    Deliberately shallow: it catches HTML error pages, empty bodies and
    truncated downloads. Real schema validation still happens inside Bettbox
    when the profile is applied, so an invalid file is recoverable by the
    caller restoring the backup taken before the write.

    Returns the parsed document so callers can inspect it cheaply.
    """
    if not content.strip():
        raise StoreError("Subscription returned an empty body.")
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise StoreError(f"Subscription is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise StoreError(
            "Subscription did not parse to a YAML mapping; the endpoint may "
            "have returned a node list or an HTML error page instead of a "
            "Clash config."
        )
    if not any(key in document for key in _REQUIRED_KEYS):
        raise StoreError(
            "Subscription has no proxies / proxy-groups / rules section; "
            "refusing to overwrite the existing profile."
        )
    if not any(key in document for key in _PORT_KEYS):
        raise StoreError("Subscription is missing the inbound port keys.")
    return document


def count_nodes(document: dict[str, Any]) -> int:
    """Total proxy definitions declared anywhere in a Clash config."""
    total = len(document.get("proxies") or [])
    providers = document.get("proxy-providers") or {}
    if isinstance(providers, dict):
        for provider in providers.values():
            if isinstance(provider, dict):
                payload = provider.get("payload")
                if isinstance(payload, list):
                    total += len(payload)
    return total


def parse_subscription_userinfo(header: str | None) -> dict[str, int]:
    """Port of ``SubscriptionInfo.formHString`` (bytes; expire is epoch secs)."""
    result = {"upload": 0, "download": 0, "total": 0, "expire": 0}
    if not header:
        return result
    for chunk in header.split(";"):
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key in result:
            try:
                result[key] = int(float(value.strip()))
            except ValueError:
                pass
    return result


def dart_timestamp(moment: datetime | None = None) -> str:
    """Match Dart's naive local ``DateTime`` JSON form: 2026-11-05T14:19:19.950626."""
    return (moment or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S.%f")


async def download_subscription(
    settings: Settings, url: str, *, timeout: float = 60.0
) -> tuple[bytes, dict[str, str]]:
    """Fetch a subscription the way Bettbox does.

    The app proxies this request through its own mixed port
    (``BettboxHttpOverrides.handleFindProxy``); some providers are only
    reachable that way, so the proxy is always used when known.
    """
    proxy = f"http://127.0.0.1:{settings.mixed_port()}"
    headers = {
        "User-Agent": SUBSCRIPTION_USER_AGENT,
        "Connection": "close",
    }
    attempts: list[str | None] = [proxy]
    if not settings.patch_clash_config().get("mixed-port"):
        attempts.append(None)
    last_error: Exception | None = None
    for candidate in attempts:
        try:
            async with httpx.AsyncClient(
                proxy=candidate, timeout=timeout, follow_redirects=True
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.content, dict(response.headers)
        except (httpx.HTTPError, OSError) as exc:  # pragma: no cover - network
            last_error = exc
            continue
    raise StoreError(f"Could not download the subscription: {last_error}")


async def refresh_subscription(
    settings: Settings,
    profile: dict[str, Any],
    *,
    timeout: float = 60.0,
) -> SubscriptionUpdate:
    """Refresh one profile's YAML in place. Safe to run while the app is open.

    ``profiles/*.yaml`` is never rewritten on exit, so no restart is needed
    unless the caller also wants the metadata in ``config.json`` updated.
    """
    profile_id = str(profile.get("id", ""))
    label = str(profile.get("label") or profile_id)
    url = profile.get("url")

    if profile.get("type") in LOCAL_PROFILE_TYPES or not isinstance(url, str) or not url:
        return SubscriptionUpdate(
            profile_id=profile_id,
            label=label,
            ok=False,
            error="Local file profiles have no URL to refresh.",
        )

    try:
        raw, headers = await download_subscription(settings, url, timeout=timeout)
        content = raw.decode("utf-8")
        content = patch_yaml_config(content)
        document = validate_subscription(content)
    except (StoreError, UnicodeDecodeError) as exc:
        return SubscriptionUpdate(profile_id=profile_id, label=label, ok=False, error=str(exc))

    path = settings.profile_path(profile_id)
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    if previous == content:
        return SubscriptionUpdate(
            profile_id=profile_id,
            label=label,
            ok=True,
            changed=False,
            bytes_written=len(content.encode("utf-8")),
            node_count=count_nodes(document),
            upstream=len(raw),
        )

    # Keep a one-step rollback copy before overwriting.
    if path.is_file():
        shutil.copy2(path, path.with_suffix(".yaml.mcp-bak"))

    _atomic_write_text(path, content)
    return SubscriptionUpdate(
        profile_id=profile_id,
        label=label,
        ok=True,
        changed=True,
        bytes_written=len(content.encode("utf-8")),
        node_count=count_nodes(document),
        upstream=len(raw),
        userinfo=parse_subscription_userinfo(
            headers.get("subscription-userinfo") or headers.get("Subscription-Userinfo")
        ),
    )


async def refresh_all(
    settings: Settings, *, timeout: float = 60.0, concurrency: int = 6
) -> list[SubscriptionUpdate]:
    """Port of the "sync all" button: every profile in parallel."""
    config = read_config(settings)
    targets = [
        profile
        for profile in profiles(config)
        if profile.get("type") not in LOCAL_PROFILE_TYPES
    ]
    semaphore = asyncio.Semaphore(concurrency)

    async def run(profile: dict[str, Any]) -> SubscriptionUpdate:
        async with semaphore:
            return await refresh_subscription(settings, profile, timeout=timeout)

    return list(await asyncio.gather(*(run(p) for p in targets)))


def apply_metadata(
    settings: Settings, updates: Iterable[SubscriptionUpdate]
) -> int:
    """Write ``lastUpdateDate`` / ``subscriptionInfo`` back into config.json.

    Must be called inside ``app_control.RestartWindow``: the app overwrites
    config.json on graceful exit.
    """
    config = read_config(settings)
    by_id = {str(p.get("id")): p for p in profiles(config)}
    touched = 0
    for update in updates:
        if not update.ok or not update.changed:
            continue
        profile = by_id.get(update.profile_id)
        if profile is None:
            continue
        profile["lastUpdateDate"] = dart_timestamp()
        if update.userinfo:
            profile["subscriptionInfo"] = update.userinfo
        touched += 1
    if touched:
        write_config(settings, config)
    return touched


# --------------------------------------------------------------------------
# add / remove
# --------------------------------------------------------------------------
def new_profile_id() -> str:
    """Bettbox ids are millisecond epoch strings."""
    return str(int(datetime.now().timestamp() * 1000))


def add_subscription(
    settings: Settings,
    *,
    url: str,
    label: str | None = None,
    position: int | None = None,
) -> dict[str, Any]:
    """Register a new subscription. Call inside a RestartWindow."""
    config = read_config(settings)
    existing = profiles(config)
    for profile in existing:
        if profile.get("url") == url:
            raise StoreError(f"That URL is already used by {profile.get('label')!r}.")

    profile_id = new_profile_id()
    entry: dict[str, Any] = {
        "id": profile_id,
        "label": label or profile_id,
        "currentGroupName": "Proxies",
        "url": url,
        "lastUpdateDate": dart_timestamp(),
        "autoUpdateDuration": 86400000000,
        "subscriptionInfo": {"upload": 0, "download": 0, "total": 0, "expire": 0},
        "autoUpdate": False,
        "selectedMap": {},
        "unfoldSet": [],
        "overrideData": {
            "enable": False,
            "rule": {"type": "override", "overrideRules": [], "addedRules": []},
        },
        "useScriptOverride": False,
        "ageSecretKey": "",
    }
    if position is None or position >= len(existing):
        existing.append(entry)
    else:
        existing.insert(max(position, 0), entry)
    config["profiles"] = existing
    write_config(settings, config)
    return entry


def remove_subscription(settings: Settings, ident: str) -> dict[str, Any]:
    """Drop a subscription. Call inside a RestartWindow."""
    config = read_config(settings)
    target = find_profile(config, ident)
    if str(target.get("id")) == current_profile_id(config):
        raise StoreError(
            "Refusing to delete the active profile; switch to another one first."
        )
    config["profiles"] = [
        profile for profile in profiles(config) if profile is not target
    ]
    write_config(settings, config)
    return target

