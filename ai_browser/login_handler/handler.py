"""LoginHandler — automated login form filling using shared form helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ai_browser.browser_session import BrowserSession
from ai_browser._form_helpers import fill_form_fields, submit_form, check_captcha

from .models import LoginConfig

logger = logging.getLogger(__name__)


class LoginHandler:
    """Handles automated login form filling and submission.

    Reuses the shared form-filling and CAPTCHA helpers from _form_helpers
    to avoid duplicating the same field-name matching logic.

    Usage::

        login_config = LoginConfig(
            login_url="https://target.com/login",
            email="test+target@mydomain.com",
            password="...",
        )
        handler = LoginHandler(login_config)
        authenticated_page = await handler.login(session)
    """

    def __init__(self, config: LoginConfig):
        self.config = config
        self.authenticated: Optional[bool] = None

    # ------------------------------------------------------------------
    # Main login flow
    # ------------------------------------------------------------------

    async def login(self, session: BrowserSession) -> Page:
        """Execute the full login flow.

        Returns the page after login attempt.  Check ``self.authenticated``
        afterward to determine whether login actually succeeded.
        """
        login_url = self._resolve_login_url()
        logger.info("Starting login as %s on %s", self.config.email, login_url)

        page = await session.new_page()

        # Step 1: Navigate to login URL
        await page.goto(login_url, timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)

        # Step 2: Check for CAPTCHA
        await self._check_captcha(page, "login_form")

        # Save cookies before login for before/after comparison
        cookies_before = await self._get_cookie_names(page)

        # Step 3: Fill the login form
        await self._fill_login_form(page)

        # Step 4: Check for CAPTCHA again
        await self._check_captcha(page, "login_submit")

        # Step 5: Submit
        login_selectors = [
            "button:has-text('Log In')",
            "button:has-text('Login')",
            "button:has-text('Sign In')",
            "button:has-text('Sign in')",
            "button:has-text('log in')",
            "button:has-text('sign in')",
        ]
        await submit_form(page, extra_selectors=login_selectors)

        # Step 6: Wait and check again
        await asyncio.sleep(2)
        await self._check_captcha(page, "post_login")

        # ---- Success verification -----------------------------------------
        self.authenticated = await self._check_authenticated(
            page, login_url, cookies_before,
        )

        if self.authenticated:
            logger.info("Login successful! Current URL: %s", page.url)
        elif self.authenticated is False:
            logger.warning(
                "Login appears to have failed (still on or near the login page, "
                "no new auth cookies). Current URL: %s", page.url,
            )
        else:
            logger.info(
                "Could not determine login outcome. Current URL: %s", page.url,
            )

        return page

    # ------------------------------------------------------------------
    # Form filling
    # ------------------------------------------------------------------

    async def _fill_login_form(self, page: Page) -> None:
        """Heuristically fill common login form fields using the shared helper."""
        logger.info("Attempting to fill login form fields")

        field_mappings: list[tuple[list[str], str]] = [
            (
                [
                    "email", "email_address", "login_email", "username",
                    "user[email]", "login", "user_login", "user[login]",
                ],
                self.config.email,
            ),
            (
                [
                    "password", "passwd", "pwd", "user[password]",
                    "login_password", "user_pass",
                ],
                self.config.password,
            ),
        ]

        await fill_form_fields(page, field_mappings)

    # ------------------------------------------------------------------
    # Login URL resolution (reuses registration's candidate-discovery pattern)
    # ------------------------------------------------------------------

    def _resolve_login_url(self) -> str:
        """Return the best login URL. If candidates are provided, try to
        discover one; otherwise fall back to the configured URL."""
        candidates = self.config.candidate_endpoints
        if candidates:
            discovered = _discover_login_url(candidates)
            if discovered:
                logger.info("Discovered login URL from crawl endpoints: %s", discovered)
                return discovered

        return self.config.login_url

    # ------------------------------------------------------------------
    # Success verification
    # ------------------------------------------------------------------

    async def _check_authenticated(
        self, page: Page, login_url: str, cookies_before: set[str],
    ) -> Optional[bool]:
        """Determine whether login succeeded.

        Returns True (clear success), False (clear failure), or None
        (ambiguous — AI judge fallback failed or wasn't available).
        """
        # Deterministic check: did we navigate away from the login page?
        current_url = page.url
        login_parsed = urlparse(login_url)
        current_parsed = urlparse(current_url)

        url_changed = (
            current_parsed.path.rstrip("/") != login_parsed.path.rstrip("/")
        )

        # Deterministic check: new auth/session cookies appeared?
        cookies_after = await self._get_cookie_names(page)
        new_cookies = cookies_after - cookies_before

        # Strong signal: URL changed away from login AND new cookies
        if url_changed and new_cookies:
            return True

        # Strong signal: still on exact login path, no new cookies
        if not url_changed and not new_cookies:
            return False

        # Ambiguous: URL changed but no new cookies, or same URL but
        # new cookies appeared.  Fall back to AI judge if configured.
        if self.config.use_ai_judge and self.config.llm_api_key:
            return await self._ai_judge_authenticated(page)

        return None

    async def _ai_judge_authenticated(self, page: Page) -> Optional[bool]:
        """Use an LLM to judge whether the page looks like a successful login."""
        try:
            from ai_browser._llm_client import call_llm

            visible_text = await self._extract_visible_text(page)
            if not visible_text or len(visible_text) < 20:
                return None  # fail open

            messages = [{
                "role": "user",
                "content": (
                    "You are reviewing the visible text of a web page AFTER "
                    "a login form was submitted.\n\n"
                    "Visible page text:\n"
                    f"{visible_text[:3000]}\n\n"
                    "Does this page look like a successful login? Indicators "
                    "include: an account/profile element, a logout link, a "
                    "dashboard, a welcome message. Indicators of failure: "
                    "still showing a login form, error messages like 'invalid "
                    "credentials' or 'wrong password'. "
                    "Answer with exactly one word: YES or NO."
                ),
            }]
            response = await call_llm(
                provider=self.config.llm_provider,
                api_key=self.config.llm_api_key,
                model=self.config.llm_model,
                base_url=self.config.llm_base_url or None,
                messages=messages,
                max_tokens=10,
            )
            if not response:
                return None  # fail open

            result = "yes" in response.strip().lower()
            logger.info(
                "AI judge login verdict: %s",
                "authenticated" if result else "NOT authenticated",
            )
            return result
        except Exception as exc:
            logger.debug("AI judge login error: %s — failing open", exc)
            return None

    async def _extract_visible_text(self, page: Page) -> str:
        try:
            return await page.evaluate("""
                () => {
                    const els = document.querySelectorAll(
                        'h1, h2, h3, p, .alert, .error, .message, [role="alert"], form'
                    );
                    return [...els].map(el => el.textContent?.trim() || '')
                        .filter(t => t).join('\\n');
                }
            """) or ""
        except Exception:
            return ""

    async def _get_cookie_names(self, page: Page) -> set[str]:
        try:
            cookies = await page.context.cookies()
            return {c["name"] for c in cookies}
        except Exception:
            return set()

    # ------------------------------------------------------------------
    # CAPTCHA detection (delegates to shared helper)
    # ------------------------------------------------------------------

    async def _check_captcha(self, page: Page, stage: str) -> None:
        """Check for CAPTCHA using the shared helper."""
        await check_captcha(page, stage, self.config.captcha_screenshot_dir, self.config.login_url)


# ---------------------------------------------------------------------------
# Login URL candidate discovery (module-level, reusable)
# ---------------------------------------------------------------------------

_LOGIN_PATH_PATTERNS = [
    "/login", "/log-in", "/log_in", "/signin", "/sign-in", "/sign_in",
    "/auth/login", "/account/login", "/accounts/login",
]


def _discover_login_url(endpoints: list[str]) -> Optional[str]:
    """Scan *endpoints* for URLs whose path suggests a login page.

    Returns the best candidate, or None if none found.
    """
    for pattern in _LOGIN_PATH_PATTERNS:
        for url in endpoints:
            path = urlparse(url).path.lower().rstrip("/") or "/"
            if path == pattern or path.endswith(pattern) and "/" in path[:-len(pattern)]:
                return url
    # Fall back to substring match
    for url in endpoints:
        path = urlparse(url).path.lower()
        if "/login" in path or "/signin" in path or "/sign-in" in path:
            return url
    return None
