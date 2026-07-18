"""AgentExplorer — LLM-driven exploration of JS-rendered SPAs via accessibility tree snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable

import httpx
from playwright.async_api import Page

from ai_browser.browser_session import BrowserSession
from ai_browser._scope import page_url_matches_scope

from .models import (
    ActionType,
    AgentAction,
    AuditLogEntry,
    ExplorerConfig,
)

logger = logging.getLogger(__name__)


def _escape_css_string(value: str) -> str:
    """Escape special characters in a string for safe use in CSS attribute selectors."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


# System prompt instructing Claude how to decide actions from an accessibility tree
EXPLORER_SYSTEM_PROMPT = """You are a web exploration agent. Your goal is to discover all navigable
pages and interactive elements on a website by interacting with it step by step.

You are given an accessibility tree snapshot of the current page. Your task is to choose the
single best next action to explore the application:

Available actions:
- click: Click on an interactive element (link, button, menu item). Provide the element's
  "name" (visible text) or "role" from the accessibility tree as the target.
- fill: Type text into an input field. Provide the field's "name" or "placeholder" plus the
  value to type.
- submit: Submit the current form.
- navigate: Navigate to a specific URL discovered on the page.
- scroll: Scroll down if there is more content below the visible viewport.
- wait: Wait for the page to load if it seems to be loading content dynamically.

Guidelines:
1. Prefer exploring new links and navigation items you haven't visited yet.
2. Avoid filling forms unless the page is clearly a search or login form that likely leads
   to more content.
3. If the page looks like a SPA with no traditional links, look for buttons, tabs, and
   menu items that might reveal more content.
4. If you've exhausted all visible interactive elements, respond with "done" to end exploration.

CRITICAL SAFETY RULES — NEVER perform these actions:
- Do NOT click/submit anything related to: delete account, cancel subscription, confirm
  purchase, pay now, checkout, remove all, wipe, destroy, permanently delete.
- If you see any text matching these patterns, skip that element entirely.

Respond in this exact JSON format (no markdown, no extra text):
{"action": "<action_type>", "target": "<element_name_or_url>", "value": "<fill_value>", "reasoning": "<why this action>"}

For "done": {"action": "done", "reasoning": "<summary of what was explored>"}"""

ACTION_SYSTEM_PROMPT = EXPLORER_SYSTEM_PROMPT  # alias for readability

MAX_ACCESSIBILITY_DEPTH = 5  # how deep to traverse the accessibility tree for the snapshot


