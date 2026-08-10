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
    candidate_endpoints: list[str] = Field(
        default_factory=list,
        description="URLs discovered during Phase 1 crawl, used for login-page "
        "candidate discovery. Passed in from cli.py.",
    )

    # ---- AI judge fields (for login success verification) ----------------
    use_ai_judge: bool = Field(
        default=True,
        description="If True, use AI classification to verify login success "
        "when deterministic checks are ambiguous.",
    )
    llm_provider: str = Field(
        default="anthropic",
        description="LLM provider for AI-judge calls.",
    )
    llm_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Model identifier.",
    )
    llm_api_key: str = Field(
        default="",
        description="API key for the LLM provider.",
    )
    llm_base_url: str = Field(
        default="",
        description="Custom base URL.",
    )

    model_config = {"arbitrary_types_allowed": True}
