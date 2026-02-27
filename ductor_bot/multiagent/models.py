"""Data models for multi-agent configuration."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from ductor_bot.config import (
    AgentConfig,
    ApiConfig,
    CLIParametersConfig,
    CleanupConfig,
    DockerConfig,
    HeartbeatConfig,
    StreamingConfig,
    WebhookConfig,
)

logger = logging.getLogger(__name__)


class SubAgentConfig(BaseModel):
    """Minimal sub-agent definition from agents.json.

    Only ``name``, ``telegram_token``, and ``allowed_user_ids`` are required.
    All other fields are optional and inherit from the main agent config.
    """

    name: str
    telegram_token: str
    allowed_user_ids: list[int] = Field(default_factory=list)

    # Optional overrides — inherit from main agent if None
    provider: str | None = None
    model: str | None = None
    log_level: str | None = None
    idle_timeout_minutes: int | None = None
    session_age_warning_hours: int | None = None
    daily_reset_hour: int | None = None
    daily_reset_enabled: bool | None = None
    max_budget_usd: float | None = None
    max_turns: int | None = None
    max_session_messages: int | None = None
    permission_mode: str | None = None
    cli_timeout: float | None = None
    reasoning_effort: str | None = None
    file_access: str | None = None
    streaming: StreamingConfig | None = None
    docker: DockerConfig | None = None
    heartbeat: HeartbeatConfig | None = None
    cleanup: CleanupConfig | None = None
    webhooks: WebhookConfig | None = None
    api: ApiConfig | None = None
    cli_parameters: CLIParametersConfig | None = None
    user_timezone: str | None = None
    update_check: bool | None = None
    group_mention_only: bool | None = None


def merge_sub_agent_config(
    main: AgentConfig,
    sub: SubAgentConfig,
    agent_home: Path,
) -> AgentConfig:
    """Create a full AgentConfig by merging main config with sub-agent overrides.

    Sub-agent fields that are ``None`` inherit the main agent value.
    ``ductor_home`` is always set to *agent_home* and ``telegram_token`` /
    ``allowed_user_ids`` always come from the sub-agent definition.
    """
    base = main.model_dump()
    overrides = sub.model_dump(exclude_none=True, exclude={"name"})
    base.update(overrides)
    base["ductor_home"] = str(agent_home)
    base["telegram_token"] = sub.telegram_token
    base["allowed_user_ids"] = sub.allowed_user_ids
    return AgentConfig(**base)
