"""Shared form-filling and CAPTCHA detection helpers used by both
RegistrationHandler and LoginHandler."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# Common CAPTCHA detection patterns
CAPTCHA_PATTERNS = [
    ("recaptcha", "iframe[src*='recaptcha'], div.g-recaptcha, div[data-sitekey]"),
    ("hcaptcha", "iframe[src*='hcaptcha'], div.h-captcha"),
    ("cf-turnstile", "div.cf-turnstile, iframe[src*='challenges.cloudflare.com']"),
    ("generic", "img[src*='captcha'], input[name*='captcha'], div[id*='captcha']"),
]


def _escape_css_string(value: str) -> str:
    """Escape special characters in a string for safe use in CSS attribute selectors."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


async def fill_form_fields(
    page: Page,
    field_mappings: list[tuple[list[str], str]],
) -> None:
    """Heuristically fill form fields on *page* using name/id/placeholder patterns.

    *field_mappings* is a list of (field_names, value) tuples where field_names
    is a list of possible name/id/placeholder values to try.
    """
    for names, value in field_mappings:
        for name in names:
            try:
                escaped = _escape_css_string(name)
                field = await page.query_selector(
                    f"input[name='{escaped}'], input[id='{escaped}'], "
                    f"input[placeholder*='{_escape_css_string(name.replace('_', ' '))}' i]"
                )
                if field and await field.is_visible():
                    await field.fill(value)
                    logger.debug("Filled field '%s'", name)
                    break
            except Exception:
                continue


async def submit_form(page: Page, extra_selectors: Optional[list[str]] = None) -> None:
    """Find and submit a form on *page*.

    *extra_selectors* are additional button text selectors to try before the fallback.
    """
    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "form button",
    ]
    if extra_selectors:
        submit_selectors = extra_selectors + submit_selectors

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

    # Fallback: press Enter
    try:
        await page.keyboard.press("Enter")
        await asyncio.sleep(2)
    except Exception:
        pass


async def check_captcha(
    page: Page,
    stage: str,
    screenshot_dir: Path,
    label: str = "page",
) -> None:
    """Check *page* for CAPTCHA elements. Raises CaptchaDetected if found.

    *screenshot_dir* is where the screenshot is saved.
    *label* is used in the screenshot filename and error message.
    """
    from ai_browser.registration_handler.models import CaptchaDetected

    for captcha_type, selector in CAPTCHA_PATTERNS:
        try:
            element = await page.query_selector(selector)
            if element and await element.is_visible():
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                safe_label = label.replace("://", "_").replace("/", "_")[:60]
                screenshot_path = (
                    screenshot_dir / f"captcha_{safe_label}_{stage}_{timestamp}.png"
                )
                await page.screenshot(path=str(screenshot_path), full_page=True)

                exc = CaptchaDetected(
                    page_url=page.url,
                    captcha_type=captcha_type,
                    screenshot_path=screenshot_path,
                )
                logger.warning("CAPTCHA detected: %s at %s", captcha_type, page.url)
                raise exc
        except CaptchaDetected:
            raise
        except Exception:
            continue
