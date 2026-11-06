"""Process-level control of the Bettbox desktop app.

Facts from the Bettbox source that drive this module
----------------------------------------------------
* ``lib/common/external_control.dart`` opens a line-oriented control socket
  (Unix domain socket first, loopback TCP port file as fallback) accepting
  exactly ``exit`` / ``restart`` / ``show``. ``ExternalControl.stop()`` deletes
  the socket file on shutdown (``application.dart:562``), so the file's
  existence is a usable liveness signal.
* ``lib/main.dart:32`` maps the CLI flags ``--exit`` / ``--restart`` onto those
  same socket commands. ``--quick`` / ``--boot`` are Android-only.
* CPython on Windows has no ``socket.AF_UNIX``, so the CLI flags are the primary
  transport there. They work on macOS/Linux too, so they are used everywhere
  the executable is known; a raw socket is only a fallback.

There is no IPC channel for "start/stop proxying"; that button lives purely
inside the Flutter UI. Process-level control is therefore the only exact,
cross-platform, patch-free way to stop the proxy from the outside.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import WINDOWS_EXE, Settings

EXIT_TIMEOUT_S = 40.0
START_TIMEOUT_S = 60.0

# CPython only exposes AF_UNIX where the platform supports it.
_AF_UNIX = getattr(socket, "AF_UNIX", None)


class AppControlError(RuntimeError):
    """Raised when the app cannot be controlled on this machine."""


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------
def _send_unix_socket(path: Path, command: str) -> None:
    """Fire-and-forget: the control server never replies."""
    if _AF_UNIX is None:
        raise AppControlError("Unix domain sockets are unavailable on this platform.")
    client = socket.socket(_AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(3.0)
        client.connect(str(path))
        client.sendall(f"{command}\n".encode("utf-8"))
    finally:
        client.close()


def _send_tcp(port: int, command: str) -> None:
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(3.0)
        client.connect(("127.0.0.1", port))
        client.sendall(f"{command}\n".encode("utf-8"))
    finally:
        client.close()


def _send_via_cli(settings: Settings, command: str) -> bool:
    """Let the app deliver its own message: ``Bettbox.exe --exit`` / ``--restart``.

    A fresh process starts, sends the command over the socket, and exits. This
    is the officially supported desktop path and works regardless of whether
    Python can talk AF_UNIX.
    """
    if not settings.executable or not settings.executable.exists():
        return False
    flag = f"--{command}"
    try:
        result = subprocess.run(
            [str(settings.executable), flag],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # The CLI helper always exits 0; success is decided by the caller waiting
    # for the socket to disappear.
    return result.returncode == 0


def send_command(settings: Settings, command: str) -> None:
    """Send ``exit`` / ``restart`` / ``show`` to the running instance.

    Transport order differs per platform because the capabilities differ:

    * POSIX -- talk the Unix domain socket directly. This is the same channel
      Bettbox's own CLI flag uses internally, but without spawning a second
      process, which makes it work even where process spawning is restricted.
    * Windows -- CPython has no ``socket.AF_UNIX``, so the only option is the
      app's own ``--exit`` / ``--restart`` helper process.
    """
    errors: list[str] = []

    if _AF_UNIX is not None:
        sock = settings.control_sock_path
        if sock.exists():
            try:
                _send_unix_socket(sock, command)
                return
            except OSError as exc:
                errors.append(f"unix socket: {exc}")

        port_path = settings.control_port_path
        if port_path.is_file():
            try:
                port = int(port_path.read_text(encoding="utf-8").strip())
                if port > 0:
                    _send_tcp(port, command)
                    return
            except (OSError, ValueError) as exc:
                errors.append(f"tcp port file: {exc}")

    if _send_via_cli(settings, command):
        return
    errors.append("cli flag: no executable configured or it could not be launched")

    raise AppControlError(
        "Could not reach Bettbox. The app may not be running, or it is a "
        "portable build (portable builds disable the control socket). "
        f"Attempts: {'; '.join(errors)}"
    )


# --------------------------------------------------------------------------
# liveness
# --------------------------------------------------------------------------
def _controllable(settings: Settings) -> bool:
    """Legacy weak probe kept for diagnostics; NOT used for liveness.

    The socket file survives a clean exit (verified), so it must never be
    treated as proof that the app is running. ``is_process_running`` uses the
    executable lock instead.
    """
    return settings.control_sock_path.exists() or settings.control_port_path.is_file()


def _posix_process_running(settings: Settings) -> bool:
    """POSIX liveness: scan ``/proc`` on Linux, fall back to ``pgrep``.

    Unlike Windows, the process table is not filtered here, so a plain scan is
    both cheap and accurate. The name match is intentionally loose because the
    binary may be ``bettbox``, ``Bettbox``, an AppImage mount path, or a
    flatpak wrapper.
    """
    if sys.platform == "linux":
        proc = Path("/proc")
        try:
            entries = list(proc.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if b"bettbox" in cmdline.lower():
                return True
        return False

    for pattern in (
        settings.executable.name if settings.executable else "Bettbox",
        "Bettbox",
    ):
        try:
            out = subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if out.strip():
            return True
    return False


def _process_visible(settings: Settings) -> bool:
    """Best-effort process probe (Windows only; the view may be restricted).

    Kept as a last resort on Windows and never as the primary signal.
    """
    if sys.platform != "win32":
        return _posix_process_running(settings)
    exe_name = settings.executable.name if settings.executable else WINDOWS_EXE
    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return exe_name.lower() in output.lower()


def _exe_locked(settings: Settings) -> bool:
    """Windows liveness probe: a running .exe cannot be opened for writing.

    This is the only reliable signal available to this process. Verified
    facts that rule out the alternatives:

    * ``tasklist`` / ``EnumProcesses`` -- the MCP server may run in a
      restricted context and sees only a subset of the process table
      (measured: 54 processes visible, Bettbox absent while it was running).
    * the control socket *file* -- it survives a clean exit, so its presence
      proves nothing (measured after ``--exit``).
    """
    if not settings.executable:
        return False
    try:
        handle = os.open(str(settings.executable), os.O_RDWR)
    except FileNotFoundError:
        return False
    except PermissionError:
        return True  # locked by the running process
    except OSError:
        return False
    else:
        os.close(handle)
        return False


def _controller_alive(settings: Settings) -> bool:
    """True when the mihomo REST API answers (core running, proxy on)."""
    host, _, port = settings.controller_addr().rpartition(":")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect((host or "127.0.0.1", int(port)))
        return True
    except (OSError, ValueError):
        return False
    finally:
        probe.close()


def is_process_running(settings: Settings) -> bool:
    """Is the Bettbox app running right now?

    POSIX (the primary target): a direct AF_UNIX connect to the control socket
    is authoritative, with a ``/proc`` scan as the fallback for the window
    where the app is up but the socket is not yet bound.

    Windows: the executable lock, because the control socket file survives a
    clean exit and the process table may be filtered by the host sandbox.
    """
    if sys.platform == "win32":
        return _exe_locked(settings)

    if _AF_UNIX is not None:
        if _can_connect_unix(settings.control_sock_path):
            return True
        if _can_connect_tcp(settings.control_port_path):
            return True
    return _posix_process_running(settings)


async def wait_for_exit(settings: Settings, timeout: float = EXIT_TIMEOUT_S) -> bool:
    """Poll until the app is gone; returns True if it exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_running(settings):
            return True
        await asyncio.sleep(0.3)
    return not is_process_running(settings)


