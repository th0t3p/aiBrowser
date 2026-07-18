"""LoginHandler — automated login form filling using shared form helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

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

    # ------------------------------------------------------------------
    # Main login flow
    # ------------------------------------------------------------------

    async def login(self, session: BrowserSession) -> Page:
        """Execute the full login flow.

        Returns the authenticated page after successful login.
        May raise CaptchaDetected.
        """
        logger.info("Starting login as %s on %s", self.config.email, self.config.login_url)

        page = await session.new_page()

        # Step 1: Navigate to login URL
        await page.goto(self.config.login_url, timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)

        # Step 2: Check for CAPTCHA
        await self._check_captcha(page, "login_form")

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

        logger.info("Login complete. Current URL: %s", page.url)
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
    # CAPTCHA detection (delegates to shared helper)
    # ------------------------------------------------------------------

    async def _check_captcha(self, page: Page, stage: str) -> None:
        """Check for CAPTCHA using the shared helper."""
        await check_captcha(page, stage, self.config.captcha_screenshot_dir, self.config.login_url)
