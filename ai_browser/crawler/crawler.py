"""Deterministic crawler — BFS link crawl, robots.txt, sitemap, JS endpoint extraction."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from ai_browser.browser_session import BrowserSession, BrowserSessionConfig

from .models import CrawlConfig, CrawlResult, DiscoveredEndpoint, DiscoveryMethod

logger = logging.getLogger(__name__)

# Patterns for extracting API-like endpoints from JavaScript
JS_API_PATTERNS: list[re.Pattern] = [
    # fetch('/api/...'), fetch("...")
    re.compile(r"""fetch\s*\(\s*["']([^"']+)["']""", re.IGNORECASE),
    # axios.get('/api/...'), axios.post("...")
    re.compile(r"""axios\.(?:get|post|put|delete|patch|options|head|request)\s*\(\s*["']([^"']+)["']""", re.IGNORECASE),
    # XMLHttpRequest .open('GET', '/api/...')
    re.compile(r"""\.open\s*\(\s*["'][A-Z]+["']\s*,\s*["']([^"']+)["']""", re.IGNORECASE),
    # $.ajax({ url: '/api/...' })
    re.compile(r"""url\s*:\s*["']([^"']+)["']""", re.IGNORECASE),
    # $.get('/api/...'), $.post("...")
    re.compile(r"""\$\.(?:get|post|getJSON|ajax)\s*\(\s*["']([^"']+)["']""", re.IGNORECASE),
    # Path-like strings: "/api/v1/...", "/graphql", "/v2/..."
    re.compile(r"""["']((?:/[a-zA-Z0-9._~!$&'()*+,;=:@%-]+){2,})["']"""),
    # Template literal paths: `/api/users/${id}`
    re.compile(r"""`((?:/[a-zA-Z0-9._~!$&'()*+,;=:@%-]|[$]\{[^}]+\})+){2,}`"""),
]


class Crawler:
    """Deterministic, no-LLM web crawler.

    Performs a BFS crawl over same-hostname <a href> links, fetches and parses
    robots.txt and sitemap.xml, and extracts likely API endpoints from inline
    and linked JavaScript.

    Relies on a BrowserSession for scope-guarded, proxy-routed navigation.
    """

    def __init__(self, config: CrawlConfig):
        self.config = config
        self._visited: set[str] = set()
        self._result = CrawlResult(config=config)
        self._session: Optional[BrowserSession] = None

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, session: BrowserSession) -> CrawlResult:
        """Execute the crawl using an already-started BrowserSession."""
        self._session = session
        self._result.started_at = datetime.utcnow()

        try:
            # Phase 1: robots.txt
            if self.config.respect_robots_txt:
                await self._fetch_robots_txt()

            # Phase 2: sitemap.xml
            if self.config.fetch_sitemap:
                await self._fetch_sitemap()

            # Phase 3: BFS crawl
            await self._bfs_crawl()

        except Exception as exc:
            self._result.errors.append(str(exc))
            logger.error("Crawl error: %s", exc)

        self._result.finished_at = datetime.utcnow()
        logger.info(
            "Crawl complete: %d pages, %d endpoints, %d JS endpoints, %d errors",
            self._result.total_pages_crawled,
            len(self._result.endpoints),
            self._result.total_js_endpoints,
            len(self._result.errors),
        )
        return self._result

    # ------------------------------------------------------------------
    # Phase 1: robots.txt
    # ------------------------------------------------------------------

    async def _fetch_robots_txt(self) -> None:
        """Fetch and parse robots.txt for the target hostname."""
        robots_url = f"https://{self.config.authorized_hostname}/robots.txt"
        try:
            page = await self._session.new_page()  # type: ignore[union-attr]
            response = await page.goto(robots_url, timeout=self.config.timeout_ms)
            if response and response.ok:
                content = await response.text()
                rp = RobotFileParser()
                rp.set_url(robots_url)
                rp.parse(content.splitlines())

                # Extract allowed paths from robots.txt as potential endpoints
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("Allow:") or line.startswith("Disallow:"):
                        path = line.split(":", 1)[1].strip()
                        if path and path != "/" and not path.startswith("*"):
                            full_url = urljoin(f"https://{self.config.authorized_hostname}", path)
                            self._result.add_endpoint(full_url, DiscoveryMethod.ROBOTS_TXT)

                logger.info("robots.txt parsed: %d entries", len(self._result.endpoints))
            await page.close()
        except Exception as exc:
            logger.warning("Failed to fetch robots.txt: %s", exc)

    # ------------------------------------------------------------------
    # Phase 2: sitemap.xml
    # ------------------------------------------------------------------

    async def _fetch_sitemap(self) -> None:
        """Fetch and parse sitemap.xml (and sitemap index files) for the target."""
        sitemap_url = f"https://{self.config.authorized_hostname}/sitemap.xml"
        await self._parse_sitemap(sitemap_url)

        # Also try common alternative sitemap paths
        alt_paths = [
            f"https://{self.config.authorized_hostname}/sitemap_index.xml",
            f"https://{self.config.authorized_hostname}/sitemap-index.xml",
            f"https://{self.config.authorized_hostname}/sitemap.php",
        ]
        for alt_url in alt_paths:
            try:
                page = await self._session.new_page()  # type: ignore[union-attr]
                response = await page.goto(alt_url, timeout=self.config.timeout_ms)
                if response and response.ok:
                    await self._parse_sitemap(alt_url)
                await page.close()
            except Exception:
                pass

    async def _parse_sitemap(self, url: str) -> None:
        """Parse a single sitemap XML, handling nested sitemap indexes."""
        try:
            page = await self._session.new_page()  # type: ignore[union-attr]
            response = await page.goto(url, timeout=self.config.timeout_ms)
            if not response or not response.ok:
                await page.close()
                return

            content = await response.text()
            soup = BeautifulSoup(content, "xml")

            # Check for sitemap index
            sitemap_tags = soup.find_all("sitemap")
            for sm in sitemap_tags:
                loc = sm.find("loc")
                if loc and loc.text:
                    parsed = urlparse(loc.text)
                    if parsed.hostname == self.config.authorized_hostname:
                        await self._parse_sitemap(loc.text)

            # Extract URLs
            url_tags = soup.find_all("url")
            for url_tag in url_tags:
                loc = url_tag.find("loc")
                if loc and loc.text:
                    self._result.add_endpoint(loc.text, DiscoveryMethod.SITEMAP)

            await page.close()
            logger.info("Sitemap %s: %d URLs found", url, len(url_tags))
        except Exception as exc:
            logger.warning("Failed to parse sitemap %s: %s", url, exc)

    # ------------------------------------------------------------------
    # Phase 3: BFS crawl
    # ------------------------------------------------------------------

    async def _bfs_crawl(self) -> None:
        """BFS crawl over <a href> links, respecting max_depth and max_pages."""
        start_url = self.config.start_url
        if not start_url.startswith("http"):
            start_url = f"https://{start_url}"

        queue: deque[tuple[str, int]] = deque()
        queue.append((start_url, 0))
        self._visited.add(self._normalize(start_url))

        while queue and self._result.total_pages_crawled < self.config.max_pages:
            url, depth = queue.popleft()

            if depth > self.config.max_depth:
                continue

            try:
                await self._crawl_page(url, depth, queue)
            except Exception as exc:
                self._result.errors.append(f"{url}: {exc}")
                logger.warning("Error crawling %s: %s", url, exc)

            # Be polite — delay between requests
            await asyncio.sleep(self.config.request_delay_ms / 1000.0)

    async def _crawl_page(self, url: str, depth: int, queue: deque) -> None:
        """Crawl a single page: extract links and JS endpoints."""
        if self._result.total_pages_crawled >= self.config.max_pages:
            return

        page = await self._session.new_page()  # type: ignore[union-attr]
        try:
            response = await page.goto(url, timeout=self.config.timeout_ms)
            if not response or not response.ok:
                await page.close()
                return

            self._result.total_pages_crawled += 1
            self._result.add_endpoint(url, DiscoveryMethod.LINK)

            content = await page.content()
            soup = BeautifulSoup(content, "lxml")

            # Extract <a href> links
            links = self._extract_links(soup, url, depth)
            for link_url in links:
                normalized = self._normalize(link_url)
                if normalized not in self._visited:
                    self._visited.add(normalized)
                    queue.append((link_url, depth + 1))
                    self._result.total_links_discovered += 1
                    self._result.add_endpoint(link_url, DiscoveryMethod.LINK, source_url=url)

            # Extract JS endpoints from inline scripts
            if self.config.extract_js_endpoints:
                await self._extract_js_from_page(page, url)

        finally:
            await page.close()

    # ------------------------------------------------------------------
    # Link extraction
    # ------------------------------------------------------------------

    def _extract_links(self, soup: BeautifulSoup, current_url: str, depth: int) -> list[str]:
        """Extract same-hostname <a href> links from a page."""
        links: list[str] = []
        base_url = f"https://{self.config.authorized_hostname}"

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            # Skip javascript:, mailto:, tel:, fragment-only
            if href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            absolute = urljoin(current_url, href)
            parsed = urlparse(absolute)

            if parsed.hostname != self.config.authorized_hostname:
                continue
            # Skip non-http schemes
            if parsed.scheme not in ("http", "https"):
                continue
            # Strip fragment
            clean = absolute.split("#")[0]
            links.append(clean)

        return links

    # ------------------------------------------------------------------
    # JS endpoint extraction
    # ------------------------------------------------------------------

    async def _extract_js_from_page(self, page, source_url: str) -> None:
        """Extract API endpoints from inline <script> blocks and linked JS files."""
        # Inline scripts
        inline_scripts = await page.evaluate("""
            Array.from(document.querySelectorAll('script:not([src])'))
                .map(s => s.textContent)
                .filter(t => t)
        """)
        for script_text in inline_scripts:
            self._scan_js_for_endpoints(script_text, source_url)

        # Linked JS files
        linked_scripts = await page.evaluate("""
            Array.from(document.querySelectorAll('script[src]'))
                .map(s => s.src)
        """)
        for js_url in linked_scripts:
            parsed = urlparse(js_url)
            if parsed.hostname and parsed.hostname != self.config.authorized_hostname:
                continue
            try:
                js_page = await self._session.new_page()  # type: ignore[union-attr]
                response = await js_page.goto(js_url, timeout=self.config.timeout_ms)
                if response and response.ok:
                    js_text = await response.text()
                    self._scan_js_for_endpoints(js_text, js_url)
                await js_page.close()
            except Exception as exc:
                logger.debug("Failed to fetch JS file %s: %s", js_url, exc)

    def _scan_js_for_endpoints(self, js_text: str, source_url: str) -> None:
        """Apply regex patterns to JavaScript text to find API endpoints."""
        for pattern in JS_API_PATTERNS:
            for match in pattern.finditer(js_text):
                path = match.group(1)
                # Filter out obviously non-API strings
                if not path or len(path) < 3:
                    continue
                if path.startswith("http"):
                    parsed = urlparse(path)
                    if parsed.hostname and parsed.hostname != self.config.authorized_hostname:
                        continue
                    full_url = path
                else:
                    # Relative path — resolve against the authorized hostname
                    if path.startswith("/"):
                        full_url = urljoin(f"https://{self.config.authorized_hostname}", path)
                    else:
                        # Might not be a URL-like path at all; skip unless it looks like one
                        if re.search(r"[a-zA-Z]+/", path) or "." in path:
                            full_url = urljoin(f"https://{self.config.authorized_hostname}", f"/{path}")
                        else:
                            continue

                self._result.add_endpoint(full_url, DiscoveryMethod.JS_REGEX, source_url=source_url)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(url: str) -> str:
        """Normalize a URL for deduplication: lowercase hostname, strip trailing slash, strip fragment."""
        parsed = urlparse(url)
        normalized = f"{parsed.scheme}://{parsed.hostname}{parsed.path}".rstrip("/")
        if parsed.query:
            normalized += f"?{parsed.query}"
        return normalized.lower()

    @property
    def result(self) -> CrawlResult:
        return self._result
