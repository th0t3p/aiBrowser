"""Pydantic models for login_handler."""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class LoginConfig(BaseModel):
    """Configuration for the LoginHandler."""

    login_url: str = Field(
        ...,
        description="URL of the login page. If empty string, auto-discover via common patterns.",
    )
    email: str = Field(
        ...,
        description="Email or username to log in with.",
    )
    password: str = Field(
        ...,
        description="Password for login.",
    )
    captcha_screenshot_dir: Path = Field(
        default=Path("storage/captcha_screenshots"),
        description="Directory to save CAPTCHA screenshots.",
    )

    model_config = {"arbitrary_types_allowed": True}
