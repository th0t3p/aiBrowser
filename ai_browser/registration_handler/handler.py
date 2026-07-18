"""RegistrationHandler — automated signup, IMAP email confirmation polling, CAPTCHA detection."""

from __future__ import annotations

import asyncio
import email
import logging
import re
from datetime import datetime
from typing import Optional

from playwright.async_api import Page

from ai_browser.browser_session import BrowserSession
from ai_browser._form_helpers import fill_form_fields, submit_form, check_captcha

from .models import CaptchaDetected, IMAPConfig, RegistrationConfig

logger = logging.getLogger(__name__)


class RegistrationHandler:
    """Handles automated registration form filling, email confirmation, and CAPTCHA detection.

    Usage::

        reg_config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test+target@mydomain.com",
            imap_config=IMAPConfig(host="imap.mydomain.com", username="test@mydomain.com", password="..."),
        )
        handler = RegistrationHandler(reg_config)
        try:
            await handler.register(session)
        except CaptchaDetected as cap:
            print(f"Screenshot saved: {cap.screenshot_path}")
            # ... user solves CAPTCHA manually, then calls handler.resume(session)
    """

    def __init__(self, config: RegistrationConfig):
        self.config = config
        self._current_page: Optional[Page] = None
        self._paused: bool = False
        self._captcha_info: Optional[CaptchaDetected] = None
        self._signup_submitted_at: float = 0.0

    # ------------------------------------------------------------------
    # Main registration flow
    # ------------------------------------------------------------------

    async def register(self, session: BrowserSession) -> Page:
        """Execute the full registration flow."""
        logger.info("Starting registration for %s on %s", self.config.email, self.config.signup_url)

        page = await session.new_page()

        await page.goto(self.config.signup_url, timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)

        await self._check_captcha(page, "signup_form")
        await self._fill_signup_form(page)
        await self._check_captcha(page, "signup_submit")

        self._signup_submitted_at = asyncio.get_event_loop().time()
        await self._submit_form(page)

        await asyncio.sleep(2)
        await self._check_captcha(page, "post_submit")

        if self.config.imap_config:
            from urllib.parse import urlparse
            target_domain = urlparse(self.config.signup_url).hostname or ""
            confirmation_link = await self._poll_inbox_for_link(target_domain)
            if confirmation_link:
                logger.info("Confirmation link found: %s", confirmation_link)
                await page.goto(confirmation_link, timeout=30_000)
                await page.wait_for_load_state("networkidle", timeout=15_000)
            else:
                logger.warning("No confirmation link found within timeout window")
        else:
            logger.info("No IMAP config provided; skipping email confirmation")

        self._current_page = page
        return page

    async def resume(self, session: BrowserSession) -> Page:
        """Resume the registration flow after a manual CAPTCHA solve."""
        if not self._current_page:
            raise RuntimeError("No paused registration to resume.")
        if not self._paused:
            raise RuntimeError("Registration is not paused.")

        self._paused = False
        self._captcha_info = None
        logger.info("Resuming registration after manual CAPTCHA solve")

        await self._submit_form(self._current_page)

        if self.config.imap_config:
            from urllib.parse import urlparse
            target_domain = urlparse(self.config.signup_url).hostname or ""
            confirmation_link = await self._poll_inbox_for_link(target_domain)
            if confirmation_link:
                await self._current_page.goto(confirmation_link, timeout=30_000)
                await self._current_page.wait_for_load_state("networkidle", timeout=15_000)

        return self._current_page

    # ------------------------------------------------------------------
    # Form filling (uses shared helpers)
    # ------------------------------------------------------------------

    async def _fill_signup_form(self, page: Page) -> None:
        logger.info("Attempting to fill signup form fields")

        field_mappings: list[tuple[list[str], str]] = [
            (["email", "email_address", "signup_email", "user[email]", "registration_email"],
             self.config.email),
            (["password", "passwd", "pwd", "user[password]", "registration_password"],
             self.config.password),
            (["password_confirmation", "confirm_password", "passwd_confirm", "password2",
              "user[password_confirmation]"],
             self.config.password),
        ]

        if self.config.name:
            field_mappings.extend([
                (["name", "full_name", "fullname", "display_name", "username",
                  "user[name]", "user[full_name]"],
                 self.config.name),
                (["first_name", "firstname", "given_name", "user[first_name]"],
                 self.config.name.split()[0] if self.config.name else ""),
                (["last_name", "lastname", "family_name", "surname", "user[last_name]"],
                 self.config.name.split()[-1] if self.config.name and " " in self.config.name else ""),
            ])

        await fill_form_fields(page, field_mappings)

    async def _submit_form(self, page: Page) -> None:
        signup_selectors = [
            "button:has-text('Sign Up')",
            "button:has-text('Register')",
            "button:has-text('Create Account')",
            "button:has-text('Sign up')",
            "button:has-text('register')",
            "button:has-text('Submit')",
        ]
        await submit_form(page, extra_selectors=signup_selectors)

    # ------------------------------------------------------------------
    # CAPTCHA detection (delegates to shared helper)
    # ------------------------------------------------------------------

    async def _check_captcha(self, page: Page, stage: str) -> None:
        try:
            await check_captcha(page, stage, self.config.captcha_screenshot_dir, self.config.signup_url)
        except CaptchaDetected as exc:
            self._captcha_info = exc
            self._paused = True
            self._current_page = page
            raise

    # ------------------------------------------------------------------
    # IMAP polling for confirmation email
    # ------------------------------------------------------------------

    async def _poll_inbox_for_link(self, target_domain="") -> Optional[str]:
        if not self.config.imap_config:
            return None

        logger.info(
            "Polling IMAP inbox %s for confirmation email (timeout=%ds)",
            self.config.imap_config.username,
            self.config.email_poll_timeout_seconds,
        )

        deadline = asyncio.get_event_loop().time() + self.config.email_poll_timeout_seconds

        while asyncio.get_event_loop().time() < deadline:
            link = await self._check_inbox_for_new_email(target_domain)
            if link:
                return link
            await asyncio.sleep(self.config.email_poll_interval_seconds)

        logger.warning("Timed out waiting for confirmation email")
        return None

    async def _check_inbox_for_new_email(self, target_domain=""):
        try:
            import aioimaplib

            imap_config = self.config.imap_config

            if imap_config.use_ssl:
                imap = aioimaplib.IMAP4_SSL(imap_config.host, imap_config.port)
            else:
                imap = aioimaplib.IMAP4(imap_config.host, imap_config.port)

            await imap.wait_hello_from_server()
            await imap.login(imap_config.username, imap_config.password)
            await imap.select(imap_config.mailbox)

            result, messages = await imap.search("UNSEEN")
            if result != "OK" or not messages or not messages[0]:
                await imap.logout()
                return None

            message_ids = messages[0].split()
            latest_id = message_ids[-1]

            result, msg_data = await imap.fetch(latest_id, "(RFC822)")
            await imap.logout()

            if result != "OK" or not msg_data or not msg_data[0]:
                return None

            raw_email = msg_data[1]
            if isinstance(raw_email, tuple):
                raw_email = raw_email[1]

            msg = email.message_from_bytes(raw_email)

            if target_domain:
                from_header = msg.get("From", "")
                if target_domain.lower() not in from_header.lower():
                    logger.debug("Skipping email from %s", from_header)
                    return None

            date_str = msg.get("Date", "")
            if date_str and self._signup_submitted_at > 0:
                try:
                    from email.utils import parsedate_to_datetime
                    msg_date = parsedate_to_datetime(date_str)
                    if msg_date.timestamp() < self._signup_submitted_at:
                        logger.debug("Skipping old email from %s", date_str)
                        return None
                except Exception:
                    pass

            return self._extract_link_from_email(msg, target_domain)

        except ImportError:
            logger.error("aioimaplib is required for IMAP polling")
            return None
        except Exception as exc:
            logger.error("IMAP check failed: %s", exc)
            return None

    def _extract_link_from_email(self, msg, target_domain=""):
        body_text = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type in ("text/plain", "text/html"):
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            body_text += payload.decode(charset, errors="replace")
                    except Exception:
                        continue
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
            except Exception:
                pass

        links = re.findall(r'https?://[^\s<>"\')\]]+', body_text)
        if not links:
            return None

        clean_links = [link.rstrip(".,;:'") for link in links]

        non_asset_links = [
            link for link in clean_links
            if not re.search(r'\.(png|jpg|jpeg|gif|svg|css|js)(\?|$)', link, re.IGNORECASE)
        ]
        if not non_asset_links:
            return clean_links[0]

        confirm_patterns = [r'confirm', r'verify', r'activate', r'token=', r'code=']
        for link in non_asset_links:
            if any(re.search(p, link, re.IGNORECASE) for p in confirm_patterns):
                logger.debug("Found confirmation link: %s", link)
                return link

        if target_domain:
            from urllib.parse import urlparse
            for link in non_asset_links:
                parsed = urlparse(link)
                if parsed.hostname and target_domain.lower() in parsed.hostname.lower():
                    logger.debug("Found same-domain link: %s", link)
                    return link

        return non_asset_links[0]

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def captcha_info(self) -> Optional[CaptchaDetected]:
        return self._captcha_info
