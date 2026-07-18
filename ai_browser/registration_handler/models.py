"""Pydantic models for registration_handler."""

import logging
import secrets
from pathlib import Path
from typing import Optional, Callable, Awaitable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class IMAPConfig(BaseModel):
    """Configuration for IMAP inbox polling."""

    host: str = Field(..., description="IMAP server hostname.")
    port: int = Field(default=993, description="IMAP port (default 993 for IMAPS).")
    username: str = Field(..., description="IMAP login username (full email address).")
    password: str = Field(..., description="IMAP login password or app-specific password.")
    use_ssl: bool = Field(default=True)
    mailbox: str = Field(default="INBOX", description="Mailbox to poll.")


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
    email: str = Field(
        ...,
        description="Email address to register with. Use +tag aliasing "
        "(e.g. test+targetname@mydomain.com).",
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

    model_config = {"arbitrary_types_allowed": True}
