"""BrowserSession — a Playwright-powered browser session with Burp proxy and scope guard."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .models import BrowserSessionConfig, ProxyConfig, ScopeGuardError

logger = logging.getLogger(__name__)


class BrowserSession:
    """Wraps Playwright's async API with a persistent context, Burp proxy, and hostname scope guard.

    All traffic flows through the configured Burp Suite proxy so that aiScraper
    can capture and normalize everything from Burp's proxy history.

    Usage::

        config = BrowserSessionConfig(authorized_hostname="example.com")
        async with BrowserSession(config) as session:
            page = await session.new_page()
            await page.goto("https://example.com")
            # ... any attempt to reach other hostnames raises ScopeGuardError
    """

    def __init__(self, config: BrowserSessionConfig):
        self.config = config
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._storage_file: Path = self._resolve_storage_file()
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self._route_handlers: list = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    async def start(self) -> None:
        """Launch the browser, create a persistent context with proxy and scope guard."""
        logger.info("Starting BrowserSession for %s", self.config.authorized_hostname)

        self._playwright = await async_playwright().start()

        user_data_dir = self._resolve_user_data_dir()

        launch_options: dict = {
            "headless": self.config.headless,
            "args": [],
        }

        # If a CA cert path is provided, add it via --ignore-certificate-errors-spki-list
        # or set the NSS cert db via --user-data-dir. The primary method is
        # passing it as a launch arg for Chromium.
        if self.config.ca_cert_path and self.config.ca_cert_path.exists():
            launch_options["args"].append(
                f"--ignore-certificate-errors-spki-list={self._calculate_cert_spki_fingerprint()}"
            )
            logger.info("Burp CA cert configured from %s", self.config.ca_cert_path)

        self._browser = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=launch_options["headless"],
            args=launch_options["args"],
            proxy=self.config.proxy.playwright_proxy if self.config.proxy else None,
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            locale=self.config.locale,
            timezone_id=self.config.timezone_id,
            ignore_https_errors=self.config.ignore_https_errors,
        )
        self._context = self._browser

        # Restore persisted storage state if available
        await self._restore_storage_state()

        # Install the scope guard on every new page
        self._context.on("page", self._on_new_page)

        logger.info("BrowserSession started for %s", self.config.authorized_hostname)

    async def stop(self) -> None:
        """Persist storage state and tear down the browser."""
        if self._context:
            await self._save_storage_state()
            await self._context.close()
            self._context = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        self._browser = None

        if self._temp_dir:
            self._temp_dir.cleanup()
            self._temp_dir = None

        logger.info("BrowserSession stopped for %s", self.config.authorized_hostname)

    # ------------------------------------------------------------------
    # Page factory with scope guard injection
    # ------------------------------------------------------------------

    async def new_page(self) -> Page:
        """Create a new page, injecting the hostname scope guard."""
        if not self._context:
            raise RuntimeError("BrowserSession not started. Call start() or use as context manager.")
        page = await self._context.new_page()
        await self._install_scope_guard(page)
        return page

    def _on_new_page(self, page: Page) -> None:
        """Callback: when a new page/tab is created, inject the scope guard."""
        asyncio.ensure_future(self._install_scope_guard(page))

    async def _install_scope_guard(self, page: Page) -> None:
        """Intercept all requests and navigations; abort any that leave the authorized hostname."""
        authorized = self.config.authorized_hostname.lower().strip()

        async def _guard(route):
            url = route.request.url
            hostname = urlparse(url).hostname or ""
            if hostname.lower() != authorized:
                logger.warning("Scope guard blocked navigation to %s (hostname=%s)", url, hostname)
                await route.abort()
                raise ScopeGuardError(
                    attempted_hostname=hostname,
                    authorized_hostname=self.config.authorized_hostname,
                )
            await route.continue_()

        # Route all requests through the guard
        await page.route("**/*", _guard)

        # Also guard against client-side navigation like location.href changes
        async def _guard_navigation(frame):
            if frame == page.main_frame:
                url = frame.url
                if url and url != "about:blank":
                    hostname = urlparse(url).hostname or ""
                    if hostname.lower() != authorized:
                        logger.warning(
                            "Scope guard detected navigation to %s via client-side redirect",
                            url,
                        )
                        await page.goto("about:blank")
                        raise ScopeGuardError(
                            attempted_hostname=hostname,
                            authorized_hostname=self.config.authorized_hostname,
                        )

        page.on("framenavigated", lambda frame: asyncio.ensure_future(_guard_navigation(frame)))

    # ------------------------------------------------------------------
    # Storage state persistence (cookies, localStorage per hostname)
    # ------------------------------------------------------------------

    def _resolve_storage_file(self) -> Path:
        """Storage file path, keyed by authorized hostname."""
        self.config.storage_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self.config.authorized_hostname.replace(":", "_").replace("/", "_")
        return self.config.storage_dir / f"{safe_name}.json"

    async def _save_storage_state(self) -> None:
        """Persist cookies and localStorage to disk."""
        if not self._context:
            return
        try:
            state = await self._context.storage_state()
            self._storage_file.parent.mkdir(parents=True, exist_ok=True)
            self._storage_file.write_text(json.dumps(state, indent=2))
            logger.info("Storage state saved to %s", self._storage_file)
        except Exception as exc:
            logger.error("Failed to save storage state: %s", exc)

    async def _restore_storage_state(self) -> None:
        """Restore previously saved cookies and localStorage, if any."""
        if not self._context or not self._storage_file.exists():
            return
        try:
            state = json.loads(self._storage_file.read_text())
            await self._context.add_cookies(state.get("cookies", []))
            # localStorage is restored via the origins section when we navigate
            logger.info("Storage state restored from %s", self._storage_file)
        except Exception as exc:
            logger.error("Failed to restore storage state: %s", exc)

    # ------------------------------------------------------------------
    # Burp CA certificate trust
    # ------------------------------------------------------------------

    def install_ca_cert(self, cert_path: Path) -> None:
        """Set the path to the Burp CA certificate for future browser launches.

        The Burp CA cert must be exported from Burp Suite (Proxy > Options >
        Import/Export CA certificate, export as DER or PEM). This path is
        passed to Chromium's --ignore-certificate-errors-spki-list flag.

        Note: changes take effect on the next call to start().
        """
        if not cert_path.exists():
            raise FileNotFoundError(f"CA certificate not found: {cert_path}")
        self.config.ca_cert_path = cert_path
        logger.info("Burp CA cert configured: %s", cert_path)

    def _calculate_cert_spki_fingerprint(self) -> str:
        """Calculate the SPKI fingerprint of the Burp CA certificate for Chromium.

        This is a simplified approach; for production use, import the cert
        into the OS trust store instead.
        """
        import hashlib
        import base64

        cert_bytes = self.config.ca_cert_path.read_bytes()  # type: ignore[union-attr]
        # For a PEM cert, try to extract the DER body
        if cert_bytes.startswith(b"-----"):
            b64_body = cert_bytes.decode().split("-----")[2].replace("\n", "").replace("\r", "")
            cert_bytes = base64.b64decode(b64_body)

        sha256 = hashlib.sha256(cert_bytes).digest()
        return base64.b64encode(sha256).decode()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_user_data_dir(self) -> Path:
        """Resolve the user data directory for persistent browser profile."""
        if self.config.user_data_dir:
            path = self.config.user_data_dir
            path.mkdir(parents=True, exist_ok=True)
            return path
        self._temp_dir = tempfile.TemporaryDirectory(prefix="ai_browser_")
        return Path(self._temp_dir.name)

    @property
    def context(self) -> Optional[BrowserContext]:
        """The underlying Playwright BrowserContext (if started)."""
        return self._context

    @property
    def pages(self) -> list[Page]:
        """All currently open pages in the browser context."""
        if self._context:
            return self._context.pages
        return []
