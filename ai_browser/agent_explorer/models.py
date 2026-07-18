"""Pydantic models for agent_explorer."""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


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
    anthropic_api_key: str = Field(
        ...,
        description="Claude API key for decision-making.",
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Claude model identifier.",
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

    model_config = {"arbitrary_types_allowed": True}
