"""Pydantic models for crawler."""

from datetime import datetime
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


class DiscoveryMethod(str, Enum):
    LINK = "link"
    SITEMAP = "sitemap"
    JS_REGEX = "js-regex"
    ROBOTS_TXT = "robots-txt"


class DiscoveredEndpoint(BaseModel):
    """A single discovered URL or API endpoint."""

    url: str = Field(..., description="The full URL or path discovered.")
    method: DiscoveryMethod = Field(
        ..., description="How this endpoint was discovered."
    )
    source_url: Optional[str] = Field(
        default=None,
        description="The page URL where this endpoint was found.",
    )
    discovered_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp when this endpoint was discovered.",
    )

    @property
    def parsed_url(self):
        return urlparse(self.url)

    def __hash__(self) -> int:
        return hash(self.url.rstrip("/"))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DiscoveredEndpoint):
            return self.url.rstrip("/") == other.url.rstrip("/")
        return NotImplemented


class CrawlConfig(BaseModel):
    """Configuration for the deterministic crawler.

    ``seed_hostname`` is the concrete, resolvable hostname used to construct
    fetch URLs (robots.txt, sitemap.xml). It must be a real host, not a glob.

    ``scope_pattern`` is a glob (e.g. ``*.tiktok.com``) that determines which
    discovered hostnames are in-scope to follow. Defaults to ``seed_hostname``
    (exact match) when not set.
    """

    start_url: str = Field(
        ..., description="The starting URL for the crawl."
    )
    seed_hostname: str = Field(
        ...,
        description="Concrete hostname to start crawling from, e.g. 'developers.tiktok.com'. "
        "Must be a real resolvable host, not a glob pattern.",
    )
    scope_pattern: str = Field(
        default="",
        description="Glob pattern for which discovered hostnames are in-scope to follow, "
        "e.g. '*.tiktok.com'. Defaults to seed_hostname (exact match) if not set.",
    )
    max_depth: int = Field(default=3, ge=0, le=20)
    max_pages: int = Field(default=50, ge=1, le=500)
    respect_robots_txt: bool = Field(default=True)
    fetch_sitemap: bool = Field(default=True)
    extract_js_endpoints: bool = Field(default=True)
    request_delay_ms: int = Field(
        default=200,
        description="Delay between requests in milliseconds (be polite).",
    )
    timeout_ms: int = Field(
        default=30_000,
        description="Navigation timeout in milliseconds.",
    )
    user_agent: Optional[str] = Field(
        default=None,
        description="Custom User-Agent header. If unset, uses browser default.",
    )

    @model_validator(mode="after")
    def _default_scope_pattern(self) -> "CrawlConfig":
        """Default scope_pattern to seed_hostname when left empty."""
        if not self.scope_pattern:
            self.scope_pattern = self.seed_hostname
        return self


class CrawlResult(BaseModel):
    """The result of a crawl operation."""

    config: CrawlConfig
    endpoints: list[DiscoveredEndpoint] = Field(default_factory=list)
    total_pages_crawled: int = 0
    total_links_discovered: int = 0
    total_js_endpoints: int = 0
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    @property
    def unique_urls(self) -> list[str]:
        """Return deduplicated list of discovered URLs."""
        seen: set[str] = set()
        result: list[str] = []
        for ep in self.endpoints:
            key = ep.url.rstrip("/")
            if key not in seen:
                seen.add(key)
                result.append(ep.url)
        return result

    def add_endpoint(self, url: str, method: DiscoveryMethod, source_url: Optional[str] = None):
        """Add a discovered endpoint, deduplicating by URL."""
        candidate = DiscoveredEndpoint(url=url, method=method, source_url=source_url)
        for existing in self.endpoints:
            if existing == candidate:
                # Prefer the more specific discovery method
                method_priority = {DiscoveryMethod.SITEMAP: 0, DiscoveryMethod.ROBOTS_TXT: 1, DiscoveryMethod.LINK: 2, DiscoveryMethod.JS_REGEX: 3}
                if method_priority.get(method, 99) < method_priority.get(existing.method, 99):
                    existing.method = method
                return
        self.endpoints.append(candidate)
        if method == DiscoveryMethod.JS_REGEX:
            self.total_js_endpoints += 1
