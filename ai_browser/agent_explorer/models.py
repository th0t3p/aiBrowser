"""Pydantic models for agent_explorer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ActionType(str, Enum):
    CLICK = "click"
    FILL = "fill"
    SUBMIT = "submit"
    NAVIGATE = "navigate"
    SCROLL = "scroll"
    WAIT = "wait"


class AgentAction(BaseModel):
    """A single action taken by the agent explorer."""

    action_type: ActionType
    target_selector: Optional[str] = Field(
        default=None,
        description="CSS selector or accessibility identifier of the target element.",
    )
    target_text: Optional[str] = Field(
        default=None,
        description="Visible text or label of the target element, for audit trail readability.",
    )
    input_value: Optional[str] = Field(
        default=None,
        description="Value to fill in (only for FILL action).",
    )
    current_url: str = Field(..., description="The page URL when the action was taken.")
    reasoning: str = Field(
        default="",
        description="The Claude-provided reasoning for this action.",
    )


class AuditLogEntry(BaseModel):
    """A logged entry recording an autonomous action taken by the agent."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action: AgentAction
    success: bool = Field(default=True)
    error_message: Optional[str] = Field(default=None)


class ExplorerConfig(BaseModel):
    """Configuration for the AgentExplorer."""

    authorized_hostname: str = Field(
        ...,
        description="Same scope guard as BrowserSession.",
    )

    # New multi-provider LLM fields (preferred)
    llm_provider: str = Field(
        default="anthropic",
        description="LLM provider: anthropic | openai | deepseek",
    )
    llm_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Model identifier for the chosen provider.",
    )
    llm_api_key: str = Field(
        default="",
        description="API key for the LLM provider. Falls back to anthropic_api_key.",
    )
    llm_base_url: str = Field(
        default="",
        description="Custom base URL. Falls back to provider default if empty.",
    )

    # Deprecated aliases — kept for backward compat, map onto the new fields
    anthropic_api_key: str = Field(
        default="",
        description="[Deprecated] Use llm_api_key. Sets llm_provider='anthropic'.",
    )
    anthropic_model: str = Field(
        default="",
        description="[Deprecated] Use llm_model.",
    )
    max_actions: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Maximum number of autonomous actions before giving up.",
    )
    audit_log_path: Path = Field(
        default=Path("storage/audit_logs"),
        description="Directory to write per-session audit log files.",
    )
    action_delay_ms: int = Field(
        default=500,
        description="Delay between actions to avoid hammering the target.",
    )

    # Safety: by default, borderline actions require human confirmation.
    # Set allow_unattended=True to let the agent operate fully autonomously.
    allow_unattended: bool = Field(
        default=False,
        description="If True, borderline actions (save/confirm/update/submit) proceed "
        "without human confirmation. Default False (fail-closed: denied unless a "
        "confirmation callback approves them).",
    )

    # Denylist patterns for destructive actions
    destructive_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?i)\bdelete\s+account\b",
            r"(?i)\bcancel\s+subscription\b",
            r"(?i)\bconfirm\s+purchase\b",
            r"(?i)\bpay\s+now\b",
            r"(?i)\bcheckout\b",
            r"(?i)\bremove\s+all\b",
            r"(?i)\bwipe\b",
            r"(?i)\bdestroy\b",
            r"(?i)\bpermanently\s+delete\b",
        ],
    )

    # Registration patterns — handled separately from destructive patterns.
    # When allow_registration=False (default), these trigger the confirmation
    # path. When allow_registration=True and registration_config is set, the
    # agent delegates the full signup flow to RegistrationHandler.
    registration_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?i)\bsign\s*up\b",
            r"(?i)\bcreate\s+account\b",
            r"(?i)\bregister\b",
            r"(?i)\bget\s+started\b",
            r"(?i)\bjoin\s+now\b",
        ],
    )
    allow_registration: bool = Field(
        default=False,
        description="If True, the agent may autonomously fill and submit registration "
        "forms encountered during exploration, using registration_config for the values "
        "to fill. If False (default), matching elements require confirmation and are "
        "skipped without one.",
    )
    registration_config: Optional[object] = Field(
        default=None,
        description="Configuration passed to RegistrationHandler when allow_registration "
        "is True and a signup form is detected during exploration.",
    )

    @model_validator(mode="after")
    def _migrate_deprecated_fields(self) -> "ExplorerConfig":
        """Map deprecated anthropic_* fields onto the new llm_* fields."""
        if self.anthropic_api_key and not self.llm_api_key:
            object.__setattr__(self, "llm_api_key", self.anthropic_api_key)
        if self.anthropic_model and self.llm_model == "claude-sonnet-4-20250514":
            object.__setattr__(self, "llm_model", self.anthropic_model)
        return self

    model_config = {"arbitrary_types_allowed": True}