async def exit_app(settings: Settings, timeout: float = EXIT_TIMEOUT_S) -> bool:
    """Gracefully quit. Bettbox restores system proxy / DNS / TUN on this path.

    The command is retried once: a long-running instance occasionally needs a
    second nudge, and a false negative here would make the caller refuse a
    legitimate config edit.
    """
    last_error: AppControlError | None = None
    for attempt, budget in enumerate((timeout * 0.4, timeout * 0.6)):
        try:
            send_command(settings, "exit")
        except AppControlError as exc:
            last_error = exc
            break
        if await wait_for_exit(settings, budget):
            return True
    if not is_process_running(settings):
        return True
    if last_error is not None:
        raise last_error
    return False


# --------------------------------------------------------------------------
# starting
# --------------------------------------------------------------------------
def _display_available() -> bool:
    """A GUI app needs a display server; a headless session cannot open one."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _spawn(settings: Settings, args: list[str] | None = None) -> None:
    if not settings.executable or not settings.executable.exists():
        raise AppControlError(
            "Bettbox executable not found. Set the BETTBOX_EXE environment "
            "variable to the full path of the Bettbox binary."
        )
    if sys.platform.startswith("linux") and not _display_available():
        raise AppControlError(
            "No DISPLAY or WAYLAND_DISPLAY in this environment, so the Bettbox "
            "GUI cannot be started from here. Run the MCP server inside the "
            "desktop session (or export DISPLAY=:0) and retry."
        )
    if sys.platform.startswith("linux"):
        # A downloaded AppImage is not executable until chmod +x.
        try:
            mode = settings.executable.stat().st_mode
            if not mode & 0o111:
                settings.executable.chmod(mode | 0o755)
        except OSError:
            pass
    subprocess.Popen(
        [str(settings.executable), *(args or [])],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if sys.platform == "win32"
        else 0,
    )


async def start_app(settings: Settings) -> None:
    """Launch the app.

    On desktop, starting the app also starts proxying when a profile exists and
    ``appSetting.autoRun`` is true (``controller.dart:1440``). There is no CLI
    flag to open the UI without starting the proxy, so this is Bettbox's
    documented behaviour rather than a side effect of this MCP.
    """
    if is_process_running(settings):
        return
    _spawn(settings)


async def restart_app(settings: Settings, timeout: float = EXIT_TIMEOUT_S) -> bool:
    """Restart through the control socket, falling back to exit + start."""
    if not is_process_running(settings):
        await start_app(settings)
        return True
    try:
        send_command(settings, "restart")
    except AppControlError:
        pass
    if await wait_for_exit(settings, timeout):
        await start_app(settings)
        return True
    return False


# --------------------------------------------------------------------------
# restart window
# --------------------------------------------------------------------------
class RestartWindow:
    """Context manager: quit the app, run file mutations, then start it again.

    Bettbox writes its in-memory ``Config`` back to ``config.json`` on every
    graceful exit (``savePreferences()``), so any edit made while the app is
    running is silently overwritten. Mutations to ``config.json`` must happen
    strictly inside this window. ``profiles/*.yaml`` is not rewritten on exit
    and may be updated without a restart.
    """

    def __init__(self, settings: Settings, *, restart: bool = True) -> None:
        self._settings = settings
        self._restart = restart
        self.exited = False
        self.backup: Path | None = None

    async def __aenter__(self) -> "RestartWindow":
        if is_process_running(self._settings):
            self.exited = await exit_app(self._settings)
            if not self.exited:
                raise AppControlError(
                    "Bettbox did not exit within the timeout; refusing to edit "
                    "config.json because the app would overwrite the change."
                )
        else:
            self.exited = True
        self.backup = _backup_config(self._settings)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and self.backup and self.backup.exists():
            shutil.copy2(self.backup, self._settings.config_path)
        if self._restart:
            await start_app(self._settings)
        return False


def _backup_config(settings: Settings) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = settings.config_path.with_suffix(f".json.mcp-bak-{stamp}")
    shutil.copy2(settings.config_path, backup)
    return backup
