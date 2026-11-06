"""Thin async client for the mihomo REST API exposed by Bettbox.

All calls are direct to loopback (``trust_env=False``) so a system-wide proxy
config can never redirect them. Only the subscription download in
``store.py`` deliberately goes through the proxy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .config import Settings

DEFAULT_TIMEOUT = 10.0
DELAY_TIMEOUT_MS = 5000
DELAY_TEST_URL = "http://www.gstatic.com/generate_204"


class ClashApiError(RuntimeError):
    """Raised when the controller is unreachable or returns an error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ClashClient:
    """Minimal, typed wrapper over the endpoints this MCP needs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base = f"http://{settings.controller_addr()}"

    # --- low level -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        secret = self._settings.controller_secret()
        return {"Authorization": f"Bearer {secret}"} if secret else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self._base,
                timeout=timeout,
                trust_env=False,  # never route the control channel via a proxy
            ) as client:
                response = await client.request(
                    method, path, headers=self._headers(), json=json_body
                )
        except httpx.ConnectError as exc:
            raise ClashApiError(
                "The Bettbox controller is not reachable at "
                f"{self._base}. Enable it in Bettbox: Settings -> General -> "
                "'External controller', then restart the app."
            ) from exc
        except httpx.TimeoutException as exc:
            raise ClashApiError(f"Timed out talking to {self._base}{path}") from exc

        if response.status_code == 401:
            raise ClashApiError(
                "The controller rejected the request (401). The 'secret' in "
                "config.json does not match the running core; restart Bettbox.",
                status_code=401,
            )
        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase
            raise ClashApiError(
                f"{method} {path} failed with HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # --- read ----------------------------------------------------------
    async def version(self) -> dict[str, Any]:
        return await self._request("GET", "/version")

    async def configs(self) -> dict[str, Any]:
        return await self._request("GET", "/configs")

    async def proxies(self) -> dict[str, Any]:
        data = await self._request("GET", "/proxies")
        return data.get("proxies", {}) if isinstance(data, dict) else {}

    async def proxy(self, name: str) -> dict[str, Any]:
        data = await self._request("GET", f"/proxies/{_quote(name)}")
        return data if isinstance(data, dict) else {}

    async def connections(self) -> dict[str, Any]:
        return await self._request("GET", "/connections")

    async def rules(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/rules")
        return data.get("rules", []) if isinstance(data, dict) else []

    # --- write ---------------------------------------------------------
    async def select_proxy(self, group: str, proxy_name: str) -> None:
        await self._request(
            "PUT", f"/proxies/{_quote(group)}", json_body={"name": proxy_name}
        )

    async def set_mode(self, mode: str) -> None:
        await self._request("PATCH", "/configs", json_body={"mode": mode})

    async def set_tun(self, enable: bool) -> dict[str, Any]:
        """Toggle TUN for the current run only.

        Runtime changes are *not* persisted: Bettbox rebuilds the effective
        config from ``config.json`` on the next start or profile apply.
        """
        current = (await self.configs()).get("tun") or {}
        payload = {**current, "enable": enable}
        await self._request("PATCH", "/configs", json_body={"tun": payload})
        return payload

    async def close_all_connections(self) -> None:
        await self._request("DELETE", "/connections")

    async def close_connection(self, connection_id: str) -> None:
        await self._request("DELETE", f"/connections/{_quote(connection_id)}")

    async def flush_dns(self) -> None:
        await self._request("POST", "/dns/flush")

    async def flush_fakeip(self) -> None:
        await self._request("POST", "/fakeip/flush")

    # --- delay test ----------------------------------------------------
    async def proxy_delay(self, name: str, timeout_ms: int = DELAY_TIMEOUT_MS) -> int | None:
        """Return latency in ms, or ``None`` when the node fails the test."""
        try:
            data = await self._request(
                "GET",
                f"/proxies/{_quote(name)}/delay"
                f"?timeout={timeout_ms}&url={_quote(DELAY_TEST_URL)}",
                timeout=(timeout_ms / 1000) + 5,
            )
        except ClashApiError:
            return None
        if isinstance(data, dict):
            delay = data.get("delay")
            return delay if isinstance(delay, int) and delay > 0 else None
        return None

    async def group_delay(self, name: str, timeout_ms: int = DELAY_TIMEOUT_MS) -> dict[str, int | None]:
        """Test every member of a group; returns {node name: delay ms}.

        The route is ``/group/{name}/delay`` (singular); ``/groups/`` returns 404.
        """
        try:
            data = await self._request(
                "GET",
                f"/group/{_quote(name)}/delay"
                f"?timeout={timeout_ms}&url={_quote(DELAY_TEST_URL)}",
                timeout=(timeout_ms / 1000) + 15,
            )
        except ClashApiError:
            return {}
        return data if isinstance(data, dict) else {}

    # --- composite -----------------------------------------------------
    async def is_running(self) -> bool:
        """Controller reachable == core running with the controller enabled."""
        try:
            await asyncio.wait_for(self.version(), timeout=3.0)
            return True
        except (ClashApiError, asyncio.TimeoutError):
            return False


def _quote(value: str) -> str:
    """Percent-encode a path segment (node and group names are arbitrary text)."""
    return httpx.URL(path=f"/{value}").raw_path.decode().removeprefix("/")
