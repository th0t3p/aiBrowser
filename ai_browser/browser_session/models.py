"""Pydantic models for browser_session."""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ProxyConfig(BaseModel):
    """Configuration for the Burp Suite proxy."""

    server: str = Field(
        default="http://127.0.0.1:8080",
        description="Burp Suite proxy address. All browser traffic is routed through this proxy.",
    )
    username: Optional[str] = Field(
        default=None,
        description="Optional proxy authentication username.",
    )
    password: Optional[str] = Field(
        default=None,
        description="Optional proxy authentication password.",
    )
    bypass: Optional[str] = Field(
        default=None,
        description="Comma-separated list of addresses to bypass the proxy (e.g. '<-loopback>').",
    )

    @property
    def playwright_proxy(self) -> dict[str, str]:
        """Return proxy settings in Playwright's expected format."""
        cfg: dict[str, str] = {"server": self.server}
        if self.username:
            cfg["username"] = self.username
        if self.password:
            cfg["password"] = self.password
        if self.bypass:
            cfg["bypass"] = self.bypass
        return cfg


class BrowserSessionConfig(BaseModel):
    """Configuration for a BrowserSession."""

    authorized_hostname: str = Field(
        ...,
        description="The only hostname this session is permitted to navigate to. "
        "Any attempt to navigate to a different hostname raises ScopeGuardError.",
    )
    proxy: ProxyConfig = Field(
        default_factory=ProxyConfig,
        description="Proxy configuration (defaults to Burp Suite on localhost:8080).",
    )
    headless: bool = Field(
        default=True,
        description="Run the browser in headless mode.",
    )
    storage_dir: Path = Field(
        default=Path("storage/browser_states"),
        description="Directory to persist browser storage_state files, keyed by hostname.",
    )
    ca_cert_path: Optional[Path] = Field(
        default=None,
        description="Path to an exported Burp CA certificate (DER or PEM) to trust in the browser profile.",
    )
    user_data_dir: Optional[Path] = Field(
        default=None,
        description="Directory for persistent browser profile data. Defaults to a temp directory.",
    )
    viewport_width: int = Field(default=1280, ge=320)
    viewport_height: int = Field(default=720, ge=240)
    locale: str = Field(default="en-US")
    timezone_id: str = Field(default="America/New_York")
    ignore_https_errors: bool = Field(
        default=True,
        description="Ignore HTTPS certificate errors (needed when routing through Burp).",
    )

    model_config = {"arbitrary_types_allowed": True}


class ScopeGuardError(Exception):
    """Raised when the browser attempts to navigate outside the authorized hostname."""

    def __init__(self, attempted_hostname: str, authorized_hostname: str):
        self.attempted_hostname = attempted_hostname
        self.authorized_hostname = authorized_hostname
        super().__init__(
            f"Navigation blocked: attempted to reach '{attempted_hostname}' "
            f"but only '{authorized_hostname}' is authorized."
        )


class BlockedSubresource:
    """Record of a blocked out-of-scope sub-resource (JS, CSS, image, font, etc.).

    These are informational — they indicate that a page loaded assets from
    external domains, which is common and expected. Unlike ScopeGuardError,
    they do NOT halt crawling of the page that triggered them.
    """

    def __init__(self, url: str, hostname: str, resource_type: str):
        self.url = url
        self.hostname = hostname
        self.resource_type = resource_type

    def __repr__(self) -> str:
        return (
            f"BlockedSubresource(url={self.url!r}, hostname={self.hostname!r}, "
            f"resource_type={self.resource_type!r})"
        )
