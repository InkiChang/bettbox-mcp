"""Pydantic input models for every MCP tool."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


# --------------------------------------------------------------------------
# read-only
# --------------------------------------------------------------------------
class StatusInput(_Base):
    """Input for the overall status probe."""

    include_traffic: bool = Field(
        default=True,
        description="Include cumulative upload/download counters from /connections.",
    )


class ListSubscriptionsInput(_Base):
    """Input for listing configured subscriptions."""

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for a readable table, 'json' for machine-readable output.",
    )
    reveal_urls: bool = Field(
        default=False,
        description=(
            "Return full subscription URLs. Off by default because these URLs "
            "usually embed an account token."
        ),
    )


class ListProxyGroupsInput(_Base):
    """Input for listing proxy groups."""

    only_selectable: bool = Field(
        default=True,
        description="Return only selectable groups (Selector / URLTest / Fallback).",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class ListProxiesInput(_Base):
    """Input for listing the nodes inside one group."""

    group: str = Field(
        ...,
        description="Exact group name, e.g. 'Proxies', 'HK', 'GLOBAL'. "
        "Use bettbox_list_proxy_groups to discover names.",
        min_length=1,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class TestDelayInput(_Base):
    """Input for latency testing."""

    name: str = Field(
        ...,
        description="Group or node name. Groups test every member concurrently.",
        min_length=1,
    )
    kind: Literal["auto", "group", "proxy"] = Field(
        default="auto",
        description=(
            "'auto' detects whether the name is a group. Use 'group' to test all "
            "members, 'proxy' for one node."
        ),
    )
    timeout_ms: int = Field(
        default=5000,
        ge=500,
        le=30000,
        description="Per-node timeout in milliseconds.",
    )


class ListConnectionsInput(_Base):
    """Input for listing live connections."""

    limit: int = Field(default=20, ge=1, le=200, description="Maximum rows to return.")
    sort_by: Literal["download", "upload", "start"] = Field(
        default="download", description="Sort order for the returned connections."
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# --------------------------------------------------------------------------
# write - runtime (hot, no restart)
# --------------------------------------------------------------------------
class SelectProxyInput(_Base):
    """Input for switching the active node of a group."""

    group: str = Field(..., description="Group name, e.g. 'Proxies'.", min_length=1)
    proxy: str = Field(..., description="Target node name inside that group.", min_length=1)


class SetModeInput(_Base):
    """Input for changing the outbound routing mode."""

    mode: Literal["rule", "global", "direct"] = Field(
        ...,
        description=(
            "'rule' honours the rule list, 'global' sends everything through the "
            "selected node, 'direct' bypasses the proxy."
        ),
    )


class SetTunInput(_Base):
    """Input for toggling TUN at runtime."""

    enable: bool = Field(..., description="True to enable TUN, False to disable it.")


class CloseConnectionInput(_Base):
    """Input for closing one connection."""

    connection_id: str = Field(..., min_length=1, description="Connection id from bettbox_list_connections.")


# --------------------------------------------------------------------------
# write - subscriptions
# --------------------------------------------------------------------------
class UpdateSubscriptionInput(_Base):
    """Input for refreshing one or more subscriptions."""

    profiles: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Profile ids or labels, e.g. ['ReShare'] or ['1783062752747']. "
            "These are matched case-insensitively against the label."
        ),
    )
    sync_metadata: bool = Field(
        default=False,
        description=(
            "Also refresh lastUpdateDate / subscriptionInfo in config.json. "
            "Requires one app restart, because Bettbox rewrites config.json on exit."
        ),
    )
    validate_only: bool = Field(
        default=False,
        description="Download and validate without writing anything to disk.",
    )


class SyncAllSubscriptionsInput(_Base):
    """Input for the equivalent of the app's "sync all" button."""

    sync_metadata: bool = Field(
        default=False,
        description="Also refresh dates and quotas in config.json (needs one restart).",
    )
    concurrency: int = Field(
        default=6, ge=1, le=16, description="How many downloads to run in parallel."
    )


class AddSubscriptionInput(_Base):
    """Input for registering a new subscription."""

    url: str = Field(..., min_length=8, description="Subscription URL (http/https).")
    label: str | None = Field(
        default=None, max_length=80, description="Display name. Defaults to the profile id."
    )
    position: int | None = Field(
        default=None, ge=0, description="Insert index; appended when omitted."
    )
    download: bool = Field(
        default=True,
        description="Fetch the config immediately and write profiles/<id>.yaml.",
    )

    @field_validator("url")
    @classmethod
    def _check_scheme(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return value


class RemoveSubscriptionInput(_Base):
    """Input for deleting a subscription."""

    profile: str = Field(..., min_length=1, description="Profile id or label to remove.")
    keep_files: bool = Field(
        default=False,
        description="Keep profiles/<id>.yaml on disk instead of deleting it.",
    )


# --------------------------------------------------------------------------
# write - process level
# --------------------------------------------------------------------------
class RestartAppInput(_Base):
    """Input for restarting the Bettbox app."""

    reason: str | None = Field(
        default=None, max_length=200, description="Optional note for the audit trail."
    )


class StopAppInput(_Base):
    """Input for quitting the app (which also stops the proxy)."""

    reason: str | None = Field(default=None, max_length=200)
