"""RegistrationHandler — automated signup, IMAP email confirmation polling, CAPTCHA detection."""

from __future__ import annotations

import asyncio
import email
import email.utils
import json
import logging
import re
from datetime import datetime
from typing import Optional

import tldextract
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


def _sender_domain(msg) -> str:
    """Extract the domain part of the From header's actual email address
    (handles both 'Name <addr@domain>' and bare 'addr@domain' forms)."""
    _, addr = email.utils.parseaddr(msg.get("From", ""))
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def _same_registrable_domain(a: str, b: str) -> bool:
    """True if *a* and *b* share the same registrable domain.

    Examples::

        _same_registrable_domain("developers.tiktok.com", "dev.tiktok.com")  # → True
        _same_registrable_domain("dev.tiktok.com", "spam.evil.com")          # → False
        _same_registrable_domain("foo.example.co.uk", "bar.example.co.uk")   # → True
    """
    if not a or not b:
        return False
    try:
        ext_a = tldextract.extract(a)
        ext_b = tldextract.extract(b)
        return (ext_a.domain, ext_a.suffix) == (ext_b.domain, ext_b.suffix)
    except Exception:
        return False


async def _collect_visible_inputs(page: Page) -> tuple[list, list[dict]]:
    """Return (element_handles, descriptions) for every visible <input>
    on the page, in DOM order. Purely descriptive — no filtering by
    purpose, no assumptions about which inputs matter. The AI decides
    that; this just gathers facts.
    """
    elements = await page.query_selector_all("input")
    handles = []
    descriptions = []
    for el in elements:
        try:
            if not await el.is_visible():
                continue
        except Exception:
            continue
        handles.append(el)
        descriptions.append({
            "index": len(handles) - 1,
            "type": await el.get_attribute("type") or "",
            "name": await el.get_attribute("name") or "",
            "id": await el.get_attribute("id") or "",
            "placeholder": await el.get_attribute("placeholder") or "",
            "maxlength": await el.get_attribute("maxlength") or "",
            "aria_label": await el.get_attribute("aria-label") or "",
            "class": (await el.get_attribute("class") or "")[:100],
        })
    return handles, descriptions


async def _ai_plan_code_input_fill(
    *,
    descriptions: list[dict],
    code: str,
    llm_provider: str,
    llm_api_key: str,
    llm_model: str,
    llm_base_url: Optional[str] = None,
) -> Optional[list[dict]]:
    """Ask the AI how a verification code should be distributed across
    the visible input elements on the page. Returns a list of
    {"index": int, "value": str} assignments (possibly a single entry
    covering the whole code, or one entry per box for a split-digit
    UI), or None if no plan could be determined.

    Fails closed: any error, empty/unparseable response, or a response
    that fails validation against the actual candidate list and code
    returns None — the caller should treat this the same as 'no code
    field found', not attempt a best-guess fallback that might type the
    code into the wrong place on a live target.
    """
    try:
        messages = [{
            "role": "user",
            "content": (
                f"A web form needs a verification code entered: {code}\n\n"
                "Below is a JSON list of visible <input> elements "
                "currently on the page, described by their attributes "
                "(not their live content — these are empty form "
                "fields). Some may be unrelated to the code (email, "
                "name, search boxes, etc.) — ignore those entirely.\n\n"
                "Decide how the code should be entered:\n"
                "- If ONE input is clearly the code field, it should "
                "receive the whole code.\n"
                "- If SEVERAL inputs together form a split-digit/split-"
                "character code entry (e.g. maxlength of 1, sequential "
                "naming, or simply several small inputs grouped "
                "together with no other plausible purpose), each "
                "should receive one character, in left-to-right/DOM "
                "order, together spelling out the full code.\n"
                "- If none of these inputs look like the right place "
                "for this code, say so.\n\n"
                f"Inputs:\n{json.dumps(descriptions, indent=2)}\n\n"
                "Respond with ONLY a JSON array, nothing else — no "
                "explanation, no markdown code fences. Each element: "
                '{"index": <int from the list above>, "value": '
                '"<exact substring of the code for this input>"}. '
                "If no inputs are appropriate, respond with exactly: []"
            ),
        }]
        response = await call_llm(
            provider=llm_provider,
            api_key=llm_api_key,
            model=llm_model,
            base_url=llm_base_url,
            messages=messages,
        )
        if not response:
            logger.warning(
                "AI code-input planning: call failed or returned empty response"
            )
            return None

        text = response.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()

        try:
            plan = json.loads(text)
        except Exception:
            logger.warning(
                "AI code-input planning: response wasn't valid JSON: %r",
                text[:200],
            )
            return None

        if not isinstance(plan, list):
            logger.warning(
                "AI code-input planning: response wasn't a JSON array: %r",
                text[:200],
            )
            return None
        if not plan:
            logger.info(
                "AI code-input planning: model found no appropriate "
                "input for this code"
            )
            return None

        valid_indices = {d["index"] for d in descriptions}
        validated = []
        for item in plan:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            value = item.get("value", "")
            if (
                idx not in valid_indices
                or not isinstance(value, str)
                or not value
                or value not in code
            ):
                logger.warning(
                    "AI code-input planning: discarding invalid "
                    "assignment %r", item,
                )
                continue
            validated.append({"index": idx, "value": value})

        return validated if validated else None
    except Exception as exc:
        logger.warning("AI code-input planning: error calling LLM: %s", exc)
        return None