class AgentExplorer:
    """Uses Claude API to decide the next interaction on JS-heavy pages.

    Takes Playwright accessibility tree snapshots (not screenshots) as input,
    enforcing a strict action denylist and hostname scope guard.

    Every action is logged to a per-session audit file.
    """

    def __init__(self, config: ExplorerConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)
        self._audit_entries: list[AuditLogEntry] = []
        self._paused: bool = False
        self._confirmation_callback: Optional[Callable[[AgentAction], Awaitable[bool]]] = None

    # ------------------------------------------------------------------
    # Main exploration loop
    # ------------------------------------------------------------------

    async def explore(self, session: BrowserSession, start_page: Page) -> list[AuditLogEntry]:
        """Run the exploration loop on a starting page until exhaustion or max actions.

        Args:
            session: An active BrowserSession with scope guard active.
            start_page: The Playwright Page to begin exploring from.

        Returns:
            List of all AuditLogEntry records for this session.
        """
        self._audit_entries = []
        current_page = start_page
        actions_taken = 0

        logger.info("Starting agent exploration on %s (max %d actions)",
                     current_page.url, self.config.max_actions)

        while actions_taken < self.config.max_actions:
            # Take accessibility snapshot
            snapshot = await self._capture_accessibility_tree(current_page)
            if snapshot is None:
                logger.warning("No accessibility tree available, ending exploration.")
                break

            current_url = current_page.url

            # Ask LLM what to do
            action = await self._ask_llm(snapshot, current_url)

            if action is None or action.get("action") == "done":
                logger.info("Claude signaled exploration complete after %d actions", actions_taken)
                break

            # Check denylist
            if self._is_denied(action):
                logger.warning("Action blocked by denylist: %s on '%s'",
                               action.get("action"), action.get("target"))
                continue

            # Check for registration forms (separate from destructive denylist):
            # - If allow_registration=False → treat as needs-confirmation
            # - If allow_registration=True and config present → delegate to handler
            is_registration = self._matches_registration(action)
            if is_registration:
                if not self.config.allow_registration:
                    logger.info("Registration form detected but allow_registration=False")
                    if self._needs_confirmation(action):
                        approved = await self._request_confirmation(action)
                        if not approved:
                            logger.info("Registration action denied (no callback)")
                            continue
                elif self.config.registration_config:
                    logger.info("Registration form detected; delegating to RegistrationHandler")
                    try:
                        new_page = await self._delegate_registration(session, current_page)
                        current_page = new_page
                        actions_taken += 1
                    except Exception as exc:
                        logger.error("Registration delegation failed: %s", exc)
                        self._audit_entries.append(AuditLogEntry(
                            action=AgentAction(
                                action_type=ActionType.CLICK,
                                target_text="[registration delegation]",
                                current_url=current_page.url,
                                reasoning="Delegated to RegistrationHandler",
                            ),
                            success=False,
                            error_message=str(exc),
                        ))
                        if "CaptchaDetected" in type(exc).__name__:
                            raise
                    continue

            # Check if we need human confirmation (for borderline cases)
            if self._needs_confirmation(action):
                approved = await self._request_confirmation(action)
                if not approved:
                    logger.info("Human denied action: %s", action)
                    continue

            # Execute the action
            entry = await self._execute_action(session, current_page, action)
            self._audit_entries.append(entry)
            actions_taken += 1

            # Persist audit log after each action
            self._flush_audit_log()

            # Check if a new page was opened
            pages = session.pages
            if len(pages) > 1:
                # Switch to the most recently opened page
                current_page = pages[-1]

            # Delay between actions
            await asyncio.sleep(self.config.action_delay_ms / 1000.0)

        logger.info("Agent exploration finished: %d actions taken", actions_taken)
        self._flush_audit_log()
        return self._audit_entries

    # ------------------------------------------------------------------
    # Accessibility tree capture
    # ------------------------------------------------------------------

    async def _capture_accessibility_tree(self, page: Page) -> Optional[dict]:
        """Capture a depth-limited accessibility tree snapshot from the page.

        Returns a simplified dict representation, or None if unavailable.
        """
        try:
            snapshot = await page.accessibility.snapshot()
            if snapshot is None:
                return None
            return self._simplify_snapshot(snapshot, depth=0)
        except Exception as exc:
            logger.warning("Failed to capture accessibility tree: %s", exc)
            return None

    def _simplify_snapshot(self, node: dict, depth: int) -> Optional[dict]:
        """Recursively prune the accessibility snapshot to a manageable depth."""
        if depth > MAX_ACCESSIBILITY_DEPTH:
            return {"role": node.get("role", "unknown"), "name": node.get("name", ""),
                    "_truncated": True}

        simplified: dict = {
            "role": node.get("role", ""),
            "name": node.get("name", ""),
        }
        if "value" in node:
            simplified["value"] = node["value"]
        if "checked" in node:
            simplified["checked"] = node["checked"]
        if "disabled" in node:
            simplified["disabled"] = node["disabled"]
        if "expanded" in node:
            simplified["expanded"] = node["expanded"]
        if "selected" in node:
            simplified["selected"] = node["selected"]
        if "level" in node:
            simplified["level"] = node["level"]

        children = node.get("children", [])
        if children:
            pruned = []
            for child in children:
                simplified_child = self._simplify_snapshot(child, depth + 1)
                if simplified_child:
                    pruned.append(simplified_child)
            if pruned:
                simplified["children"] = pruned

        return simplified

    # ------------------------------------------------------------------
    # LLM interaction (multi-provider via httpx)
    # ------------------------------------------------------------------

    async def _ask_llm(self, snapshot: dict, current_url: str) -> Optional[dict]:
        """Send the accessibility tree to the configured LLM provider."""
        snapshot_json = json.dumps(snapshot, indent=2)
        if len(snapshot_json) > 100_000:
            snapshot_json = snapshot_json[:100_000] + "\n... (truncated)"

        message = (
            f"Current URL: {current_url}\n\n"
            f"Accessibility tree snapshot:\n{snapshot_json}\n\n"
            "What is the next action to explore this application?"
        )

        provider = self.config.llm_provider.lower()
        api_key = self.config.llm_api_key
        model = self.config.llm_model
        base_url = self.config.llm_base_url

        try:
            if provider == "anthropic":
                resp = await self._call_anthropic(api_key, model, base_url, message)
            elif provider in ("openai", "deepseek"):
                resp = await self._call_openai_compatible(provider, api_key, model, base_url, message)
            else:
                logger.error("Unknown LLM provider: %s", provider)
                return None
            return self._parse_llm_response(provider, resp)
        except Exception as exc:
            logger.error("LLM API error (%s): %s", provider, exc)
            return None

    async def _call_anthropic(self, api_key, model, base_url, message):
        url = base_url or "https://api.anthropic.com"
        url = url.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 512,
            "system": ACTION_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": message}],
        }
        return await self._client.post(url, json=body, headers=headers)

    async def _call_openai_compatible(self, provider, api_key, model, base_url, message):
        if base_url:
            url = base_url.rstrip("/") + "/chat/completions"
        elif provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
        else:
            url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": ACTION_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
        }
        return await self._client.post(url, json=body, headers=headers)

    def _parse_llm_response(self, provider, response) -> Optional[dict]:
        """Extract action JSON from provider-specific API response."""
        data = response.json()
        if provider == "anthropic":
            content = data.get("content", [])
            text = content[0]["text"].strip() if content else ""
        else:
            choices = data.get("choices", [])
            text = choices[0].get("message", {}).get("content", "").strip() if choices else ""

        json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
            logger.debug("LLM (%s) response: %s", provider, parsed)
            return parsed
        logger.warning("Could not parse JSON from %s response: %s", provider, text[:200])
        return None

    # Deprecated backward-compat alias
    async def _ask_claude(self, snapshot, current_url):
        import warnings
        warnings.warn("_ask_claude is deprecated; use _ask_llm", DeprecationWarning, stacklevel=2)
        return await self._ask_llm(snapshot, current_url)

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _execute_action(
        self, session: BrowserSession, page: Page, action_raw: dict
    ) -> AuditLogEntry:
        """Execute the parsed action on the page and return an audit entry.

        Defense-in-depth: verifies the page's hostname against authorized_hostname
        before dispatching any click/fill/submit, independent of BrowserSession's guard.
        """
        # Independent scope verification — defense in depth
        self._verify_scope(page)

        action_type = action_raw.get("action", "")
        target = action_raw.get("target", "")
        value = action_raw.get("value", "")
        reasoning = action_raw.get("reasoning", "")

        agent_action = AgentAction(
            action_type=ActionType(action_type) if action_type in ActionType.__members__ else ActionType.WAIT,
            target_text=target,
            input_value=value,
            current_url=page.url,
            reasoning=reasoning,
        )

        entry = AuditLogEntry(action=agent_action)

        try:
            if action_type == "click":
                entry.success = await self._do_click(page, target)
            elif action_type == "fill":
                entry.success = await self._do_fill(page, target, value)
            elif action_type == "submit":
                entry.success = await self._do_submit(page, target)
            elif action_type == "navigate":
                entry.success = await self._do_navigate(session, page, target)
            elif action_type == "scroll":
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(0.5)
                entry.success = True
            elif action_type == "wait":
                await asyncio.sleep(2)
                entry.success = True
            else:
                logger.warning("Unknown action type: %s", action_type)
                entry.success = False
                entry.error_message = f"Unknown action type: {action_type}"
        except Exception as exc:
            entry.success = False
            entry.error_message = str(exc)
            logger.error("Action execution failed: %s", exc)

        return entry

    async def _do_click(self, page: Page, target: str) -> bool:
        """Try to click an element by accessible name, text content, or role.

        Before clicking, the *actual* resolved element text is checked against
        the denylist — even if the LLM's self-report already passed _is_denied().
        This prevents prompt-injection or model-paraphrasing bypasses.
        """
        escaped = _escape_css_string(target)
        selectors = [
            f"text={escaped}",
            f"[aria-label='{escaped}']",
            f"button:has-text('{escaped}')",
            f"a:has-text('{escaped}')",
            f"[role='{escaped}']",
        ]
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    # Defense-in-depth: check actual element text before clicking
                    if await self._element_matches_denylist(element):
                        logger.warning(
                            "Click blocked: element text matches denylist (selector=%s)",
                            selector,
                        )
                        return False
                    # Also check registration: if allow_registration is True, the
                    # registration delegation path handles this — don't click here
                    if await self._element_matches_registration(element):
                        if self.config.allow_registration:
                            logger.info("Skipping click on registration element; handled by delegation")
                            return False
                    await element.click()
                    await page.wait_for_load_state("networkidle", timeout=5000)
                    return True
            except Exception:
                continue
        return False

    async def _do_fill(self, page: Page, target: str, value: str) -> bool:
        """Try to fill an input field by name, placeholder, or label."""
        escaped = _escape_css_string(target)
        selectors = [
            f"input[name='{escaped}']",
            f"input[placeholder*='{escaped}']",
            f"input[aria-label='{escaped}']",
            f"[aria-label='{escaped}']",
        ]
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    await element.fill(value)
                    return True
            except Exception:
                continue

        # Fallback: try to find a label with matching text and fill the associated input
        try:
            label = await page.query_selector(f"label:has-text('{target}')")
            if label:
                for_id = await label.get_attribute("for")
                if for_id:
                    input_el = await page.query_selector(f"#{for_id}")
                    if input_el:
                        await input_el.fill(value)
                        return True
        except Exception:
            pass

        return False

    async def _do_submit(self, page: Page, target: str) -> bool:
        """Try to submit a form.

        Before submitting, the submit button's actual text is checked against
        the denylist for defense-in-depth — even if the LLM's self-report passed.
        """
        if target:
            form = await page.query_selector(target)
            if form:
                # Check the form element or its submit button for destructive text
                submit_btn = await page.query_selector(
                    f"{target} button[type='submit'], {target} input[type='submit']"
                )
                btn_to_check = submit_btn or form
                if await self._element_matches_denylist(btn_to_check):
                    logger.warning("Submit blocked: form element text matches denylist")
                    return False
                await form.evaluate("el => el.submit()")
                await page.wait_for_load_state("networkidle", timeout=5000)
                return True

        # Try pressing Enter on the active element
        try:
            # Check focused element before pressing Enter
            focused = await page.evaluate(
                "() => document.activeElement?.innerText || ''"
            )
            if focused:
                for pattern in self.config.destructive_patterns:
                    if re.search(pattern, focused):
                        logger.warning(
                            "Submit blocked: focused element text matches denylist"
                        )
                        return False
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=5000)
            return True
        except Exception:
            pass

        # Generic form submit — check submit button text first
        try:
            submit_btn = await page.query_selector(
                "button[type='submit'], input[type='submit']"
            )
            if submit_btn and await self._element_matches_denylist(submit_btn):
                logger.warning("Submit blocked: submit button text matches denylist")
                return False
            form = await page.query_selector("form")
            if form:
                await form.evaluate("el => el.submit()")
                await page.wait_for_load_state("networkidle", timeout=5000)
                return True
        except Exception:
            pass

        return False

    async def _do_navigate(self, session: BrowserSession, page: Page, url: str) -> bool:
        """Navigate to a URL, via the scope-guarded session."""
        try:
            await page.goto(url, timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            return True
        except Exception as exc:
            logger.warning("Navigation to %s failed: %s", url, exc)
            return False

    async def _delegate_registration(self, session: BrowserSession, current_page: Page) -> Page:
        """Hand off to RegistrationHandler for the full signup + email confirmation flow.

        The agent's role is to recognize this is a signup point and delegate.
        RegistrationHandler already knows how to fill fields, submit, handle CAPTCHA,
        and poll IMAP for confirmation links.

        Returns the authenticated page after successful registration.
        Propagates CaptchaDetected unchanged so the caller can handle it.
        """
        from ai_browser.registration_handler import RegistrationHandler

        if not self.config.registration_config:
            raise RuntimeError("allow_registration is True but no registration_config set")

        logger.info("Delegating registration flow to RegistrationHandler")
        handler = RegistrationHandler(self.config.registration_config)

        try:
            page = await handler.register(session)
            logger.info("Registration delegation completed: %s", page.url)
            return page
        except Exception:
            # CaptchaDetected (and any other exception) propagates up to explore()
            raise

    # ------------------------------------------------------------------
    # Denylist enforcement
    # ------------------------------------------------------------------

    def _is_denied(self, action: dict) -> bool:
        """Check if the proposed action matches any destructive patterns.

        This checks the LLM's self-reported target/value/reasoning text.
        For defense-in-depth, _do_click and _do_submit also verify the
        *actual* resolved DOM element text before acting.
        """
        target = (action.get("target") or "").lower()
        value = (action.get("value") or "").lower()
        reasoning = (action.get("reasoning") or "").lower()
        combined = f"{target} {value} {reasoning}"

        for pattern in self.config.destructive_patterns:
            if re.search(pattern, combined):
                logger.warning(
                    "Denylist match: pattern '%s' matched in '%s'",
                    pattern,
                    combined[:100],
                )
                return True
        return False

    def _matches_registration(self, action: dict) -> bool:
        """Check if the LLM's self-reported text matches registration patterns.

        The *actual* resolved element text is also checked at click/submit time
        via _element_matches_registration() for defense-in-depth.
        """
        target = (action.get("target") or "").lower()
        value = (action.get("value") or "").lower()
        reasoning = (action.get("reasoning") or "").lower()
        combined = f"{target} {value} {reasoning}"

        for pattern in self.config.registration_patterns:
            if re.search(pattern, combined):
                return True
        return False

    async def _element_matches_registration(self, element) -> bool:
        """Check the *actual* resolved DOM element text against registration patterns.

        Same approach as _element_matches_denylist but against registration_patterns.
        """
        try:
            inner_text = (await element.inner_text() or "").lower()
            aria_label = (await element.get_attribute("aria-label") or "").lower()
            combined = f"{inner_text} {aria_label}"

            for pattern in self.config.registration_patterns:
                if re.search(pattern, combined):
                    return True
        except Exception as exc:
            logger.debug("Failed to check element registration: %s", exc)
        return False

    async def _element_matches_denylist(self, element) -> bool:
        """Check the *actual* resolved DOM element text against destructive patterns.

        This is the runtime safety check — it inspects the real element's
        visible text and aria-label, regardless of what the LLM reported.

        Returns True if the element should be blocked (matches a destructive pattern).
        """
        try:
            inner_text = (await element.inner_text() or "").lower()
            aria_label = (await element.get_attribute("aria-label") or "").lower()
            combined = f"{inner_text} {aria_label}"

            for pattern in self.config.destructive_patterns:
                if re.search(pattern, combined):
                    logger.warning(
                        "Denylist ELEMENT match: pattern '%s' matched in element '%s'",
                        pattern,
                        combined[:100],
                    )
                    return True
        except Exception as exc:
            logger.debug("Failed to check element denylist: %s", exc)
        return False

    def _needs_confirmation(self, action: dict) -> bool:
        """Determine if the action falls into a borderline category needing human approval.

        Default behavior (fail-closed): if allow_unattended is False and the action
        matches a borderline pattern, confirmation is required. If no callback is
        configured, the action will be denied by _request_confirmation().

        Borderline patterns: save, confirm, update, submit — actions that modify
        state but aren't overtly destructive.
        """
        borderline = [r"(?i)\bsave\b", r"(?i)\bconfirm\b", r"(?i)\bupdate\b", r"(?i)\bsubmit\b"]
        target = (action.get("target") or "").lower()

        for pattern in borderline:
            if re.search(pattern, target):
                # If unattended mode is explicitly enabled, skip confirmation
                if self.config.allow_unattended:
                    return False
                return True
        return False

    async def _request_confirmation(self, action: dict) -> bool:
        """Request human confirmation for a potentially sensitive action.

        Fail-closed default: if no confirmation callback is configured, deny the
        action. Callers must either set allow_unattended=True or provide a callback.
        """
        if self._confirmation_callback:
            agent_action = AgentAction(
                action_type=ActionType(action.get("action", "click")),
                target_text=action.get("target", ""),
                current_url="",
                reasoning=action.get("reasoning", ""),
            )
            return await self._confirmation_callback(agent_action)
        # Fail-closed: no callback means deny
        logger.warning(
            "Action '%s' on '%s' denied: no confirmation callback configured "
            "and allow_unattended is False.",
            action.get("action"),
            action.get("target"),
        )
        return False

    def set_confirmation_callback(self, callback: Callable[[AgentAction], Awaitable[bool]]) -> None:
        """Set a callback that is invoked when an action needs human confirmation.

        The callback receives the proposed AgentAction and should return True to
        proceed or False to skip.
        """
        self._confirmation_callback = callback

    def _verify_scope(self, page: Page) -> None:
        """Verify the current page's hostname is within the authorized scope.

        Independent defense-in-depth check — separate from BrowserSession's route-level
        guard. Uses glob-pattern matching so ``*.example.com`` matches subdomains.

        Raises:
            ScopeError: If the page URL's hostname does not match the authorized scope.
        """
        from ai_browser._scope import ScopeError

        if not page_url_matches_scope(page.url, self.config.authorized_hostname):
            raise ScopeError(
                f"AgentExplorer scope violation: page at '{page.url}' "
                f"is outside authorized scope '{self.config.authorized_hostname}'"
            )

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def _flush_audit_log(self) -> None:
        """Write the current audit log to disk as newline-delimited JSON."""
        if not self._audit_entries:
            return

        self.config.audit_log_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        hostname = self.config.authorized_hostname.replace(":", "_").replace("/", "_")
        log_file = self.config.audit_log_path / f"{hostname}_{timestamp}.jsonl"

        with open(log_file, "w") as f:
            for entry in self._audit_entries:
                f.write(entry.model_dump_json() + "\n")

        logger.debug("Audit log written to %s (%d entries)", log_file, len(self._audit_entries))

    @property
    def audit_log(self) -> list[AuditLogEntry]:
        return list(self._audit_entries)
