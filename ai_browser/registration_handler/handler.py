"""RegistrationHandler — automated signup, IMAP email confirmation polling, CAPTCHA detection."""

from __future__ import annotations

import asyncio
import email
import logging
import re
from datetime import datetime
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ai_browser.browser_session import BrowserSession

from .models import CaptchaDetected, IMAPConfig, RegistrationConfig

logger = logging.getLogger(__name__)


def _escape_css_string(value: str) -> str:
    """Escape special characters in a string for safe use in CSS attribute selectors."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


# Common CAPTCHA detection patterns
CAPTCHA_PATTERNS = [
    # reCAPTCHA
    ("recaptcha", "iframe[src*='recaptcha'], div.g-recaptcha, div[data-sitekey]"),
    # hCaptcha
    ("hcaptcha", "iframe[src*='hcaptcha'], div.h-captcha"),
    # Cloudflare Turnstile
    ("cf-turnstile", "div.cf-turnstile, iframe[src*='challenges.cloudflare.com']"),
    # Generic CAPTCHA indicators
    ("generic", "img[src*='captcha'], input[name*='captcha'], div[id*='captcha']"),
]


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
        self._signup_submitted_at: float = 0.0  # watermark for IMAP filtering

    # ------------------------------------------------------------------
    # Main registration flow
    # ------------------------------------------------------------------

    async def register(self, session: BrowserSession) -> Page:
        """Execute the full registration flow.

        Returns the authenticated page after successful registration and email
        confirmation. May raise CaptchaDetected, in which case the caller should
        solve it manually and call resume().
        """
        logger.info("Starting registration for %s on %s", self.config.email, self.config.signup_url)

        page = await session.new_page()

        # Step 1: Navigate to signup URL
        await page.goto(self.config.signup_url, timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)

        # Step 2: Check for CAPTCHA before proceeding
        await self._check_captcha(page, "signup_form")

        # Step 3: Fill the registration form
        await self._fill_signup_form(page)

        # Step 4: Check for CAPTCHA after form fill (some CAPTCHAs appear on submit)
        await self._check_captcha(page, "signup_submit")

        # Step 5: Submit the form (record watermark before submitting)
        self._signup_submitted_at = asyncio.get_event_loop().time()
        await self._submit_form(page)

        # Step 6: Wait a moment, then check for CAPTCHA again
        await asyncio.sleep(2)
        await self._check_captcha(page, "post_submit")

        # Step 7: Poll email inbox for confirmation link
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
        """Resume the registration flow after a manual CAPTCHA solve.

        The caller should have solved the CAPTCHA in a visible browser window
        and called this method to continue from where we left off.
        """
        if not self._current_page:
            raise RuntimeError("No paused registration to resume. Call register() first.")
        if not self._paused:
            raise RuntimeError("Registration is not paused (no CAPTCHA was detected).")

        self._paused = False
        self._captcha_info = None

        logger.info("Resuming registration after manual CAPTCHA solve")

        # Submit the form again since the CAPTCHA should now be solved
        await self._submit_form(self._current_page)

        # Continue with email confirmation
        if self.config.imap_config:
            from urllib.parse import urlparse
            target_domain = urlparse(self.config.signup_url).hostname or ""
            confirmation_link = await self._poll_inbox_for_link(target_domain)
            if confirmation_link:
                await self._current_page.goto(confirmation_link, timeout=30_000)
                await self._current_page.wait_for_load_state("networkidle", timeout=15_000)

        return self._current_page

    # ------------------------------------------------------------------
    # Form filling
    # ------------------------------------------------------------------

    async def _fill_signup_form(self, page: Page) -> None:
        """Heuristically fill common signup form fields."""
        logger.info("Attempting to fill signup form fields")

        # Common field name patterns -> value
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

        for names, value in field_mappings:
            for name in names:
                try:
                    field = await page.query_selector(
                        f"input[name='{name}'], input[id='{name}'], "
                        f"input[placeholder*='{name.replace('_', ' ')}' i]"
                    )
                    if field and await field.is_visible():
                        await field.fill(value)
                        logger.debug("Filled field '%s' with value", name)
                        break
                except Exception:
                    continue

    async def _submit_form(self, page: Page) -> None:
        """Find and submit the form."""
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Sign Up')",
            "button:has-text('Register')",
            "button:has-text('Create Account')",
            "button:has-text('Sign up')",
            "button:has-text('register')",
            "button:has-text('Submit')",
            "form button",
        ]

        for selector in submit_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    logger.info("Clicked submit button: %s", selector)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except PlaywrightTimeout:
                        pass
                    return
            except Exception:
                continue

        # Fallback: press Enter on any focused form element
        try:
            await page.keyboard.press("Enter")
            await asyncio.sleep(2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # CAPTCHA detection
    # ------------------------------------------------------------------

    async def _check_captcha(self, page: Page, stage: str) -> None:
        """Check the current page for CAPTCHA elements. Raises CaptchaDetected if found."""
        for captcha_type, selector in CAPTCHA_PATTERNS:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    # Save a screenshot for manual review
                    self.config.captcha_screenshot_dir.mkdir(parents=True, exist_ok=True)
                    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                    hostname = self.config.signup_url.replace("://", "_").replace("/", "_")[:60]
                    screenshot_path = (
                        self.config.captcha_screenshot_dir
                        / f"captcha_{hostname}_{stage}_{timestamp}.png"
                    )
                    await page.screenshot(path=str(screenshot_path), full_page=True)

                    exc = CaptchaDetected(
                        page_url=page.url,
                        captcha_type=captcha_type,
                        screenshot_path=screenshot_path,
                    )
                    self._captcha_info = exc
                    self._paused = True
                    self._current_page = page

                    logger.warning("CAPTCHA detected: %s at %s", captcha_type, page.url)
                    raise exc
            except CaptchaDetected:
                raise
            except Exception:
                continue

    # ------------------------------------------------------------------
    # IMAP polling for confirmation email
    # ------------------------------------------------------------------

    async def _poll_inbox_for_link(self, target_domain="") -> Optional[str]:
        """Poll the configured IMAP inbox for a confirmation email and extract the link."""
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
        """Check the IMAP inbox for a recent unread email matching the target domain."""
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

            # Search for unseen emails
            result, messages = await imap.search("UNSEEN")
            if result != "OK" or not messages or not messages[0]:
                await imap.logout()
                return None

            message_ids = messages[0].split()
            # Only check the most recent
            latest_id = message_ids[-1]

            result, msg_data = await imap.fetch(latest_id, "(RFC822)")
            await imap.logout()

            if result != "OK" or not msg_data or not msg_data[0]:
                return None

            # Parse the email body
            raw_email = msg_data[1]
            if isinstance(raw_email, tuple):
                raw_email = raw_email[1]

            msg = email.message_from_bytes(raw_email)

            # Filter by sender domain: skip emails not from the target domain
            if target_domain:
                from_header = msg.get("From", "")
                if target_domain.lower() not in from_header.lower():
                    logger.debug("Skipping email from %s (not from %s)", from_header, target_domain)
                    return None

            # Filter by date watermark: skip emails received before signup was submitted
            date_str = msg.get("Date", "")
            if date_str and self._signup_submitted_at > 0:
                try:
                    from email.utils import parsedate_to_datetime
                    msg_date = parsedate_to_datetime(date_str)
                    if msg_date.timestamp() < self._signup_submitted_at:
                        logger.debug("Skipping old email from %s", date_str)
                        return None
                except Exception:
                    pass  # unparseable date, proceed anyway

            # Extract links from the email body
            link = self._extract_link_from_email(msg, target_domain)
            return link

        except ImportError:
            logger.error("aioimaplib is required for IMAP polling. Install with: pip install aioimaplib")
            return None
        except Exception as exc:
            logger.error("IMAP check failed: %s", exc)
            return None

    def _extract_link_from_email(self, msg, target_domain=""):
        """Extract the confirmation link from an email body.

        Prioritizes links whose path/query contains confirmation signals
        (confirm, verify, activate, token=, code=) or that match the target domain.
        Falls back to the first non-image HTTP link as a last resort.
        """
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

        # Find all http/https links
        links = re.findall(r'https?://[^\s<>"\')\]]+', body_text)
        if not links:
            return None

        clean_links = [link.rstrip(".,;:'") for link in links]

        # Skip image/tracking links
        non_asset_links = [
            link for link in clean_links
            if not re.search(r'\.(png|jpg|jpeg|gif|svg|css|js)(\?|$)', link, re.IGNORECASE)
        ]
        if not non_asset_links:
            return clean_links[0]

        # Priority 1: links with confirmation signals in path/query
        confirm_patterns = [r'confirm', r'verify', r'activate', r'token=', r'code=']
        for link in non_asset_links:
            if any(re.search(p, link, re.IGNORECASE) for p in confirm_patterns):
                logger.debug("Found confirmation link: %s", link)
                return link

        # Priority 2: links matching the target domain
        if target_domain:
            from urllib.parse import urlparse
            for link in non_asset_links:
                parsed = urlparse(link)
                if parsed.hostname and target_domain.lower() in parsed.hostname.lower():
                    logger.debug("Found same-domain link: %s", link)
                    return link

        # Priority 3: first non-asset link (fallback)
        return non_asset_links[0]

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def captcha_info(self) -> Optional[CaptchaDetected]:
        return self._captcha_info