# Diagnostic-only error phrases for post-submit page text — logged as a
# hint for the operator but never used to drive control flow (keyword-
# matching arbitrary error copy is too fragile for pass/fail decisions).
_ERROR_TEXT_PATTERNS = [
    "invalid code", "incorrect code", "wrong code", "code expired",
    "code has expired", "please try again", "verification failed",
    "invalid verification", "expired verification",
]


def _looks_like_docs_page(path: str) -> bool:
    """Return True if *path* looks like a documentation/tutorial page rather
    than an actual signup form."""
    return bool(_DOCS_FALSE_POSITIVE_RE.search(path))


async def _ai_extract_confirmation_action(
    *,
    body_text: str,
    target_domain: str,
    page_expects_code: bool,
    llm_provider: str,
    llm_api_key: str,
    llm_model: str,
    llm_base_url: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """AI-based extraction of the confirmation action from a registration/
    verification email — replaces separate regex link-extraction and
    AI code-extraction with a single read that decides which kind of
    confirmation this is AND extracts the value.

    Returns ("code", value) or ("link", url), or None.

    Fails closed: any error, empty response, or a response that doesn't
    parse into one of the two expected forms returns None rather than
    guessing — a missed extraction just means the poll loop retries next
    iteration, but acting on a wrong guess (submitting a bogus code, or
    navigating to an unrelated/tracking link) is a real, harder-to-undo
    mistake against a live target.
    """
    page_hint = (
        "The site's own signup page currently shows a visible code/PIN "
        "entry field, which suggests (but doesn't guarantee) this flow "
        "expects a typed code rather than a link click — weigh this "
        "alongside the actual email content, don't override clear "
        "evidence in the email itself."
        if page_expects_code else
        "No code-entry field was detected on the site's signup page, "
        "which weakly suggests a link-based flow, but check the email "
        "content itself rather than relying on this alone."
    )
    try:
        messages = [{
            "role": "user",
            "content": (
                "The following is the raw content of an account "
                "registration/verification email. It may contain HTML "
                "markup, inline styling, tracking pixels, footer "
                "boilerplate, unsubscribe links, and other noise — "
                "ignore all of that.\n\n"
                f"{page_hint}\n\n"
                "Determine how this email wants the recipient to "
                "confirm their account:\n"
                "- If it provides a short code/PIN the recipient is "
                "meant to type into a form, that's a CODE.\n"
                "- If it provides a specific button/link the recipient "
                "is meant to click to complete verification (NOT a "
                "tracking pixel, NOT an image URL, NOT an unsubscribe "
                "or footer link, NOT the sender's homepage), that's a "
                "LINK.\n\n"
                f"Email content:\n{body_text[:4000]}\n\n"
                "Respond with EXACTLY ONE of the following formats, "
                "nothing else:\n"
                "CODE: <the exact code>\n"
                "LINK: <the exact full URL>\n"
                "NONE"
            ),
        }]
        response = await call_llm(
            provider=llm_provider,
            api_key=llm_api_key,
            model=llm_model,
            base_url=llm_base_url,
            messages=messages,
        )
        if not response:
            logger.warning(
                "AI confirmation extraction: call failed or returned empty response"
            )
            return None

        text = response.strip()
        if text.upper() == "NONE":
            logger.info(
                "AI confirmation extraction: model reported no actionable "
                "confirmation in this email"
            )
            return None

        if text.upper().startswith("CODE:"):
            candidate = text[len("CODE:"):].strip().strip("\"'` ")
            if re.fullmatch(r"[A-Za-z0-9\-]{3,12}", candidate):
                return ("code", candidate)
            logger.warning(
                "AI confirmation extraction: CODE response doesn't look "
                "like a code, discarding: %r", candidate,
            )
            return None

        if text.upper().startswith("LINK:"):
            candidate = text[len("LINK:"):].strip().strip("\"'` ")
            if re.match(r"^https?://\S+$", candidate):
                return ("link", candidate)
            logger.warning(
                "AI confirmation extraction: LINK response doesn't look "
                "like a URL, discarding: %r", candidate,
            )
            return None

        logger.warning(
            "AI confirmation extraction: response didn't match expected "
            "format, discarding: %r", text[:200],
        )
        return None
    except Exception as exc:
        logger.warning(
            "AI confirmation extraction: error calling LLM: %s", exc
        )
        return None


def _detect_error_text(body_text: str) -> Optional[str]:
    """Return the first matching error phrase found in the page's visible
    text, or None. Diagnostic only — logged as a hint for the operator,
    never used to alter control flow or set self.confirmed."""
    lowered = body_text.lower()
    for pattern in _ERROR_TEXT_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


async def _ai_judge_is_registration_email_text(
    *,
    sender: str,
    subject: str,
    body_text: str,
    hostname: str,
    llm_provider: str,
    llm_api_key: str,
    llm_model: str,
    llm_base_url: Optional[str] = None,
) -> bool:
    """Standalone, reusable AI classifier for registration/verification emails.

    IMPORTANT — this fails CLOSED (returns False), not open, on any
    error/empty response. Every other AI judge in this codebase fails
    open (returns None, caller doesn't block progress on an inconclusive
    read) because being wrong there just means proceeding without extra
    confirmation. Here, being wrong in the "yes, use it" direction means
    acting on a stranger's unrelated email — the safe default when
    uncertain is to NOT extract/use it, not to assume yes.
    """
    try:
        messages = [{
            "role": "user",
            "content": (
                f"An automated signup was just submitted on "
                f"{hostname or 'a website'}.\n"
                f"Here is the single most recent unread email in the "
                f"inbox used for that signup:\n\n"
                f"From: {sender}\nSubject: {subject}\n\nBody:\n{body_text[:3000]}\n\n"
                "Does this email look like an account registration or "
                "email-verification message related to that signup "
                "(e.g. contains a confirmation link, a verification "
                "code/PIN, or similar account-activation language)? "
                "It may be sent from a different domain than the site "
                "itself — that's normal for transactional email "
                "providers — so judge by content, not just the sender "
                "address.\n\n"
                "Answer with exactly one word: YES or NO."
            ),
        }]
        response = await call_llm(
            provider=llm_provider,
            api_key=llm_api_key,
            model=llm_model,
            base_url=llm_base_url,
            messages=messages,
        )
        if not response:
            logger.debug("AI classification call failed or empty — failing closed to NO")
            return False
        return "yes" in response.strip().lower()
    except Exception as exc:
        logger.debug("AI classification error: %s — failing closed to NO", exc)
        return False


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
        self.login_verified: Optional[bool] = None
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

        # ---- Check what the page is asking for -------------------------------
        # Before touching the email, inspect the page's own DOM: if it shows
        # a code/PIN/OTP input field, extraction should prioritize code over
        # link — the page is the ground truth for what mechanism is in play.
        self._page_expects_code = await self._page_expects_code_check(page)
        if self._page_expects_code:
            logger.info(
                "Post-submit page has a code-entry field — will prioritize "
                "extracting a code from the confirmation email over a link"
            )

        # ---- Confirmation email: dispatch on backend ------------------------
        target_domain = urlparse(signup_url).hostname or ""
        if self.config.disposable_inbox_config:
            from . import disposable_inbox

            confirmation_link = await disposable_inbox.wait_for_confirmation_link(
                config=self.config.disposable_inbox_config,
                inbox_address=self.config.email or "",
                timeout_seconds=self.config.email_poll_timeout_seconds,
                target_domain=target_domain,
                llm_provider=self.config.llm_provider,
                llm_api_key=self.config.llm_api_key,
                llm_model=self.config.llm_model,
                llm_base_url=self.config.llm_base_url or None,
            )
            if confirmation_link:
                await self._handle_confirmation_result(page, confirmation_link)
            else:
                logger.warning("No confirmation link found within timeout window")
        elif self.config.imap_config:
            result = await self._poll_inbox_for_link(target_domain)
            if result:
                await self._handle_confirmation_result(page, result)
            else:
                logger.warning("No confirmation link found within timeout window")
        else:
            logger.info("No IMAP or disposable inbox config; skipping email confirmation")

        # ---- Post-confirmation: verify the account actually works ---------
        if self.confirmed:
            self.login_verified = await self._verify_via_login(session)
            if self.login_verified is True:
                logger.info("Post-confirmation login succeeded — account is active")
            elif self.login_verified is False:
                logger.warning(
                    "Confirmation action completed but follow-up login DID NOT "
                    "succeed — the account may not actually be active"
                )
            else:
                logger.info(
                    "Confirmation action completed; login verification was "
                    "inconclusive — account status unknown"
                )

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
            result = await self._poll_inbox_for_link(target_domain)
            if result:
                await self._handle_confirmation_result(self._current_page, result)

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

    async def _page_expects_code_check(self, page: Page) -> bool:
        """True if the page's visible inputs look to the AI like they
        expect a verification code/PIN/OTP — a *before-the-fact* check
        (no filling), used to decide extraction priority before ever
        looking at the email.
        """
        _, descriptions = await _collect_visible_inputs(page)
        if not descriptions:
            return False
        try:
            messages = [{
                "role": "user",
                "content": (
                    "Below is a JSON list of visible <input> elements on a "
                    "web page, described by their attributes.\n\n"
                    f"{json.dumps(descriptions, indent=2)}\n\n"
                    "Does this look like a page asking the user to enter a "
                    "verification code, PIN, or OTP (whether as one field "
                    "or several small boxes forming one code together)? "
                    "Answer with exactly one word: YES or NO."
                ),
            }]
            response = await call_llm(
                provider=self.config.llm_provider,
                api_key=self.config.llm_api_key,
                model=self.config.llm_model,
                base_url=self.config.llm_base_url or None,
                messages=messages,
            )
            return bool(response) and "yes" in response.strip().lower()
        except Exception as exc:
            logger.debug("Page-expects-code AI check failed: %s", exc)
            return False

    async def _submit_verification_code(self, page: Page, code: str) -> bool:
        """Fill a verification code into the page and submit the form.

        Uses AI to decide how to distribute the code across visible
        inputs — handles single-field, split-digit boxes, and custom
        widgets without any hardcoded assumptions about markup.
        """
        handles, descriptions = await _collect_visible_inputs(page)
        if not descriptions:
            logger.info(
                "No visible input fields found on the page to fill "
                "the code into"
            )
            return False

        plan = await _ai_plan_code_input_fill(
            descriptions=descriptions,
            code=code,
            llm_provider=self.config.llm_provider,
            llm_api_key=self.config.llm_api_key,
            llm_model=self.config.llm_model,
            llm_base_url=self.config.llm_base_url or None,
        )
        if not plan:
            logger.info(
                "AI could not determine how to enter the code %s into "
                "any field on this page", code,
            )
            return False

        for assignment in plan:
            el = handles[assignment["index"]]
            try:
                await el.click()
                await el.press_sequentially(assignment["value"])
            except Exception as exc:
                logger.warning(
                    "Failed to fill input at index %d: %s",
                    assignment["index"], exc,
                )
                return False

        logger.info(
            "Filled code across %d input(s) per AI plan; submitting...",
            len(plan),
        )
        verify_selectors = [
            "button:has-text('Verify')",
            "button:has-text('Confirm')",
            "button:has-text('Submit')",
            "button:has-text('Continue')",
            "button:has-text('Next')",
        ]
        url_before = page.url
        await submit_form(page, extra_selectors=verify_selectors)

        # Log what the page shows after submission — purely diagnostic.
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        url_after = page.url
        body_text = await self._get_visible_page_text(page)

        logger.info(
            "Post-submit page state — url_before=%s url_after=%s navigated=%s",
            url_before, url_after, url_before != url_after,
        )
        logger.info(
            "Post-submit page text (first 500 chars): %r",
            body_text[:500].replace("\n", " "),
        )

        error_indicators = _detect_error_text(body_text)
        if error_indicators:
            logger.warning(
                "Post-submit page appears to show an error/rejection: %r",
                error_indicators,
            )

        return True

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
                        "'welcome', 'thank you for registering', a request to "
                        "enter a verification code or PIN that was sent to your "
                        "email ('enter the code below', 'we sent you a code', "
                        "a code/PIN input field), or similar. "
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
            )
            if not response:
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

    async def _get_visible_page_text(self, page: Page) -> str:
        """Best-effort extraction of the page's visible text, for diagnostic
        logging only — not used for any pass/fail decision."""
        try:
            return await page.inner_text("body")
        except Exception as exc:
            logger.debug("Could not read page body text: %s", exc)
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

    async def _verify_via_login(self, session) -> Optional[bool]:
        """Attempt a real login with the registration credentials to verify
        the account is actually active. Returns True/False/None matching
        LoginHandler.authenticated semantics."""
        try:
            from ai_browser.login_handler import LoginHandler
            from ai_browser.login_handler.models import LoginConfig

            hostname = urlparse(self.config.signup_url).hostname or ""
            login_url = (
                self.config.login_verify_url
                or f"https://{hostname}/login"
            )
            login_config = LoginConfig(
                login_url=login_url,
                email=self.config.email or "",
                password=self.config.password,
            )
            handler = LoginHandler(login_config)
            await handler.login(session)
            return handler.authenticated
        except Exception as exc:
            logger.warning("Post-confirmation login verification failed to run: %s", exc)
            return None

    async def _handle_confirmation_result(
        self, page: Page, result: tuple[str, str],
    ) -> None:
        """Dispatch on the extraction result: link → navigate, code → fill + submit."""
        kind, value = result
        if kind == "link":
            logger.info("Confirmation link found: %s", value)
            await page.goto(value, timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            self.confirmed = True
        elif kind == "code":
            logger.info("Verification code extracted: %s", value)
            code_submitted = await self._submit_verification_code(page, value)
            if code_submitted:
                # Code entry field found and submitted — treat as confirmed.
                # A detected code-entry field is also strong evidence this
                # is a real registration, overriding any prior AI false negative.
                self.confirmed = True
                if self._registration_looked_real is False:
                    logger.info(
                        "Code-entry field detected on page — overriding "
                        "earlier 'NOT a registration' AI judge verdict"
                    )
                    self._registration_looked_real = True
            else:
                logger.warning(
                    "Verification code was extracted but no matching code "
                    "field was found on the page (reason=code_found_no_field)"
                )

    async def _log_recent_mailbox_state(self, imap_config) -> None:
        """Diagnostic only — logs the most recent messages in the mailbox
        regardless of Seen status, so a silent 'nothing found' can be told
        apart from 'genuinely nothing arrived' vs 'arrived but not UNSEEN
        anymore' vs 'arrived in a different folder than we're polling'."""
        try:
            import aioimaplib
            imap = (aioimaplib.IMAP4_SSL(imap_config.host, imap_config.port)
                    if imap_config.use_ssl else
                    aioimaplib.IMAP4(imap_config.host, imap_config.port))
            await imap.wait_hello_from_server()
            await imap.login(imap_config.username, imap_config.password)
            await imap.select(imap_config.mailbox)
            result, messages = await imap.search("ALL")
            if result != "OK" or not messages or not messages[0]:
                logger.info(
                    "Diagnostic: mailbox %s has 0 messages total",
                    imap_config.mailbox,
                )
                await imap.logout()
                return
            ids = [
                mid.decode() if isinstance(mid, bytes) else mid
                for mid in messages[0].split()[-5:]
            ]
            logger.info(
                "Diagnostic: %d most recent message(s) in %s (any read status):",
                len(ids), imap_config.mailbox,
            )
            for msg_id in reversed(ids):
                result, msg_data = await imap.fetch(msg_id, "(BODY.PEEK[HEADER])")
                if result != "OK" or not msg_data or not msg_data[0]:
                    logger.warning(
                        "IMAP fetch failed for diagnostic message %s "
                        "(result=%s) — skipping",
                        msg_id, result,
                    )
                    continue
                raw = msg_data[1]
                if isinstance(raw, tuple):
                    raw = raw[1]
                msg = email.message_from_bytes(raw)
                logger.info(
                    "  from=%s subject=%r date=%s",
                    msg.get("From", ""), msg.get("Subject", ""), msg.get("Date", ""),
                )
            await imap.logout()
        except Exception as exc:
            logger.error("Diagnostic mailbox scan failed: %s", exc)

    async def _poll_inbox_for_link(self, target_domain="") -> Optional[str]:
        if not self.config.imap_config:
            return None

        logger.info(
            "Polling IMAP inbox %s for confirmation email (timeout=%ds)",
            self.config.imap_config.username,
            self.config.email_poll_timeout_seconds,
        )

        deadline = asyncio.get_event_loop().time() + self.config.email_poll_timeout_seconds
        reason = "no_email_received"

        while asyncio.get_event_loop().time() < deadline:
            link = await self._check_inbox_for_new_email(target_domain)
            if link:
                return link
            await asyncio.sleep(self.config.email_poll_interval_seconds)

        # No extractable link found in Tiers 1-2 — at this point we know
        # that if any mail arrived, it didn't contain a URL the regex could
        # pick up.  (A PIN/code-only email lands here, for instance.)
        reason = "email_found_no_extractable_content"

        # Diagnostic: log what's actually in the mailbox right now,
        # regardless of read status, so the operator can see whether mail
        # arrived but isn't UNSEEN (wrong folder, spam, seen-mutation),
        # or genuinely never arrived at all.
        if self.config.imap_config:
            await self._log_recent_mailbox_state(self.config.imap_config)

        # Tier 3 — last resort, exactly once, only after Tiers 1-2 found
        # nothing across the ENTIRE poll window. This is deliberately placed
        # OUTSIDE the while loop so it can never fire more than once per
        # _poll_inbox_for_link call, regardless of how many poll iterations
        # happened — an LLM call every email_poll_interval_seconds would be
        # wasteful and pointless while there's legitimately no mail yet.
        logger.info(
            "No domain-matched or content-extractable email found across the "
            "poll window — trying AI classification of the latest unread "
            "message as a last resort"
        )
        link = await self._ai_classify_and_extract_latest_unread(target_domain)
        if link:
            return link

        # If Tier 3 ran and the AI judge explicitly said NO, override the
        # reason so the operator knows the email arrived but was rejected.
        if getattr(self, "_tier3_ai_judge_rejected", False):
            reason = "ai_judge_rejected"

        logger.warning(
            "No confirmation link found within timeout window (reason=%s)",
            reason,
        )
        return None

    async def _check_inbox_for_new_email(self, target_domain="") -> Optional[str]:
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
            count = (
                len(messages[0].split())
                if (result == "OK" and messages and messages[0])
                else 0
            )
            logger.info("IMAP poll: %d UNSEEN message(s) in %s", count, imap_config.mailbox)
            if count == 0:
                await imap.logout()
                return None

            message_ids = [
                mid.decode() if isinstance(mid, bytes) else mid
                for mid in messages[0].split()
            ]

            # Check up to the last 20 UNSEEN messages (newest first) instead
            # of only the single latest one.  On a shared personal inbox, an
            # unrelated unread email arriving during the poll window shouldn't
            # mask the real confirmation email sitting right behind it.
            _max_to_check = 20
            _to_check = message_ids[-_max_to_check:]

            # Pass 1: fetch + date-filter every candidate, split into
            # domain-matching vs. everything-else, preserving newest-first
            # order within each group.
            domain_matched: list = []
            others: list = []
            for msg_id in reversed(_to_check):
                result, msg_data = await imap.fetch(msg_id, "(BODY.PEEK[])")

                if result != "OK" or not msg_data or not msg_data[0]:
                    logger.warning(
                        "IMAP fetch failed for message %s (result=%s) — skipping",
                        msg_id, result,
                    )
                    continue

                raw_email = msg_data[1]
                if isinstance(raw_email, tuple):
                    raw_email = raw_email[1]

                msg = email.message_from_bytes(raw_email)

                date_str = msg.get("Date", "")
                if date_str and self._signup_submitted_at > 0:
                    try:
                        from email.utils import parsedate_to_datetime
                        msg_date = parsedate_to_datetime(date_str)
                        if msg_date.timestamp() < self._signup_submitted_at:
                            continue
                    except Exception:
                        pass

                # Diagnostic: log every inspected candidate
                body_preview = self._get_email_body_text(msg)[:3000].replace("\n", " ")
                logger.info(
                    "Candidate confirmation email — from=%s subject=%r body_preview=%r",
                    msg.get("From", ""),
                    msg.get("Subject", ""),
                    body_preview,
                )

                if target_domain and _same_registrable_domain(_sender_domain(msg), target_domain):
                    domain_matched.append(msg)
                else:
                    others.append(msg)

            # Tier 1: domain-matching candidates, deterministic extraction.
            # NOTE: target_domain is still passed through to
            # _extract_link_from_email — that's a SEPARATE use (preferring
            # links WITHIN the email body whose own hostname matches the
            # target), not the sender check. Don't conflate the two.
            for msg in domain_matched:
                found = await self._extract_link_from_email(msg, target_domain)
                if found:
                    await imap.logout()
                    return found

            # Tier 2: everything else, same deterministic extraction —
            # content-extractable even though the sender didn't domain-match
            # (e.g. a third-party ESP sending domain).
            for msg in others:
                found = await self._extract_link_from_email(msg, target_domain)
                if found:
                    await imap.logout()
                    return found

            await imap.logout()
            return None

        except ImportError:
            logger.error("aioimaplib is required for IMAP polling")
            return None
        except Exception as exc:
            logger.error("IMAP check failed: %s", exc)
            return None

    async def _ai_classify_and_extract_latest_unread(self, target_domain="") -> Optional[str]:
        """Fetch the single latest UNSEEN message (any sender domain, still
        respecting the date-after-submission filter), ask the LLM whether it
        looks like the registration/verification email for this signup, and
        if so, run the existing deterministic extractor on it."""
        try:
            import aioimaplib

            imap_config = self.config.imap_config
            imap = (aioimaplib.IMAP4_SSL(imap_config.host, imap_config.port)
                    if imap_config.use_ssl else
                    aioimaplib.IMAP4(imap_config.host, imap_config.port))
            await imap.wait_hello_from_server()
            await imap.login(imap_config.username, imap_config.password)
            await imap.select(imap_config.mailbox)

            result, messages = await imap.search("UNSEEN")
            count = (
                len(messages[0].split())
                if (result == "OK" and messages and messages[0])
                else 0
            )
            logger.info("IMAP poll (Tier 3): %d UNSEEN message(s) in %s", count, imap_config.mailbox)
            if count == 0:
                await imap.logout()
                return None

            message_ids = [
                mid.decode() if isinstance(mid, bytes) else mid
                for mid in messages[0].split()
            ]
            if not message_ids:
                await imap.logout()
                return None
            latest_id = message_ids[-1]

            result, msg_data = await imap.fetch(latest_id, "(BODY.PEEK[])")
            await imap.logout()
            if result != "OK" or not msg_data or not msg_data[0]:
                logger.warning(
                    "IMAP fetch failed for latest message %s (result=%s) "
                    "during Tier 3 classification — skipping",
                    latest_id, result,
                )
                return None

            raw_email = msg_data[1]
            if isinstance(raw_email, tuple):
                raw_email = raw_email[1]
            msg = email.message_from_bytes(raw_email)

            date_str = msg.get("Date", "")
            if date_str and self._signup_submitted_at > 0:
                try:
                    from email.utils import parsedate_to_datetime
                    msg_date = parsedate_to_datetime(date_str)
                    if msg_date.timestamp() < self._signup_submitted_at:
                        logger.debug(
                            "Latest unread mail predates signup submission "
                            "— skipping AI fallback"
                        )
                        return None
                except Exception:
                    pass

            body_text = self._get_email_body_text(msg)
            logger.info(
                "Tier 3 candidate — from=%s subject=%r body_preview=%r",
                msg.get("From", ""),
                msg.get("Subject", ""),
                body_text[:3000].replace("\n", " "),
            )
            is_registration_email = await self._ai_judge_is_registration_email(
                sender=msg.get("From", ""),
                subject=msg.get("Subject", ""),
                body_text=body_text,
                hostname=target_domain,
            )
            logger.info(
                "AI judge verdict on latest unread mail: %s",
                "REGISTRATION EMAIL" if is_registration_email else "not a registration email",
            )
            if not is_registration_email:
                self._tier3_ai_judge_rejected = True
                return None

            logger.info(
                "AI classified latest unread mail (from %s) as likely the "
                "registration email", msg.get("From", ""),
            )
            return await _ai_extract_confirmation_action(
                body_text=body_text,
                target_domain=target_domain,
                page_expects_code=getattr(self, "_page_expects_code", False),
                llm_provider=self.config.llm_provider,
                llm_api_key=self.config.llm_api_key,
                llm_model=self.config.llm_model,
                llm_base_url=self.config.llm_base_url or None,
            )

        except ImportError:
            logger.error("aioimaplib is required for IMAP polling")
            return None
        except Exception as exc:
            logger.error("AI-fallback IMAP check failed: %s", exc)
            return None

    async def _ai_judge_is_registration_email(
        self, sender: str, subject: str, body_text: str, hostname: str
    ) -> bool:
        """Bounded, single classification call — delegates to the module-level
        implementation so the disposable-inbox path can also reuse it."""
        return await _ai_judge_is_registration_email_text(
            sender=sender,
            subject=subject,
            body_text=body_text,
            hostname=hostname,
            llm_provider=self.config.llm_provider,
            llm_api_key=self.config.llm_api_key,
            llm_model=self.config.llm_model,
            llm_base_url=self.config.llm_base_url or None,
        )

    def _get_email_body_text(self, msg) -> str:
        """Extract plain-text body from a parsed email.message.Message,
        handling multipart and non-multipart cases. Factored out of
        _extract_link_from_email so other code (the Tier 3 AI classifier)
        can get the raw body without also running link extraction."""
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
        return body_text

    async def _extract_link_from_email(self, msg, target_domain=""):
        """Extract a confirmation action from *msg* using a single AI call
        that decides code-vs-link and returns the value — no regex fallback.

        Returns ``("link", url)``, ``("code", code)``, or ``None``.
        """
        body_text = self._get_email_body_text(msg)
        return await _ai_extract_confirmation_action(
            body_text=body_text,
            target_domain=target_domain,
            page_expects_code=getattr(self, "_page_expects_code", False),
            llm_provider=self.config.llm_provider,
            llm_api_key=self.config.llm_api_key,
            llm_model=self.config.llm_model,
            llm_base_url=self.config.llm_base_url or None,
        )

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
