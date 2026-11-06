"""Locate the Bettbox installation and its data directory across platforms.

Verification status
-------------------
* Windows : verified against Bettbox 1.19.7
* Linux   : paths inferred from Flutter ``getApplicationSupportDirectory``
            (``$XDG_DATA_HOME/<app>``); a generic scan fallback covers
            deb / rpm / AppImage / snap / flatpak layouts. Run
            ``tests/platform_probe.py`` on the target box to confirm.
* macOS   : same caveat as Linux.

The data directory always contains ``config.json``; that file is used as the
authoritative marker, and it is shape-checked so a wrong guess fails loudly
instead of silently editing an unrelated JSON file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_ID = "com.appshub"
APP_NAME = "Bettbox"

WINDOWS_EXE = "Bettbox.exe"

# Bettbox's own UA when fetching a subscription. Providers pick between Clash
# YAML and a node list based on it, so it must match exactly.
# Source: lib/common/package.dart -> 'FlClash/ClashMetaForAndroid/2.11.33.Bettbox'
SUBSCRIPTION_USER_AGENT = "FlClash/ClashMetaForAndroid/2.11.33.Bettbox"

DEFAULT_EXTERNAL_CONTROLLER = "127.0.0.1:9090"

# Keys that must exist in a real Bettbox config.json. Used to reject false
# positives during discovery.
_CONFIG_MARKERS = ("patchClashConfig", "profiles", "appSetting")


class BettboxNotConfigured(RuntimeError):
    """Raised when the Bettbox data directory cannot be located."""


# --------------------------------------------------------------------------
# config.json shape check
# --------------------------------------------------------------------------
def looks_like_bettbox_config(path: Path) -> bool:
    """Cheap shape probe used during discovery; never raises."""
    try:
        if path.stat().st_size < 200:
            return False
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and all(marker in data for marker in _CONFIG_MARKERS)


# --------------------------------------------------------------------------
# data directory discovery
# --------------------------------------------------------------------------
def _xdg_data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value).expanduser() if value else Path.home() / ".local" / "share"


def _linux_candidates() -> list[Path]:
    """Candidate data dirs across deb/rpm, AppImage, snap and flatpak installs."""
    home = Path.home()
    xdg = _xdg_data_home()
    names = (
        APP_NAME.lower(),          # path_provider_linux uses the exe basename
        APP_NAME,
        "bettbox",
        f"{APP_ID}.{APP_NAME}",
        f"{APP_ID}.{APP_NAME.lower()}",
        f"{APP_ID}/{APP_NAME}",
        f"{APP_ID}/{APP_NAME.lower()}",
    )
    out: list[Path] = []
    for root in (xdg, home / ".local" / "share", home / ".config", home / ".bettbox"):
        for name in names:
            out.append(root / name)
    # Flatpak / snap sandboxes.
    for base in (home / ".var" / "app", Path("/var/lib/flatpak/app")):
        if base.is_dir():
            try:
                for entry in base.iterdir():
                    if APP_NAME.lower() in entry.name.lower():
                        out.append(entry / "data" / APP_NAME.lower())
                        out.append(entry / "data" / f"{APP_ID}.{APP_NAME}")
            except OSError:
                pass
    for entry in (home / "snap").glob("*") if (home / "snap").is_dir() else []:
        if APP_NAME.lower() in entry.name.lower():
            out.append(entry / "current" / ".local" / "share" / APP_NAME.lower())
            out.append(entry / "current" / ".local" / "share" / f"{APP_ID}.{APP_NAME}")
    return out


def _generic_scan() -> list[Path]:
    """Last resort: look for any Bettbox-shaped config.json in data roots.

    Covers installs whose directory name we cannot predict (custom packaging,
    translated names, future path_provider changes).
    """
    home = Path.home()
    roots = [
        _xdg_data_home(),
        home / ".local" / "share",
        home / ".config",
        home / "Library" / "Application Support",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or "bett" not in child.name.lower():
                continue
            candidate = child / "config.json"
            if looks_like_bettbox_config(candidate):
                found.append(child)
    return found


def _candidate_data_dirs() -> list[Path]:
    home = Path.home()
    out: list[Path] = []

    override = os.environ.get("BETTBOX_HOME")
    if override:
        out.append(Path(override).expanduser())

    if sys.platform == "win32":
        for env in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                out.append(Path(base) / APP_ID / APP_NAME)
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        out += [
            support / f"{APP_ID}.{APP_NAME}",
            support / APP_ID / APP_NAME,
            support / APP_NAME,
        ]
    else:
        out += _linux_candidates()

    out += _generic_scan()

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in out:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


# --------------------------------------------------------------------------
# executable discovery
# --------------------------------------------------------------------------
def running_executable() -> Path | None:
    """Resolve the path of the *running* Bettbox binary, if any.

    On Linux this matters for AppImage: ``/proc/<pid>/exe`` points into an
    ephemeral ``/tmp/.mount_*`` directory, while the ``APPIMAGE`` environment
    variable of that process holds the real file we must relaunch.
    """
    if sys.platform == "linux":
        proc = Path("/proc")
        if not proc.is_dir():
            return None
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                exe_link = os.readlink(entry / "exe")
            except OSError:
                continue
            haystack = (cmdline.replace(b"\0", b" ") + exe_link.encode("utf-8", "replace")).lower()
            if b"bettbox" not in haystack:
                continue
            try:
                environ = (entry / "environ").read_bytes().split(b"\0")
                for item in environ:
                    if item.startswith(b"APPIMAGE="):
                        return Path(item.split(b"=", 1)[1].decode("utf-8", "replace"))
            except OSError:
                pass
            return Path(exe_link)
        return None

    if sys.platform == "darwin":
        try:
            import subprocess

            pid = subprocess.run(
                ["pgrep", "-f", "Bettbox"], capture_output=True, text=True, timeout=10
            ).stdout.split()
        except (OSError, Exception):
            return None
        for candidate in pid:
            try:
                return Path(os.readlink(f"/proc/{candidate}/exe"))
            except OSError:
                continue
        for guess in (
            Path("/Applications/Bettbox.app/Contents/MacOS/Bettbox"),
            Path.home() / "Applications" / "Bettbox.app" / "Contents" / "MacOS" / "Bettbox",
        ):
            if guess.exists():
                return guess
        return None

    return None


def _candidate_executables(data_dir: Path) -> list[Path]:
    found: list[Path] = []

    override = os.environ.get("BETTBOX_EXE")
    if override:
        found.append(Path(override).expanduser())

    running = running_executable()
    if running and running.exists():
        found.append(running)

    if sys.platform == "win32":
        for root in (
            Path(r"D:\Program Files\Bettbox"),
            Path(r"C:\Program Files\Bettbox"),
            Path(r"C:\Program Files (x86)\Bettbox"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Bettbox",
            Path(os.environ.get("LOCALAPPDATA", "")) / APP_NAME,
        ):
            candidate = root / WINDOWS_EXE
            if candidate.exists():
                found.append(candidate)
    elif sys.platform == "darwin":
        for guess in (
            Path("/Applications/Bettbox.app/Contents/MacOS/Bettbox"),
            Path.home() / "Applications" / "Bettbox.app" / "Contents" / "MacOS" / "Bettbox",
        ):
            if guess.exists():
                found.append(guess)
    else:
        from shutil import which

        for name in ("bettbox", "Bettbox"):
            resolved = which(name)
            if resolved:
                found.append(Path(resolved))
        for guess in (
            Path("/usr/bin/bettbox"),
            Path("/usr/local/bin/bettbox"),
            Path("/opt/Bettbox/bettbox"),
            Path("/opt/bettbox/bettbox"),
            Path("/app/bin/bettbox"),  # flatpak
        ):
            if guess.exists():
                found.append(guess)
        apps = Path.home() / "Applications"
        if apps.is_dir():
            found += sorted(apps.glob("*ettbox*.AppImage"))
        found += sorted(Path.home().glob("*ettbox*.AppImage"))

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Resolved locations and runtime knobs for one machine."""

    data_dir: Path
    executable: Path | None

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def prefs_path(self) -> Path:
        return self.data_dir / "shared_preferences.json"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def scripts_dir(self) -> Path:
        return self.data_dir / "scripts"

    @property
    def control_sock_path(self) -> Path:
        return self.data_dir / f"{APP_NAME}.control.sock"

    @property
    def control_port_path(self) -> Path:
        return self.data_dir / f"{APP_NAME}.control.port"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / f"{APP_NAME}.lock"

    def profile_path(self, profile_id: str) -> Path:
        return self.profiles_dir / f"{profile_id}.yaml"

    def read_config(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def patch_clash_config(self) -> dict[str, Any]:
        return self.read_config().get("patchClashConfig") or {}

    def controller_secret(self) -> str:
        """The REST secret lives only in config.json; the API never echoes it."""
        secret = self.patch_clash_config().get("secret")
        return secret if isinstance(secret, str) else ""

    def controller_addr(self) -> str:
        addr = self.patch_clash_config().get("external-controller")
        if not isinstance(addr, str) or not addr:
            return DEFAULT_EXTERNAL_CONTROLLER
        host, _, port = addr.rpartition(":")
        if not port:
            return DEFAULT_EXTERNAL_CONTROLLER
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"{host}:{port}"

    def mixed_port(self) -> int:
        patch = self.patch_clash_config()
        for key in ("mixed-port", "port"):
            value = patch.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return 10808


def load_settings() -> Settings:
    """Resolve the Bettbox installation, raising a helpful error when missing."""
    for data_dir in _candidate_data_dirs():
        if not looks_like_bettbox_config(data_dir / "config.json"):
            continue
        executables = _candidate_executables(data_dir)
        return Settings(data_dir=data_dir, executable=executables[0] if executables else None)

    tried = "\n".join(f"  - {p}" for p in _candidate_data_dirs())
    raise BettboxNotConfigured(
        "Could not find the Bettbox data directory (looked for a config.json "
        f"containing {', '.join(_CONFIG_MARKERS)}).\n"
        f"Tried:\n{tried}\n"
        "Set BETTBOX_HOME to the folder that contains config.json "
        "(on Linux typically ~/.local/share/bettbox)."
    )


def platform_report() -> str:
    """Human-readable diagnostic used by ``tests/platform_probe.py``."""
    import socket

    lines = [
        f"platform       : {sys.platform}",
        f"python         : {sys.version.split()[0]}",
        f"AF_UNIX        : {hasattr(socket, 'AF_UNIX')}",
        f"XDG_DATA_HOME  : {os.environ.get('XDG_DATA_HOME') or '(unset)'}",
        f"APPIMAGE       : {os.environ.get('APPIMAGE') or '(unset)'}",
        f"XDG_SESSION    : {os.environ.get('XDG_SESSION_TYPE') or '(unset)'}",
        "candidate dirs :",
    ]
    lines += [f"  - {p}{'  <== valid' if looks_like_bettbox_config(p / 'config.json') else ''}"
              for p in _candidate_data_dirs()[:12]]
    lines.append(f"running exe    : {running_executable() or '(not found)'}")
    try:
        settings = load_settings()
    except BettboxNotConfigured as exc:
        lines.append(f"settings       : NOT FOUND\n{exc}")
        return "\n".join(lines)
    lines += [
        f"data dir       : {settings.data_dir}",
        f"executable     : {settings.executable or '(not found)'}",
        f"controller     : {settings.controller_addr()}",
        f"mixed port     : {settings.mixed_port()}",
        f"secret stored  : {bool(settings.controller_secret())}",
        f"control sock   : {settings.control_sock_path} "
        f"({'exists' if settings.control_sock_path.exists() else 'missing'})",
        f"control port   : {settings.control_port_path} "
        f"({'exists' if settings.control_port_path.exists() else 'missing'})",
    ]
    return "\n".join(lines)


_settings_cache: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Cached accessor; the data dir does not move during a session."""
    global _settings_cache
    if refresh or _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache
