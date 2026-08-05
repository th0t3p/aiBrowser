"""RegistrationHandler — automated signup, IMAP email confirmation polling, CAPTCHA detection."""

from __future__ import annotations

import asyncio
import email
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Page

from ai_browser.browser_session import BrowserSession
from ai_browser._form_helpers import fill_form_fields, submit_form, check_captcha
from ai_browser._llm_client import call_llm

from .models import CaptchaDetected, DisposableInboxConfig, IMAPConfig, RegistrationConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signup-page candidate discovery (deterministic, no AI)
# ---------------------------------------------------------------------------

# Strong signup path patterns (exact path-segment match)
_STRONG_SIGNUP_PATTERNS = [
    "/signup", "/sign-up", "/sign_up", "/register", "/registration",
    "/join", "/create-account", "/createaccount", "/get-started",
]

# Weak patterns — match anywhere in the path, rank lower
_WEAK_SIGNUP_PATTERNS = [
    "signup", "sign-up", "register", "create-account", "join",
]


def discover_signup_url(
    endpoints: list[str],
    seed_hostname: str = "",
) -> Optional[str]:
    """Scan crawled endpoints for URLs whose path suggests a signup page.

    Returns the best candidate, or ``None`` if no plausible signup URL was
    found.  Strong (exact path-segment) matches rank above weak (substring)
    matches.  The caller should pass the result as ``signup_url`` in
    ``RegistrationConfig``, or fall back to the bare hostname root if
    ``None`` is returned.

    *endpoints* should be a list of URL strings (e.g. from
    ``result.endpoints`` in the crawl output).
    """

    strong_candidates: list[str] = []
    weak_candidates: list[str] = []

    for url in endpoints:
        parsed = urlparse(url)
        path = parsed.path.lower().rstrip("/") or "/"

        # Exact path-segment match (strong)
        for pattern in _STRONG_SIGNUP_PATTERNS:
            if path == pattern or path.endswith(pattern) and "/" in path[:-len(pattern)]:
                strong_candidates.append(url)
                break
        else:
            # Fall back to substring match (weak), but exclude false-positives like
            # "getting-started" or "create-an-app" which sound like docs, not signup
            for pattern in _WEAK_SIGNUP_PATTERNS:
                if pattern in path:
                    # Exclude known false-positive patterns
                    if not _looks_like_docs_page(path):
                        weak_candidates.append(url)
                    break

    # Prefer strong matches
    if strong_candidates:
        # If the seed hostname is in the URL, prefer it
        if seed_hostname:
            for url in strong_candidates:
                parsed = urlparse(url)
                if parsed.hostname and seed_hostname in parsed.hostname:
                    return url
        return strong_candidates[0]

    if weak_candidates:
        return weak_candidates[0]

    return None


_DOCS_FALSE_POSITIVE_RE = re.compile(
    r"(getting-started|docs?/|documentation|tutorial|guide)", re.IGNORECASE
)


def _looks_like_docs_page(path: str) -> bool:
    """Return True if *path* looks like a documentation/tutorial page rather
    than an actual signup form."""
    return bool(_DOCS_FALSE_POSITIVE_RE.search(path))


