"""AgentExplorer — Claude-driven exploration of JS-rendered SPAs via accessibility tree snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable

import anthropic
from playwright.async_api import Page

from ai_browser.browser_session import BrowserSession

from .models import (
    ActionType,
    AgentAction,
    AuditLogEntry,
    ExplorerConfig,
)

logger = logging.getLogger(__name__)

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
        self._client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
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

            # Ask Claude what to do
            action = await self._ask_claude(snapshot, current_url)

            if action is None or action.get("action") == "done":
                logger.info("Claude signaled exploration complete after %d actions", actions_taken)
                break

            # Check denylist
            if self._is_denied(action):
                logger.warning("Action blocked by denylist: %s on '%s'",
                               action.get("action"), action.get("target"))
                # Skip this action but continue exploring other elements
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
    # Claude interaction
    # ------------------------------------------------------------------

    async def _ask_claude(self, snapshot: dict, current_url: str) -> Optional[dict]:
        """Send the accessibility tree to Claude and parse the action response."""
        snapshot_json = json.dumps(snapshot, indent=2)
        # Truncate if too large (Claude context limits)
        if len(snapshot_json) > 100_000:
            snapshot_json = snapshot_json[:100_000] + "\n... (truncated)"

        message = (
            f"Current URL: {current_url}\n\n"
            f"Accessibility tree snapshot:\n{snapshot_json}\n\n"
            "What is the next action to explore this application?"
        )

        try:
            response = await self._client.messages.create(
                model=self.config.anthropic_model,
                max_tokens=512,
                system=ACTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": message}],
            )
            text = response.content[0].text.strip()
            # Extract JSON from response (Claude sometimes wraps in markdown)
            json_match = re.search(r"\{[^}]+\}", text, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                logger.debug("Claude response: %s", parsed)
                return parsed
            logger.warning("Could not parse JSON from Claude response: %s", text[:200])
            return None
        except Exception as exc:
            logger.error("Claude API error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _execute_action(
        self, session: BrowserSession, page: Page, action_raw: dict
    ) -> AuditLogEntry:
        """Execute the parsed action on the page and return an audit entry."""
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
        """Try to click an element by accessible name, text content, or role."""
        selectors = [
            f"text={target}",
            f"[aria-label='{target}']",
            f"button:has-text('{target}')",
            f"a:has-text('{target}')",
            f"[role='{target}']",
        ]
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element and await element.is_visible():
                    await element.click()
                    await page.wait_for_load_state("networkidle", timeout=5000)
                    return True
            except Exception:
                continue
        return False

    async def _do_fill(self, page: Page, target: str, value: str) -> bool:
        """Try to fill an input field by name, placeholder, or label."""
        selectors = [
            f"input[name='{target}']",
            f"input[placeholder*='{target}']",
            f"input[aria-label='{target}']",
            f"[aria-label='{target}']",
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
        """Try to submit a form."""
        if target:
            form = await page.query_selector(target)
            if form:
                await form.evaluate("el => el.submit()")
                await page.wait_for_load_state("networkidle", timeout=5000)
                return True

        # Try pressing Enter on the active element
        try:
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=5000)
            return True
        except Exception:
            pass

        # Generic form submit
        try:
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

    # ------------------------------------------------------------------
    # Denylist enforcement
    # ------------------------------------------------------------------

    def _is_denied(self, action: dict) -> bool:
        """Check if the proposed action matches any destructive patterns."""
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

    def _needs_confirmation(self, action: dict) -> bool:
        """Determine if the action falls into a borderline category needing human approval.

        Currently, all actions pass through if they clear the denylist. Override
        this method or set a confirmation callback for stricter policies.
        """
        if self._confirmation_callback is None:
            return False

        # Check for borderline patterns: "save", "confirm", "update settings"
        borderline = [r"(?i)\bsave\b", r"(?i)\bconfirm\b", r"(?i)\bupdate\b", r"(?i)\bsubmit\b"]
        target = (action.get("target") or "").lower()
        for pattern in borderline:
            if re.search(pattern, target):
                return True
        return False

    async def _request_confirmation(self, action: dict) -> bool:
        """Request human confirmation for a potentially sensitive action."""
        if self._confirmation_callback:
            agent_action = AgentAction(
                action_type=ActionType(action.get("action", "click")),
                target_text=action.get("target", ""),
                current_url="",
                reasoning=action.get("reasoning", ""),
            )
            return await self._confirmation_callback(agent_action)
        return True

    def set_confirmation_callback(self, callback: Callable[[AgentAction], Awaitable[bool]]) -> None:
        """Set a callback that is invoked when an action needs human confirmation.

        The callback receives the proposed AgentAction and should return True to
        proceed or False to skip.
        """
        self._confirmation_callback = callback

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
