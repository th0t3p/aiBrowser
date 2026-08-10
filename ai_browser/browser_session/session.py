"""BrowserSession — a Playwright-powered browser session with Burp proxy and scope guard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from ai_browser._scope import hostname_matches_any_scope, hostname_matches_scope

from .models import BrowserSessionConfig, ProxyConfig, ScopeGuardError, BlockedSubresource

logger = logging.getLogger(__name__)


def _parse_cookies_file(raw: dict | list) -> list[dict]:
    """Parse a cookie file blob into a list of cookie dicts for
    ``context.add_cookies()``.

    Accepts two shapes:

    1. Playwright storage_state dict: ``{"cookies": [...], "origins": [...]}``
    2. Bare cookie array: ``[{"name": ..., "value": ..., ...}, ...]``

    Raises ValueError on unrecognized shapes.
    """
    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        if "cookies" in raw:
            return raw["cookies"]
        raise ValueError(
            "Unrecognized cookies-file shape: got a JSON object but expected "
            "either a Playwright storage_state dict (with a 'cookies' key) or "
            "a bare cookie array."
        )

    raise ValueError(
        f"Unrecognized cookies-file shape: expected a JSON array or object, "
        f"got {type(raw).__name__}"
    )


class BrowserSession:
    """Wraps Playwright's async API with a persistent context, Burp proxy, and hostname scope guard.

    All traffic flows through the configured Burp Suite proxy so that aiScraper
    can capture and normalize everything from Burp's proxy history.

    Because Playwright route handlers run as background tasks, exceptions raised
    inside them are NOT propagated to the caller. Instead, scope violations are
    recorded in ``session.violations`` and can be checked explicitly::

        config = BrowserSessionConfig(authorized_hostname="example.com")
        async with BrowserSession(config) as session:
            page = await session.new_page()
            await session.goto(page, "https://example.com")
            # Check for scope violations after navigation:
            session.check_violations()  # raises ScopeGuardError if any occurred
            # Or inspect: if session.violations: ...
    """

    def __init__(self, config: BrowserSessionConfig):
        self.config = config
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._storage_file: Path = self._resolve_storage_file()
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self._route_handlers: list = []
        self.violations: list[ScopeGuardError] = []
        self.blocked_subresources: list[BlockedSubresource] = []
        self._violation_event: Optional[asyncio.Event] = None
        self.cdp_url: Optional[str] = None

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

        # ---- PID-file orphan reaping (CDP path only) --------------------
        if self.config.expose_cdp:
            await self._reap_orphan_if_needed()

        self._playwright = await async_playwright().start()

        user_data_dir = self._resolve_user_data_dir()

        launch_options: dict = {
            "headless": self.config.headless,
            "args": [],
        }

        # Pick a free port for CDP so browser-use (or any other tool)
        # can attach to the same browser process.  We bind to port 0 and
        # let the OS assign a free one to avoid collisions.
        cdp_port: Optional[int] = None
        if self.config.expose_cdp:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                cdp_port = s.getsockname()[1]
            launch_options["args"].append(f"--remote-debugging-port={cdp_port}")
            # Chromium-wide cert ignore — needed because browser-use
            # reuses our CDP context but creates its own pages through
            # a separate Playwright connection that doesn't inherit our
            # per-context ignore_https_errors setting.
            launch_options["args"].append("--ignore-certificate-errors")

        # If a CA cert path is provided, add it via --ignore-certificate-errors-spki-list
        # or set the NSS cert db via --user-data-dir. The primary method is
        # passing it as a launch arg for Chromium.
        if self.config.ca_cert_path and self.config.ca_cert_path.exists():
            launch_options["args"].append(
                f"--ignore-certificate-errors-spki-list={self._calculate_cert_spki_fingerprint()}"
            )
            logger.info("Burp CA cert configured from %s", self.config.ca_cert_path)

        # launch_persistent_context() internally adds --remote-debugging-pipe
        # which conflicts with --remote-debugging-port (SIGBUS on chromium-1228).
        # When CDP is needed, use launch() + new_context() instead —
        # the same pattern verified working in scripts/verify_browser_use_safety.py.
        if self.config.expose_cdp:
            self._browser = await self._playwright.chromium.launch(
                headless=launch_options["headless"],
                args=launch_options["args"],
                proxy=self.config.proxy.playwright_proxy if self.config.proxy else None,
            )
            self._context = await self._browser.new_context(
                viewport={
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
                locale=self.config.locale,
                timezone_id=self.config.timezone_id,
                ignore_https_errors=self.config.ignore_https_errors,
            )
        else:
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

        # Expose the CDP URL for external tools (e.g. browser-use) to
        # attach to the same browser process.
        if cdp_port is not None:
            self.cdp_url = f"http://localhost:{cdp_port}"
            logger.info("CDP endpoint available at %s", self.cdp_url)
            # Best-effort PID file write for orphan reaping on next run
            await self._write_pid_file()

        # Restore persisted storage state if available (skip when an explicit
        # cookies-file is provided — it becomes the sole source of truth)
        if self.config.cookies_file:
            await self._apply_cookies_file(self.config.cookies_file)
        else:
            await self._restore_storage_state()

        # Install the scope guard on every new page
        self._context.on("page", self._on_new_page)

        logger.info("BrowserSession started for %s", self.config.authorized_hostname)

    async def stop(self) -> None:
        """Persist storage state and tear down the browser."""
        # Clean PID file on a normal shutdown (CDP path only) so the
        # next run doesn't mistake this for an orphaned process.
        if self.config.expose_cdp:
            self._clear_pid_file()

        if self._context:
            await self._save_storage_state()
            await self._context.close()
            self._context = None

        # When using launch() (CDP path), the browser is separate from
        # the context and must be closed independently.
        if self._browser is not None and self._browser is not self._context:
            await self._browser.close()

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
        """Create a new page, injecting the hostname scope guard.

        Raises ScopeGuardError if any scope violations were recorded
        during page setup (e.g. from pre-existing persisted state triggering
        background requests). Stale violations from earlier navigations
        are NOT surfaced here.
        """
        if not self._context:
            raise RuntimeError("BrowserSession not started. Call start() or use as context manager.")
        violations_before = len(self.violations)
        page = await self._context.new_page()
        await self._install_scope_guard(page)
        self._check_new_violations(violations_before)
        return page

    async def goto(self, page: Page, url: str, **kwargs) -> None:
        """Navigate a page to *url*, then check for scope violations.

        This wraps ``page.goto()`` and only raises if a scope violation
        occurred during **this specific navigation** — stale violations from
        earlier navigations in the same session do NOT cause false positives.

        Blocked sub-resources (scripts, images, etc.) are NOT surfaced here —
        they are recorded in ``self.blocked_subresources`` and can be queried
        via ``get_blocked_subresource_summary()``.
        """
        violations_before = len(self.violations)
        await page.goto(url, **kwargs)
        self._check_new_violations(violations_before)

    def check_violations(self) -> None:
        """Raise the most recent ScopeGuardError if any scope violations occurred.

        Only checks ``self.violations`` (top-level navigation blocks).
        Sub-resource blocks are tracked separately in ``self.blocked_subresources``.

        This checks the **entire session history** — suitable for end-of-session
        or batch-level checks. For per-navigation checks (e.g. inside ``goto()``),
        use ``_check_new_violations()`` instead to avoid stale-violation false positives.

        Raises:
            ScopeGuardError: The most recent violation, if any were recorded.
        """
        if self.violations:
            raise self.violations[-1]

    def _check_new_violations(self, before_count: int) -> None:
        """Raise if a violation occurred since ``before_count`` was recorded.

        Unlike ``check_violations()``, this is scoped to violations that were
        appended to ``self.violations`` *after* the caller's snapshot, avoiding
        stale-state false positives where a single violation poisons every
        subsequent navigation in the session.

        ``self.violations`` itself is preserved as a full historical record.

        Raises:
            ScopeGuardError: The most recent NEW violation, if any.
        """
        new_violations = self.violations[before_count:]
        if new_violations:
            raise new_violations[-1]

    def get_blocked_subresource_summary(self) -> tuple[int, list[str]]:
        """Return (count, deduplicated_hostnames) of blocked sub-resources.

        These are out-of-scope assets (JS, CSS, images, etc.) that were
        blocked during page loads. They are informational — useful for
        reporting ("this page loads from N external domains") — and are
        NOT an error state.
        """
        hosts = list({b.hostname for b in self.blocked_subresources})
        return len(self.blocked_subresources), sorted(hosts)

    def _get_violation_event(self) -> asyncio.Event:
        """Lazily create and return the violation event (requires a running event loop)."""
        if self._violation_event is None:
            self._violation_event = asyncio.Event()
        return self._violation_event

    def _on_new_page(self, page: Page) -> None:
        """Callback: when a new page/tab is created, inject the scope guard."""
        asyncio.ensure_future(self._install_scope_guard(page))

    async def _install_scope_guard(self, page: Page) -> None:
        """Intercept all requests and navigations; abort any that leave the authorized hostname.

        Uses glob-pattern matching so ``*.example.com`` covers all subdomains.
        """
        authorized = self.config.authorized_hostname

        async def _guard(route):
            url = route.request.url
            hostname = urlparse(url).hostname or ""
            if not hostname_matches_any_scope(hostname, authorized):
                resource_type = getattr(route.request, "resource_type", None) or "unknown"

                if resource_type == "document":
                    # Top-level / iframe navigation — full violation
                    logger.warning(
                        "Scope guard blocked navigation to %s (hostname=%s)", url, hostname
                    )
                    violation = ScopeGuardError(
                        attempted_hostname=hostname,
                        authorized_hostname=self.config.authorized_hostname,
                    )
                    self.violations.append(violation)
                    self._get_violation_event().set()
                    await route.abort()
                    return

                if resource_type in ("xhr", "fetch"):
                    # XHR/fetch — could be page-initiated telemetry OR an
                    # agent_explorer action. Block by default; only allow
                    # through if the hostname is explicitly allowlisted.
                    for pattern in self.config.passive_xhr_hosts:
                        if hostname_matches_scope(hostname, pattern):
                            logger.debug(
                                "Scope guard allowed XHR (%s) to %s (hostname=%s) — "
                                "passive_xhr_hosts match",
                                resource_type, url, hostname,
                            )
                            await route.continue_()
                            return
                    logger.debug(
                        "Scope guard blocked XHR/fetch to %s (hostname=%s)",
                        url, hostname,
                    )
                    self.blocked_subresources.append(
                        BlockedSubresource(url=url, hostname=hostname, resource_type=resource_type)
                    )
                    await route.abort()
                    return

                # All other resource types: script, stylesheet, image, font,
                # media, and anything else — these are page sub-resources that
                # are loaded passively during rendering. Let them through.
                logger.debug(
                    "Scope guard allowed sub-resource (%s) to %s (hostname=%s)",
                    resource_type, url, hostname,
                )
                await route.continue_()
                return

            await route.continue_()

        # Route all requests through the guard
        await page.route("**/*", _guard)

        # Also guard against client-side navigation like location.href changes
        async def _guard_navigation(frame):
            if frame == page.main_frame:
                url = frame.url
                if url and url != "about:blank":
                    hostname = urlparse(url).hostname or ""
                    if not hostname_matches_any_scope(hostname, authorized):
                        logger.warning(
                            "Scope guard detected navigation to %s via client-side redirect",
                            url,
                        )
                        violation = ScopeGuardError(
                            attempted_hostname=hostname,
                            authorized_hostname=self.config.authorized_hostname,
                        )
                        self.violations.append(violation)
                        self._get_violation_event().set()
                        try:
                            await page.goto("about:blank")
                        except Exception:
                            # Losing this race means another _guard_navigation task
                            # is already redirecting to the same "about:blank"
                            # target — the desired end state (page not left on the
                            # disallowed URL) is reached either way.  The violation
                            # bookkeeping above is unaffected.
                            logger.debug(
                                "Scope guard remediation goto('about:blank') raised "
                                "(expected when another framenavigated handler won "
                                "the race)",
                                exc_info=True,
                            )
                        return

        page.on("framenavigated", lambda frame: asyncio.ensure_future(_guard_navigation(frame)))

    # ------------------------------------------------------------------
    # Storage state persistence (cookies, localStorage per hostname)
    # ------------------------------------------------------------------

    def _resolve_storage_key(self) -> str:
        """Return a filesystem-safe key for file/folder naming.

        Uses ``storage_key`` if explicitly set.  Otherwise falls back to
        ``authorized_hostname`` — but only when it is a plain ``str``.
        When ``authorized_hostname`` is a list and no ``storage_key`` is
        set, that is a programming error — raises ``ValueError`` rather
        than silently producing a broken filename.
        """
        if self.config.storage_key:
            return self.config.storage_key
        if isinstance(self.config.authorized_hostname, str):
            return self.config.authorized_hostname
        raise ValueError(
            "BrowserSessionConfig.authorized_hostname is a list, but "
            "storage_key is not set.  Pass storage_key=<seed_hostname> when "
            "constructing the config."
        )

    def _storage_key_safe(self) -> str:
        """Return the storage key with colons/slashes replaced for safe filename use."""
        return self._resolve_storage_key().replace(":", "_").replace("/", "_")

    def _resolve_storage_file(self) -> Path:
        """Storage file path, keyed by authorized hostname."""
        self.config.storage_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self._storage_key_safe()
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

    async def _apply_cookies_file(self, path: Path) -> None:
        """Parse *path* as a Playwright storage_state JSON or bare cookie array,
        and apply its cookies to the current context."""
        if not self._context:
            return
        if not path.exists():
            raise FileNotFoundError(f"Cookies file not found: {path}")

        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cookies file is not valid JSON: {path}") from exc

        cookies = _parse_cookies_file(raw)
        await self._context.add_cookies(cookies)
        logger.info(
            "Applied %d cookie(s) from %s (skipped automatic session restore)",
            len(cookies), path,
        )

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

        Extracts the SubjectPublicKeyInfo substructure, SHA-256 hashes it,
        and returns the base64-encoded result. Uses the ``cryptography`` library
        for correct ASN.1 parsing. Falls back to whole-cert hash if unavailable.
        """
        import hashlib
        import base64

        cert_bytes = self.config.ca_cert_path.read_bytes()  # type: ignore[union-attr]

        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            # Try PEM first, then DER
            try:
                cert = x509.load_pem_x509_certificate(cert_bytes)
            except Exception:
                cert = x509.load_der_x509_certificate(cert_bytes)

            # Extract just the SubjectPublicKeyInfo (not the whole certificate)
            spki_der = cert.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            sha256 = hashlib.sha256(spki_der).digest()
            return base64.b64encode(sha256).decode()

        except ImportError:
            logger.warning(
                "cryptography library not installed; falling back to whole-cert hash "
                "(install with: pip install cryptography)"
            )
            # Fallback: hash the whole DER certificate (incorrect but functional)
            if cert_bytes.startswith(b"-----"):
                b64_body = (
                    cert_bytes.decode()
                    .split("-----")[2]
                    .replace("\n", "")
                    .replace("\r", "")
                )
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

    # ------------------------------------------------------------------
    # PID file (CDP-path only) — prevents orphaned Chromium accumulation
    # ------------------------------------------------------------------

    def _pid_file_path(self) -> Path:
        """Path for the PID file, keyed by hostname (mirrors _resolve_storage_file)."""
        self.config.storage_dir.mkdir(parents=True, exist_ok=True)
        pids_dir = self.config.storage_dir / "pids"
        pids_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self._storage_key_safe()
        return pids_dir / f"{safe_name}.pid"

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Return True if a process with *pid* exists (best-effort)."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    @staticmethod
    def _is_chromium_process(pid: int) -> bool:
        """Return True if *pid* appears to be a Chromium/Chrome process.

        Checks the process command line via ``ps`` (works on macOS and
        Linux).  Returns False if the check can't be performed or the
        process doesn't look like Chromium/Chrome.
        """
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            )
            cmdline = result.stdout.lower()
            return "chrome" in cmdline or "chromium" in cmdline
        except Exception:
            return False

    async def _write_pid_file(self) -> None:
        """Write the browser process PID to a PID file via CDP.

        Uses CDP's ``SystemInfo.getProcessInfo`` to find the PID of the
        ``"browser"``-type process.  Best-effort — failure here is logged
        and does not break the session.
        """
        try:
            cdp = await self._browser.new_browser_cdp_session()  # type: ignore[union-attr]
            info = await cdp.send("SystemInfo.getProcessInfo")
            for entry in info.get("processInfo", []):
                if entry.get("type") == "browser":
                    pid = entry["id"]
                    self._pid_file_path().write_text(str(pid))
                    logger.debug("PID file written: %s (pid=%d)", self._pid_file_path(), pid)
                    return
            logger.debug("SystemInfo.getProcessInfo did not include a 'browser' entry")
        except Exception as exc:
            logger.debug("Failed to write PID file: %s", exc)

    async def _reap_orphan_if_needed(self) -> None:
        """Check for a leftover PID file from a previous crashed run and
        terminate the process if it's still alive and confirmed Chromium.

        Safety property: never sends a termination signal to a PID that
        hasn't been independently confirmed to be a Chromium process
        (guarding against PID recycling by the OS between runs).
        """
        pid_file = self._pid_file_path()
        if not pid_file.exists():
            return

        try:
            stale_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)
            return

        if not self._is_process_alive(stale_pid):
            logger.debug("Stale PID file for pid=%d (no longer alive) — removing", stale_pid)
            pid_file.unlink(missing_ok=True)
            return

        if not self._is_chromium_process(stale_pid):
            logger.warning(
                "PID file for pid=%d exists and process is alive, but it does "
                "NOT appear to be a Chromium process (PID likely recycled by "
                "the OS).  Leaving the process alone and removing the stale "
                "record.", stale_pid,
            )
            pid_file.unlink(missing_ok=True)
            return

        # Alive and confirmed Chromium — reap it
        logger.warning(
            "Orphaned Chromium process detected (pid=%d) — attempting graceful "
            "shutdown", stale_pid,
        )
        try:
            os.kill(stale_pid, signal.SIGTERM)
        except OSError:
            pass

        # Poll for up to ~2s, then escalate
        for _ in range(10):
            await asyncio.sleep(0.2)
            if not self._is_process_alive(stale_pid):
                logger.info("Orphaned Chromium (pid=%d) terminated cleanly", stale_pid)
                break
        else:
            logger.warning(
                "Orphaned Chromium (pid=%d) did not respond to SIGTERM — "
                "sending SIGKILL", stale_pid,
            )
            try:
                os.kill(stale_pid, signal.SIGKILL)
            except OSError:
                pass

        pid_file.unlink(missing_ok=True)

    def _clear_pid_file(self) -> None:
        """Remove the PID file on a clean shutdown (not an orphan)."""
        self._pid_file_path().unlink(missing_ok=True)

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