def _extract_link_from_body(body_text: str, target_domain: str = "") -> Optional[str]:
    """Extract a confirmation/verification link from plain text email body.

    Priority order:
    1. Links matching confirmation patterns (confirm, verify, activate, token=, code=)
    2. Links whose hostname contains *target_domain*
    3. First non-asset link in the body
    4. None if no links are found

    This is the shared core used by both IMAP (which extracts body_text from
    a parsed email.message.Message first) and disposable-inbox (which gets
    body text directly from the API response).
    """
    if not body_text:
        return None

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
        for link in non_asset_links:
            parsed = urlparse(link)
            if parsed.hostname and target_domain.lower() in parsed.hostname.lower():
                logger.debug("Found same-domain link: %s", link)
                return link

    return non_asset_links[0]


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
        self.confirmed: bool = False
        self._registration_looked_real: Optional[bool] = None
        self._provisioned_email: Optional[str] = None

    # ------------------------------------------------------------------
    # Main registration flow
    # ------------------------------------------------------------------

    async def register(self, session: BrowserSession) -> Page:
        """Execute the full registration flow."""
        # ---- Disposable inbox: provision before anything else ---------------
        if self.config.disposable_inbox_config:
            from . import disposable_inbox

            logger.info(
                "Provisioning disposable inbox (provider=%s)...",
                self.config.disposable_inbox_config.provider,
            )
            try:
                email = await disposable_inbox.provision_inbox(
                    self.config.disposable_inbox_config
                )
            except Exception:
                logger.exception("Failed to provision disposable inbox")
                raise  # fail fast — registration cannot work without an inbox

            # Set the email address dynamically (overwrites config.email which
            # should be None in disposable mode)
            self.config.email = email
            self._provisioned_email = email
            logger.info("Disposable inbox provisioned: %s", email)

        signup_url = self._resolve_signup_url()
        if not signup_url:
            logger.warning(
                "No signup URL found — candidate_endpoints=%s, configured "
                "signup_url=%s", self.config.candidate_endpoints, self.config.signup_url,
            )
            page = await session.new_page()
            self._current_page = page
            return page

        logger.info("Starting registration for %s on %s", self.config.email, signup_url)

        page = await session.new_page()

        await page.goto(signup_url, timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)

        await self._check_captcha(page, "signup_form")
        filled_fields = await self._fill_signup_form(page)
        await self._check_captcha(page, "signup_submit")

        # Do NOT submit if the email field was never found — this does not
        # look like a registration form at all (could be a newsletter box,
        # login form, or a completely unrelated page).
        if "email" not in filled_fields:
            logger.warning(
                "No email field found on %s — this does not look like a "
                "registration form (filled_fields=%s). Skipping submission.",
                signup_url, filled_fields,
            )
            self._current_page = page
            return page

        self._signup_submitted_at = asyncio.get_event_loop().time()
        await self._submit_form(page)

        await asyncio.sleep(2)
        await self._check_captcha(page, "post_submit")

        # ---- AI judge: did the submission actually look like a registration?
        if self.config.use_ai_judge and self.config.llm_api_key:
            self._registration_looked_real = await self._ai_judge_did_submit(page)

        # ---- Confirmation email: dispatch on backend ------------------------
        target_domain = urlparse(signup_url).hostname or ""
        if self.config.disposable_inbox_config:
            from . import disposable_inbox

            confirmation_link = await disposable_inbox.wait_for_confirmation_link(
                config=self.config.disposable_inbox_config,
                inbox_address=self.config.email or "",
                timeout_seconds=self.config.email_poll_timeout_seconds,
                target_domain=target_domain,
            )
            if confirmation_link:
                logger.info("Confirmation link found: %s", confirmation_link)
                await page.goto(confirmation_link, timeout=30_000)
                await page.wait_for_load_state("networkidle", timeout=15_000)
                self.confirmed = True
            else:
                logger.warning("No confirmation link found within timeout window")
        elif self.config.imap_config:
            confirmation_link = await self._poll_inbox_for_link(target_domain)
            if confirmation_link:
                logger.info("Confirmation link found: %s", confirmation_link)
                await page.goto(confirmation_link, timeout=30_000)
                await page.wait_for_load_state("networkidle", timeout=15_000)
                self.confirmed = True
            else:
                logger.warning("No confirmation link found within timeout window")
        else:
            logger.info("No IMAP or disposable inbox config; skipping email confirmation")

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
                self.confirmed = True

        return self._current_page

    # ------------------------------------------------------------------
    # Form filling (uses shared helpers)
    # ------------------------------------------------------------------

    async def _fill_signup_form(self, page: Page) -> list[str]:
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

        return await fill_form_fields(page, field_mappings)

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
    # Signup URL resolution + AI judge
    # ------------------------------------------------------------------

    def _resolve_signup_url(self) -> Optional[str]:
        """Return the best signup URL.

        If candidate_endpoints are provided in config, runs deterministic
        candidate discovery first.  Falls back to the configured signup_url.
        Returns None only if discovery found nothing and the configured URL
        is the bare hostname root (not a real signup URL).
        """
        candidates = self.config.candidate_endpoints
        if candidates:
            seed = urlparse(self.config.signup_url).hostname or ""
            discovered = discover_signup_url(candidates, seed_hostname=seed)
            if discovered:
                logger.info("Discovered signup URL from crawl endpoints: %s", discovered)
                return discovered
            logger.info(
                "No signup URL discovered from %d endpoints; using configured URL",
                len(candidates),
            )

        # If the configured URL is the bare hostname root (no path), and we had
        # candidates but found nothing, report that honestly.
        parsed = urlparse(self.config.signup_url)
        if (not parsed.path or parsed.path == "/") and candidates:
            return None

        return self.config.signup_url

    async def _ai_judge_did_submit(self, page: Page) -> Optional[bool]:
        """Use an LLM to judge whether the post-submit page looks like a
        registration was just submitted.

        Returns True (looks like registration), False (looks like something
        else), or None if the LLM call failed (fail open).
        """
        try:
            visible_text = await self._extract_visible_form_text(page)
            if not visible_text or len(visible_text) < 20:
                logger.debug("Not enough visible text for AI judge post-submit")
                return None  # fail open

            messages = [
                {
                    "role": "user",
                    "content": (
                        "You are reviewing the visible text of a web page AFTER "
                        "a registration form was submitted.\n\n"
                        "Visible page text:\n"
                        f"{visible_text[:3000]}\n\n"
                        "Does this page look like a new-account registration was "
                        "just submitted? Indicators include: 'check your email to "
                        "confirm', 'account created', 'verify your email', "
                        "'welcome', 'thank you for registering', or similar. "
                        "Answer with exactly one word: YES or NO."
                    ),
                },
            ]
            response = await call_llm(
                provider=self.config.llm_provider,
                api_key=self.config.llm_api_key,
                model=self.config.llm_model,
                base_url=self.config.llm_base_url or None,
                messages=messages,
                max_tokens=10,
            )
            if response is None:
                logger.debug("AI judge post-submit call failed — failing open")
                return None  # fail open

            looked_real = "yes" in response.strip().lower()
            logger.info(
                "AI judge post-submit verdict: %s (raw=%r)",
                "registration" if looked_real else "NOT registration", response[:100],
            )
            return looked_real

        except Exception as exc:
            logger.debug("AI judge post-submit error: %s — failing open", exc)
            return None  # fail open

    async def _extract_visible_form_text(self, page: Page) -> str:
        """Extract visible text near forms/headings from *page* for AI judging."""
        try:
            text = await page.evaluate("""
                () => {
                    const form = document.querySelector('form');
                    const els = [
                        ...document.querySelectorAll('h1, h2, h3, h4, p, label, button, .alert, .message, [role="alert"]'),
                    ];
                    if (form) els.push(form);
                    return els.map(el => el.textContent?.trim() || '').filter(t => t).join('\\n');
                }
            """)
            return text or ""
        except Exception as exc:
            logger.debug("Failed to extract visible text: %s", exc)
            return ""

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

            # Check up to the last 20 UNSEEN messages (newest first) instead
            # of only the single latest one.  On a shared personal inbox, an
            # unrelated unread email arriving during the poll window shouldn't
            # mask the real confirmation email sitting right behind it.
            _max_to_check = 20
            _to_check = message_ids[-_max_to_check:]  # newest first
            found_link = None

            for msg_id in reversed(_to_check):
                result, msg_data = await imap.fetch(msg_id, "(RFC822)")

                if result != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[1]
                if isinstance(raw_email, tuple):
                    raw_email = raw_email[1]

                msg = email.message_from_bytes(raw_email)

                if target_domain:
                    from_header = msg.get("From", "")
                    if target_domain.lower() not in from_header.lower():
                        logger.debug("Skipping email from %s (id=%s)", from_header, msg_id.decode() if isinstance(msg_id, bytes) else msg_id)
                        continue

                date_str = msg.get("Date", "")
                if date_str and self._signup_submitted_at > 0:
                    try:
                        from email.utils import parsedate_to_datetime
                        msg_date = parsedate_to_datetime(date_str)
                        if msg_date.timestamp() < self._signup_submitted_at:
                            logger.debug("Skipping old email from %s", date_str)
                            continue
                    except Exception:
                        pass

                found_link = self._extract_link_from_email(msg, target_domain)
                if found_link:
                    break

            await imap.logout()
            return found_link

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

        return _extract_link_from_body(body_text, target_domain)

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def captcha_info(self) -> Optional[CaptchaDetected]:
        return self._captcha_info

    @property
    def submitted(self) -> bool:
        """True if the registration form was actually submitted (i.e. the email
        field was found and _submit_form was called).  False when no signup
        form was detected or submission was skipped."""
        return self._signup_submitted_at > 0

    @property
    def registration_looked_real(self) -> Optional[bool]:
        """After submission, the AI judge's verdict on whether the post-submit
        page looked like a registration was actually submitted.

        * ``True`` — judge says it looked like a real registration.
        * ``False`` — judge says it looked like something else (newsletter,
          login, error, etc.).
        * ``None`` — judge was not run (use_ai_judge=False, no API key, or
          the form was never submitted).
        """
        return self._registration_looked_real

    @property
    def provisioned_email(self) -> Optional[str]:
        """The dynamically-provisioned email address, when disposable_inbox_config
        was used.  ``None`` for IMAP/static-email mode.  Populated after
        ``register()`` has run (it's set before any navigation)."""
        return self._provisioned_email
