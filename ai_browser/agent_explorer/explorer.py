"""AgentExplorer — LLM-driven exploration of JS-rendered SPAs via accessibility tree snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable

import httpx
from playwright.async_api import Page

from ai_browser.browser_session import BrowserSession
from ai_browser._scope import page_url_matches_any_scope, display_scope
from ai_browser.registration_handler.models import CaptchaDetected
from ai_browser._llm_client import _call_anthropic as _shared_call_anthropic
from ai_browser._llm_client import _call_openai_compatible as _shared_call_openai
from ai_browser._llm_client import DEFAULT_MAX_TOKENS

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


# System prompt instructing the LLM how to explore a website step by step.
# The model receives a multi-turn conversation: it sees its own past actions,
# their outcomes (success/failure, navigation), and the current page snapshot.
EXPLORER_SYSTEM_PROMPT = """You are a web exploration agent. Your goal is to discover all navigable
pages and interactive elements on a website by interacting with it step by step.

You are part of a multi-turn conversation. You will see your own past actions and their
outcomes (whether they succeeded or failed, and what URL you ended up on). Use this memory
to make smarter decisions — do NOT repeat an action that already failed or led nowhere
useful; use what you've learned to explore new areas you haven't visited yet.

On each turn you are given an accessibility tree snapshot of the current page. Your task
is to choose the single best next action to explore the application:

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

Use the ``take_action`` tool to respond on every turn, filling in:
- ``action``: one of click / fill / submit / navigate / scroll / wait / done
- ``target``: the element name, label, or URL to act on (omit if not applicable)
- ``value``: the fill value (only for the "fill" action; omit otherwise)
- ``reasoning``: a brief explanation of why this action was chosen

