"""Pydantic models for registration_handler."""

import logging
import secrets
from pathlib import Path
from typing import Literal, Optional, Callable, Awaitable

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


class IMAPConfig(BaseModel):
    """Configuration for IMAP inbox polling."""

    host: str = Field(..., description="IMAP server hostname.")
    port: int = Field(default=993, description="IMAP port (default 993 for IMAPS).")
    username: str = Field(..., description="IMAP login username (full email address).")
    password: str = Field(..., description="IMAP login password or app-specific password.")
    use_ssl: bool = Field(default=True)
    mailbox: str = Field(default="INBOX", description="Mailbox to poll.")


class DisposableInboxConfig(BaseModel):
    """Configuration for a purpose-built disposable inbox (e.g. AgentMail).

    When set, RegistrationHandler provisions a fresh inbox per attempt
    instead of using a pre-configured email address + IMAP polling.
    """

    provider: Literal["agentmail"] = "agentmail"
    api_key: str = Field(..., description="API key for the disposable inbox provider.")
    base_url: Optional[str] = Field(
        default=None,
        description="API base URL override for testing/self-hosted equivalents.",
    )
    domain: Optional[str] = Field(
        default=None,
        description="Custom domain, if the provider supports it.",
    )


class CaptchaDetected(Exception):
    """Raised when a CAPTCHA is detected on the page.

    The caller is expected to solve the CAPTCHA manually via a visible
    (non-headless) browser window and then call resume() to continue.
    """

    def __init__(
        self,
        page_url: str,
        captcha_type: str,
        screenshot_path: Path,
        message: str = "",
    ):
        self.page_url = page_url
        self.captcha_type = captcha_type
        self.screenshot_path = screenshot_path
        super().__init__(
            message
            or f"CAPTCHA ({captcha_type}) detected at {page_url}. "
            f"Screenshot saved to {screenshot_path}. Solve manually and call resume()."
        )


class RegistrationConfig(BaseModel):
    """Configuration for the RegistrationHandler."""

    signup_url: str = Field(
        ..., description="URL of the signup/registration form."
    )
    email: Optional[str] = Field(
        default=None,
        description="Email address to register with. Required for IMAP/static-email "
        "mode. Omit when using disposable_inbox_config (the address is "
        "provisioned dynamically at runtime).",
    )
    password: str = Field(
        default_factory=lambda: secrets.token_urlsafe(16),
        description="Password to use for registration. A random password is generated "
        "per instance unless explicitly provided.",
    )
    name: Optional[str] = Field(
        default="Test User",
        description="Full name to use on the registration form.",
    )
    imap_config: Optional[IMAPConfig] = Field(
        default=None,
        description="IMAP configuration for email confirmation polling.",
    )
    email_poll_timeout_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
        description="How long to poll the inbox for a confirmation email.",
    )
    email_poll_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=30,
    )
    captcha_screenshot_dir: Path = Field(
        default=Path("storage/captcha_screenshots"),
        description="Directory to save CAPTCHA screenshots.",
    )
    resume_callback: Optional[Callable[[], Awaitable[None]]] = Field(
        default=None,
        description="Async callback to invoke on resume() after manual CAPTCHA solve.",
    )

    # ---- AI judge fields (mirror ExplorerConfig for consistency) ---------
    llm_provider: str = Field(
        default="anthropic",
        description="LLM provider for AI-judge classification calls.",
    )
    llm_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Model identifier for the chosen provider.",
    )
    llm_api_key: str = Field(
        default="",
        description="API key for the LLM provider.",
    )
    llm_base_url: str = Field(
        default="",
        description="Custom base URL. Falls back to provider default if empty.",
    )
    use_ai_judge: bool = Field(
        default=True,
        description="If True, use AI classification calls to validate signup "
        "page candidates and confirm post-submit results. Set to False for "
        "deterministic-only operation with no LLM dependency.",
    )
    candidate_endpoints: list[str] = Field(
        default_factory=list,
        description="URLs discovered during Phase 1 crawl, used for signup-page "
        "candidate discovery. Passed in from cli.py.",
    )
    disposable_inbox_config: Optional[DisposableInboxConfig] = Field(
        default=None,
        description="When set, use a purpose-built disposable inbox instead of "
        "a pre-configured email + IMAP. Mutually exclusive with imap_config.",
    )

    @model_validator(mode="after")
    def _validate_email_backend(self) -> "RegistrationConfig":
        if self.disposable_inbox_config and self.imap_config:
            raise ValueError(
                "disposable_inbox_config and imap_config are mutually exclusive. "
                "Set one or neither, not both."
            )
        if not self.disposable_inbox_config and not self.email:
            raise ValueError(
                "email is required when disposable_inbox_config is not set "
                "(the email address must be known ahead of time for IMAP/static mode)."
            )
        return self

    model_config = {"arbitrary_types_allowed": True}