For "done": set ``action`` to "done" and provide a summary in ``reasoning``."""

ACTION_SYSTEM_PROMPT = EXPLORER_SYSTEM_PROMPT  # alias for readability

MAX_ACCESSIBILITY_YAML_CHARS = 8000  # max chars for aria_snapshot() YAML before truncation

# max_tokens fallback uses the shared DEFAULT_MAX_TOKENS from _llm_client.py

# Native tool definition for structured output.
# Anthropic uses "input_schema"; OpenAI-compatible providers use "parameters".
_ACTION_TOOL_ANTHROPIC = {
    "name": "take_action",
    "description": "Choose the next action to take while exploring the web application.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["click", "fill", "submit", "navigate", "scroll", "wait", "done"],
            },
            "target": {
                "type": "string",
                "description": "Element name, label, or URL to act on.",
            },
            "value": {
                "type": "string",
                "description": "Value to fill (only for the 'fill' action).",
            },
            "reasoning": {
                "type": "string",
                "description": "Why this action was chosen.",
            },
        },
        "required": ["action", "reasoning"],
    },
}

_ACTION_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "take_action",
        "description": "Choose the next action to take while exploring the web application.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "fill", "submit", "navigate", "scroll", "wait", "done"],
                },
                "target": {
                    "type": "string",
                    "description": "Element name, label, or URL to act on.",
                },
                "value": {
                    "type": "string",
                    "description": "Value to fill (only for the 'fill' action).",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why this action was chosen.",
                },
            },
            "required": ["action", "reasoning"],
        },
    },
}


# ---------------------------------------------------------------------------
# ARIA snapshot distillation — parse, filter, re-serialize
# ---------------------------------------------------------------------------

@dataclass
class _AriaNode:
    """A single node in a Playwright aria_snapshot() YAML tree."""
    role: str
    name: str = ""
    attrs: dict = field(default_factory=dict)
    text: str = ""
    children: list = field(default_factory=list)


_ARIA_LINE_RE = re.compile(
    r'^- (\w+)(?:\s+"([^"]*)")?(?:\s+\[([^\]]*)\])?(?::\s*(.*))?$'
)

# Roles that represent directly interactive elements (clickable / fillable).
_INTERACTIVE_ROLES: set[str] = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "menuitem", "tab", "switch", "slider", "searchbox", "spinbutton",
    "listbox", "option",
}

# Roles that are always kept: interactive + headings for orientation.
_ALWAYS_KEEP: set[str] = _INTERACTIVE_ROLES | {"heading"}


def _parse_aria_yaml(yaml_text: str) -> list[_AriaNode]:
    """Parse Playwright ``aria_snapshot()`` YAML into a tree of ``_AriaNode``.

    The format uses 2-space indentation for nesting.  Each line looks like::

        - role "name" [attr=val, ...]: text content
    """
    lines = yaml_text.split("\n")
    roots: list[_AriaNode] = []
    # Stack of (children_list, indent) — the children list of the current
    # parent at each indentation level.
    stack: list[tuple[list[_AriaNode], int]] = [(roots, -1)]

    for line in lines:
        stripped = line.rstrip()
        if not stripped.strip():
            continue

        indent = len(line) - len(line.lstrip())
        m = _ARIA_LINE_RE.match(stripped.strip())
        if not m:
            continue

        role = m.group(1)
        name = m.group(2) or ""
        attrs_str = m.group(3) or ""
        text = m.group(4) or ""

        # Parse ``[level=1, disabled]`` style attributes
        attrs: dict = {}
        if attrs_str:
            for part in attrs_str.split(","):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip()
                elif part:
                    attrs[part] = True

        node = _AriaNode(role=role, name=name, attrs=attrs, text=text)

        # Pop back to the correct parent based on indentation
        while stack[-1][1] >= indent:
            stack.pop()
        stack[-1][0].append(node)
        stack.append((node.children, indent))

    return roots


def _mark_kept_nodes(nodes: list[_AriaNode]) -> None:
    """Walk *nodes* bottom-up; set ``_keep=True`` on interactive elements,
    headings, and any node that has a kept descendant (parent chain)."""
    for node in nodes:
        _mark_kept_nodes(node.children)
        if node.role in _ALWAYS_KEEP or any(
            getattr(c, "_keep", False) for c in node.children
        ):
            node._keep = True  # type: ignore[attr-defined]


def _prune_tree(nodes: list[_AriaNode]) -> list[_AriaNode]:
    """Return a new tree containing only nodes marked ``_keep``."""
    result: list[_AriaNode] = []
    for node in nodes:
        if not getattr(node, "_keep", False):
            continue
        kept = _AriaNode(
            role=node.role,
            name=node.name,
            attrs=dict(node.attrs),
            text=node.text if not node.children else "",
        )
        kept.children = _prune_tree(node.children)
        result.append(kept)
    return result


def _count_interactive(nodes: list[_AriaNode]) -> int:
    """Count interactive elements in (filtered) tree."""
    n = 0
    for node in nodes:
        if node.role in _INTERACTIVE_ROLES:
            n += 1
        n += _count_interactive(node.children)
    return n


def _serialize_aria_tree(nodes: list[_AriaNode], indent: int = 0) -> str:
    """Serialize a filtered ARIA tree to compact YAML-like text."""
    lines: list[str] = []
    prefix = "  " * indent

    for node in nodes:
        line = f"{prefix}- {node.role}"
        if node.name:
            line += f' "{node.name}"'
        if node.attrs:
            attr_parts: list[str] = []
            for k, v in node.attrs.items():
                if v is True:
                    attr_parts.append(k)
                else:
                    attr_parts.append(f"{k}={v}")
            line += f" [{', '.join(attr_parts)}]"

        if node.children:
            line += ":"
            lines.append(line)
            lines.append(_serialize_aria_tree(node.children, indent + 1))
        elif node.text:
            line += f": {node.text}"
            lines.append(line)
        else:
            lines.append(line)

    return "\n".join(lines)


def _distill_aria_snapshot(raw_yaml: str) -> tuple[str, int, int]:
    """Parse, filter, and re-serialize an ARIA snapshot.

    Returns ``(distilled_text, raw_chars, interactive_count)``.
    """
    nodes = _parse_aria_yaml(raw_yaml)
    _mark_kept_nodes(nodes)
    filtered_nodes = _prune_tree(nodes)
    interactive_count = _count_interactive(filtered_nodes)
    distilled = _serialize_aria_tree(filtered_nodes)
    return distilled, len(raw_yaml), interactive_count


def _snapshots_near_identical(a: str, b: str, threshold: float = 0.95) -> bool:
    """Return True if two accessibility snapshots are near-identical.

    Normalizes whitespace, then computes Jaccard similarity of line-level
    hashes.  Returns True when >= *threshold* fraction of unique lines match
    between the two snapshots, which tolerates minor dynamic content (timestamps,
    counts, etc.) while still detecting that no real structural change occurred.
    """
    def _normalize(text: str) -> set[str]:
        lines = [line.strip() for line in text.splitlines()]
        return {ln for ln in lines if ln}

    set_a = _normalize(a)
    set_b = _normalize(b)

    if not set_a and not set_b:
        return True
    if not set_a or not set_b:
        return False

    intersection = set_a & set_b
    union = set_a | set_b
    similarity = len(intersection) / len(union)
    return similarity >= threshold


def _extract_json(text: str) -> Optional[dict]:
    """Extract a JSON object from `text` using a proper decoder-based parse.

    Finds the first ``{`` and delegates to ``json.JSONDecoder().raw_decode()``,
    which determines the real, syntactically-correct end of the JSON value
    (unlike a regex that stops at the first ``}`` and breaks on nested objects).
    """
    decoder = json.JSONDecoder()
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = decoder.raw_decode(text, start)
        return obj
    except json.JSONDecodeError:
        return None


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
        self._message_history: list[dict] = []  # raw per-session multi-turn history

        # Repeat-action guard state (reset per explore() call)
        self._attempted_no_effect: set[tuple[str, str]] = set()
        self._prev_snapshot: Optional[str] = None
        self._prev_url: Optional[str] = None
        self._prev_action_key: Optional[tuple[str, str]] = None
        self._consecutive_corrections: int = 0

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
        self._message_history = []  # fresh history per explore() call
        self._attempted_no_effect = set()
        self._prev_snapshot = None
        self._prev_url = None
        self._prev_action_key = None
        self._consecutive_corrections = 0
        current_page = start_page
        actions_taken = 0

        # Defensive guard: if the caller passes an about:blank page (e.g. a
        # navigation redirect chain that resolved to a blocked/empty page),
        # bail out early rather than wasting LLM calls on a blank canvas.
        if not current_page.url or current_page.url == "about:blank":
            logger.info(
                "Skipping agent exploration — start page is about:blank or has no URL"
            )
            return self._audit_entries

        logger.info("Starting agent exploration on %s (max %d actions)",
                     current_page.url, self.config.max_actions)

        while actions_taken < self.config.max_actions:
            # Take accessibility snapshot
            snapshot = await self._capture_accessibility_tree(current_page)
            if snapshot is None:
                # One-shot crash recovery: reload the page and retry once.
                # A single renderer crash doesn't mean every page is broken.
                logger.warning(
                    "Accessibility snapshot failed; attempting page reload "
                    "and single retry"
                )
                try:
                    await current_page.reload()
                    await asyncio.sleep(1)
                    snapshot = await self._capture_accessibility_tree(current_page)
                except Exception as reload_exc:
                    logger.warning(
                        "Page reload during crash recovery failed: %s", reload_exc
                    )
                if snapshot is None:
                    logger.warning(
                        "No accessibility tree available after reload, "
                        "ending exploration."
                    )
                    break
                logger.info("Snapshot recovered after page reload")

            current_url = current_page.url

            # ---- No-effect detection for the *previous* action -------------
            # Compare the snapshot we just captured (post-action-N) with the
            # snapshot saved *before* action N was executed.  If the URL
            # didn't change *and* the accessibility tree is near-identical,
            # mark that action as having no observable effect.
            if self._prev_snapshot is not None and self._prev_action_key is not None:
                url_unchanged = (current_url == self._prev_url)
                tree_unchanged = _snapshots_near_identical(
                    snapshot, self._prev_snapshot
                )
                if url_unchanged and tree_unchanged:
                    self._attempted_no_effect.add(self._prev_action_key)
                    logger.debug(
                        "Action %s on %r marked as no-effect "
                        "(URL unchanged, tree near-identical)",
                        self._prev_action_key[0], self._prev_action_key[1],
                    )

            # Build user turn: outcome of previous action (if any) + current snapshot
            user_content = self._build_user_turn(snapshot, current_url, actions_taken)
            self._message_history.append({"role": "user", "content": user_content})

            # Manage context window before sending to LLM
            managed_messages = self._manage_context()

            # Ask LLM with full multi-turn history
            action = await self._ask_llm(managed_messages)

            if action is None or action.get("action") == "done":
                logger.info("%s signaled exploration complete after %d actions", self.config.llm_provider, actions_taken)
                break

            # Record assistant response in raw history
            self._message_history.append(
                {"role": "assistant", "content": json.dumps(action)}
            )

            # Check denylist
            if self._is_denied(action):
                logger.warning("Action blocked by denylist: %s on '%s'",
                               action.get("action"), action.get("target"))
                # Record the denied action as a failed entry so the model
                # knows it was rejected and can learn from it.
                self._audit_entries.append(AuditLogEntry(
                    action=AgentAction(
                        action_type=ActionType(action.get("action", "click"))
                        if action.get("action", "") in ActionType.__members__
                        else ActionType.WAIT,
                        target_text=action.get("target", ""),
                        current_url=current_url,
                        reasoning=action.get("reasoning", ""),
                    ),
                    success=False,
                    error_message="Blocked by denylist",
                ))
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
                            self._audit_entries.append(AuditLogEntry(
                                action=AgentAction(
                                    action_type=ActionType.CLICK,
                                    target_text=action.get("target", ""),
                                    current_url=current_url,
                                    reasoning=action.get("reasoning", ""),
                                ),
                                success=False,
                                error_message="Registration action denied",
                            ))
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
                        # CaptchaDetected always propagates so the caller can
                        # solve it manually and resume. Other exceptions re-raise
                        # only when raise_on_registration_failure is True.
                        if isinstance(exc, CaptchaDetected):
                            raise
                        if self.config.raise_on_registration_failure:
                            raise
                    continue

            # Check if we need human confirmation (for borderline cases)
            if self._needs_confirmation(action):
                approved = await self._request_confirmation(action)
                if not approved:
                    logger.info("Human denied action: %s", action)
                    self._audit_entries.append(AuditLogEntry(
                        action=AgentAction(
                            action_type=ActionType(action.get("action", "click"))
                            if action.get("action", "") in ActionType.__members__
                            else ActionType.CLICK,
                            target_text=action.get("target", ""),
                            current_url=current_url,
                            reasoning=action.get("reasoning", ""),
                        ),
                        success=False,
                        error_message="Denied by human confirmation",
                    ))
                    continue

            # ---- Repeat-action guard ---------------------------------------
            # Before executing, check if this exact (action, target) was
            # already attempted and produced no observable change.  This is a
            # deterministic guard — it does not rely on the model noticing
            # the repetition through raw history.
            action_key = (action.get("action", ""), action.get("target", "") or "")
            if action_key in self._attempted_no_effect:
                self._consecutive_corrections += 1
                max_corr = self.config.max_consecutive_corrections
                logger.warning(
                    "Rejected repeat action: %s on %r "
                    "(already tried, no effect) — %d/%d consecutive corrections",
                    action_key[0], action_key[1],
                    self._consecutive_corrections, max_corr,
                )
                # Inject corrective message — same treatment as denylist
                # rejection: no increment of actions_taken, loop back.
                corrective = (
                    f"You already tried {action_key[0]} on "
                    f"'{action_key[1]}' and it had no effect. "
                    f"Choose a different, unexplored element instead."
                )
                self._message_history.append(
                    {"role": "user", "content": corrective}
                )
                if self._consecutive_corrections >= max_corr:
                    logger.warning(
                        "Giving up after %d consecutive repeat-action "
                        "corrections without progress",
                        self._consecutive_corrections,
                    )
                    break
                continue

            # Execute the action
            url_before_action = current_page.url
            entry = await self._execute_action(session, current_page, action)
            self._audit_entries.append(entry)
            actions_taken += 1

            # ---- Save state for next iteration's no-effect detection -------
            self._prev_snapshot = snapshot
            self._prev_url = url_before_action
            self._prev_action_key = action_key
            self._consecutive_corrections = 0

            # Log what the agent did this cycle
            action_type = action.get("action", "?")
            target = action.get("target", "") or action.get("value", "")
            url_after = current_page.url
            action_changed_url = (url_after != url_before_action)
            logger.info(
                "Action %d/%d: %s on %r -> %s",
                actions_taken, self.config.max_actions, action_type, target,
                url_after if action_changed_url else "(no navigation)",
            )

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

    async def _capture_accessibility_tree(self, page: Page) -> Optional[str]:
        """Capture, distill, and return the page's interactive-element summary.

        1. Fetches the raw ``aria_snapshot()`` YAML.
        2. Distills it to interactive elements + headings + parent chains.
        3. Applies ``MAX_ACCESSIBILITY_YAML_CHARS`` as a final safety ceiling
           on the *filtered* output (rarely hit after distillation).
        """
        try:
            raw = await page.locator("body").aria_snapshot()
            if raw is None or not raw.strip():
                return None

            distilled, raw_len, interactive_count = _distill_aria_snapshot(raw)

            logger.info(
                "Accessibility tree: %d chars raw -> %d chars after "
                "distillation (%d interactive elements found)",
                raw_len, len(distilled), interactive_count,
            )

            if len(distilled) > MAX_ACCESSIBILITY_YAML_CHARS:
                logger.warning(
                    "Distilled accessibility tree still exceeds %d chars "
                    "(%d chars, %d interactive elements). Truncating — this "
                    "page has an extraordinarily large interactive surface.",
                    MAX_ACCESSIBILITY_YAML_CHARS, len(distilled), interactive_count,
                )
                distilled = (
                    distilled[:MAX_ACCESSIBILITY_YAML_CHARS]
                    + "\n\n... (truncated after distillation)"
                )

            return distilled
        except Exception as exc:
            logger.warning("Failed to capture accessibility tree: %s", exc)
            return None

    # ------------------------------------------------------------------
    # LLM interaction (multi-provider via httpx)
    # ------------------------------------------------------------------

    async def _ask_llm(self, messages: list[dict]) -> Optional[dict]:
        """Send the accumulated multi-turn conversation to the configured LLM provider.

        Args:
            messages: List of ``{"role": ..., "content": ...}`` dicts forming
                      the full conversation (user / assistant turns only —
                      system prompt is added per-provider by the callee).
        """
        provider = self.config.llm_provider.lower()
        api_key = self.config.llm_api_key
        model = self.config.llm_model
        base_url = self.config.llm_base_url

        try:
            if provider == "anthropic":
                resp = await self._call_anthropic(api_key, model, base_url, messages)
            elif provider in ("openai", "deepseek"):
                resp = await self._call_openai_compatible(provider, api_key, model, base_url, messages)
            else:
                logger.error("Unknown LLM provider: %s", provider)
                return None

            # Surface HTTP errors (401, 429, 500, …) before parsing
            resp.raise_for_status()
            return self._parse_llm_response(provider, resp)

        except httpx.HTTPStatusError as exc:
            logger.error(
                "LLM API error (%s): HTTP %s — %s",
                provider,
                exc.response.status_code,
                exc.response.text[:200],
            )
            return None
        except Exception as exc:
            logger.error("LLM API error (%s): %s", provider, exc)
            return None

    async def _call_anthropic(self, api_key, model, base_url, messages: list[dict]):
        return await _shared_call_anthropic(
            api_key=api_key,
            model=model,
            messages=messages,
            system_prompt=ACTION_SYSTEM_PROMPT,
            max_tokens=self.config.llm_max_tokens or DEFAULT_MAX_TOKENS,
            tools=[_ACTION_TOOL_ANTHROPIC],
            tool_choice={"type": "tool", "name": "take_action"},
            base_url=base_url,
            client=self._client,
        )

    async def _call_openai_compatible(self, provider, api_key, model, base_url, messages: list[dict]):
        openai_tools = [_ACTION_TOOL_OPENAI] if provider == "openai" else None
        openai_tool_choice = (
            {"type": "function", "function": {"name": "take_action"}}
            if provider == "openai" else None
        )
        return await _shared_call_openai(
            provider=provider,
            api_key=api_key,
            model=model,
            messages=messages,
            system_prompt=ACTION_SYSTEM_PROMPT,
            max_tokens=self.config.llm_max_tokens,
            tools=openai_tools,
            tool_choice=openai_tool_choice,
            base_url=base_url,
            client=self._client,
        )

    def _parse_llm_response(self, provider, response) -> Optional[dict]:
        """Extract the action dict from a provider-specific API response.

        Priority order per provider:

        * **Anthropic** — native ``tool_use`` content block (guaranteed by
          ``tool_choice``).
        * **OpenAI** — native ``tool_calls`` in the message (guaranteed by
          ``tool_choice``).
        * **DeepSeek** — text-extraction fallback (tool-calling support is
          not confirmed for DeepSeek's API, so we keep the existing
          ``_extract_json`` path).
        * Any provider — if the structured field is absent for any reason,
          fall back to text extraction on the first text / content block.
        """
        data = response.json()

        # ---- Anthropic: tool_use block ----------------------------------
        if provider == "anthropic":
            content = data.get("content", [])
            for block in content:
                if block.get("type") == "tool_use":
                    parsed = block.get("input")
                    if parsed:
                        logger.debug("LLM (anthropic) tool-use: %s", parsed)
                        return parsed
            # Fallback: text content (should rarely happen with tool_choice)
            text = next(
                (b.get("text", "") for b in content if b.get("type") == "text"), ""
            ).strip()
            if text:
                parsed = _extract_json(text)
                if parsed is not None:
                    logger.debug("LLM (anthropic) text fallback: %s", parsed)
                    return parsed
            logger.warning(
                "Could not parse Anthropic response. Content blocks: %s", content
            )
            return None

        # ---- OpenAI / DeepSeek -------------------------------------------
        choices = data.get("choices", [])
        msg = choices[0].get("message", {}) if choices else {}

        # OpenAI: tool_calls in the message
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            try:
                raw_args = tool_calls[0]["function"]["arguments"]
                parsed = json.loads(raw_args)
                logger.debug("LLM (%s) tool-call: %s", provider, parsed)
                return parsed
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                logger.warning(
                    "Failed to parse %s tool-call arguments: %s", provider, exc
                )

        # Text-extraction fallback (DeepSeek + any unexpected response shape)
        text = msg.get("content", "").strip()
        parsed = _extract_json(text)
        if parsed is not None:
            logger.debug("LLM (%s) text: %s", provider, parsed)
            return parsed

        logger.warning(
            "Could not parse JSON from %s response. Extracted content "
            "length=%d, content=%r. Full raw response: %s",
            provider, len(text), text[:200], data,
        )
        return None

    # Deprecated backward-compat alias
    async def _ask_claude(self, snapshot, current_url):
        import warnings
        warnings.warn("_ask_claude is deprecated; use _ask_llm", DeprecationWarning, stacklevel=2)
        user_msg = (
            f"Current URL: {current_url}\n\n"
            f"Accessibility tree snapshot:\n{snapshot}\n\n"
            "What is the next action to explore this application?"
        )
        return await self._ask_llm([{"role": "user", "content": user_msg}])

    # ------------------------------------------------------------------
    # Multi-turn conversation building & context window management
    # ------------------------------------------------------------------

    def _build_user_turn(self, snapshot: str, current_url: str, actions_taken: int) -> str:
        """Build the user-message content for this turn.

        On the very first turn (actions_taken == 0) this is just the
        current snapshot.  On subsequent turns it prepends the outcome
        of the *previous* action so the model learns what happened.
        """
        base = (
            f"Current URL: {current_url}\n\n"
            f"Accessibility tree snapshot:\n{snapshot}\n\n"
            "What is the next action to explore this application?"
        )

        if actions_taken == 0:
            return base

        # Prepend outcome of the previous action
        prev_entry = self._audit_entries[-1]
        action_type = prev_entry.action.action_type.value
        target = prev_entry.action.target_text or ""

        outcome = f"Your previous action ({action_type}"
        if target:
            outcome += f" on '{target}'"
        outcome += f") {'succeeded' if prev_entry.success else 'failed'}"
        if prev_entry.error_message:
            outcome += f": {prev_entry.error_message}"
        outcome += "."

        # Note URL change if the page navigated
        prev_url = prev_entry.action.current_url
        if current_url != prev_url:
            outcome += f" Navigated to {current_url}."

        return outcome + "\n\n" + base

    def _manage_context(self) -> list[dict]:
        """Return a windowed copy of ``_message_history`` suitable for the LLM.

        - The most recent ``history_snapshot_window`` user messages keep
          their full accessibility-tree YAML.
        - Older user messages are condensed to one-line summaries.
        - A hard ``max_history_chars`` ceiling is enforced by dropping
          the oldest (already-condensed) user+assistant pairs from the
          front.
        """
        msgs = [dict(m) for m in self._message_history]
        if not msgs:
            return msgs

        window = self.config.history_snapshot_window
        max_chars = self.config.max_history_chars

        # ---- phase 1: condense old snapshots --------------------------------
        user_indices = [i for i, m in enumerate(msgs) if m["role"] == "user"]

        if len(user_indices) > window:
            keep_full = set(user_indices[-window:])
            for step_num, idx in enumerate(user_indices, start=1):
                if idx in keep_full:
                    continue
                msgs[idx]["content"] = self._condense_snapshot_message(
                    msgs[idx]["content"], step_num
                )

        # ---- phase 2: enforce hard character ceiling ------------------------
        total = sum(len(m["content"]) for m in msgs)
        while total > max_chars and len(msgs) >= 2:
            removed = msgs.pop(0)  # oldest user
            total -= len(removed["content"])
            if msgs and msgs[0]["role"] == "assistant":
                removed = msgs.pop(0)
                total -= len(removed["content"])

        return msgs

    @staticmethod
    def _condense_snapshot_message(content: str, step_num: int) -> str:
        """Replace a full-snapshot user message with a compact one-liner.

        Splits on the ``Accessibility tree snapshot:`` marker, keeps only
        the outcome/URL prefix, and formats it as ``Step N: …``.
        """
        parts = content.split("\n\nAccessibility tree snapshot:", 1)
        outcome = parts[0].strip() if parts else content

        # Normalise whitespace and strip the trailing prompt
        outcome = outcome.replace("Current URL:", "URL:")
        outcome = " ".join(outcome.split())
        outcome = outcome.replace("\n\nWhat is the next action to explore this application?", "")
        # Also handle the case where it was on the same "line" after collapse
        if "What is the next action" in outcome:
            outcome = outcome.split("What is the next action")[0].strip()

        return f"Step {step_num}: {outcome}"

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

        if not page_url_matches_any_scope(page.url, self.config.authorized_hostname):
            raise ScopeError(
                f"AgentExplorer scope violation: page at '{page.url}' "
                f"is outside authorized scope {display_scope(self.config.authorized_hostname)}"
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

        # Derive hostname for filename from storage_key (when set) or
        # authorized_hostname (only when it's a plain str)
        if self.config.storage_key:
            hostname = self.config.storage_key.replace(":", "_").replace("/", "_")
        elif isinstance(self.config.authorized_hostname, str):
            hostname = self.config.authorized_hostname.replace(":", "_").replace("/", "_")
        else:
            raise ValueError(
                "ExplorerConfig.authorized_hostname is a list, but "
                "storage_key is not set."
            )
        log_file = self.config.audit_log_path / f"{hostname}_{timestamp}.jsonl"

        with open(log_file, "w") as f:
            for entry in self._audit_entries:
                f.write(entry.model_dump_json() + "\n")

        logger.debug("Audit log written to %s (%d entries)", log_file, len(self._audit_entries))

    @property
    def audit_log(self) -> list[AuditLogEntry]:
        return list(self._audit_entries)
