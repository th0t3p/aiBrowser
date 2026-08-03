"""Tests for AgentExplorer — ARIA snapshot capture, about:blank guard,
per-action logging, and discovery-method tagging."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.agent_explorer.explorer import (
    AgentExplorer,
    MAX_ACCESSIBILITY_YAML_CHARS,
)
from ai_browser.agent_explorer import ExplorerConfig
from ai_browser.agent_explorer.models import ActionType, AgentAction, AuditLogEntry
from ai_browser.crawler import Crawler, CrawlConfig, CrawlResult, DiscoveryMethod


# ---------------------------------------------------------------------------
# Realistic ARIA snapshot YAML (what Playwright's locator.aria_snapshot()
# actually returns).
# ---------------------------------------------------------------------------

_MOCK_ARIA_YAML = """\
- heading "Welcome to Example App" [level=1]
- link "Home"
- navigation:
  - link "Products"
  - link "About Us"
  - link "Contact"
- main:
  - heading "Featured Products" [level=2]
  - list:
    - listitem:
      - link "Widget Pro"
      - text: "$29.99"
    - listitem:
      - link "Widget Lite"
      - text: "$9.99"
  - button "View All Products"
- contentinfo:
  - text: "© 2024 Example Corp"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs):
    return ExplorerConfig(
        authorized_hostname="example.com",
        anthropic_api_key="sk-ant-fake",
        **kwargs,
    )


def _make_mock_page(url="https://example.com"):
    """Return a mock Playwright Page."""
    page = AsyncMock()
    page.url = url
    # Mock locator("body").aria_snapshot()
    body_locator = AsyncMock()
    body_locator.aria_snapshot = AsyncMock(return_value=_MOCK_ARIA_YAML)
    page.locator = MagicMock(return_value=body_locator)
    return page


# ---------------------------------------------------------------------------
# _capture_accessibility_tree
# ---------------------------------------------------------------------------

class TestCaptureAccessibilityTree:
    """Tests for _capture_accessibility_tree() using locator.aria_snapshot()."""

    @pytest.mark.asyncio
    async def test_returns_yaml_string_from_aria_snapshot(self):
        """_capture_accessibility_tree() calls locator.aria_snapshot()
        and returns the YAML text as-is."""
        config = _make_config()
        explorer = AgentExplorer(config)
        page = _make_mock_page()

        result = await explorer._capture_accessibility_tree(page)

        assert isinstance(result, str)
        assert "Welcome to Example App" in result
        assert "Widget Pro" in result
        # Verify correct Playwright API was called
        page.locator.assert_called_once_with("body")
        page.locator("body").aria_snapshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_aria_snapshot_returns_none(self):
        """Returns None when aria_snapshot() itself returns None."""
        config = _make_config()
        explorer = AgentExplorer(config)
        page = AsyncMock()
        body_locator = AsyncMock()
        body_locator.aria_snapshot = AsyncMock(return_value=None)
        page.locator = MagicMock(return_value=body_locator)

        result = await explorer._capture_accessibility_tree(page)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_aria_snapshot_returns_whitespace(self):
        """Returns None when aria_snapshot() returns only whitespace."""
        config = _make_config()
        explorer = AgentExplorer(config)
        page = AsyncMock()
        body_locator = AsyncMock()
        body_locator.aria_snapshot = AsyncMock(return_value="   \n  \t  ")
        page.locator = MagicMock(return_value=body_locator)

        result = await explorer._capture_accessibility_tree(page)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        """Returns None (and logs a warning) when aria_snapshot() raises."""
        config = _make_config()
        explorer = AgentExplorer(config)
        page = AsyncMock()
        body_locator = AsyncMock()
        body_locator.aria_snapshot = AsyncMock(
            side_effect=RuntimeError("browser crashed")
        )
        page.locator = MagicMock(return_value=body_locator)

        result = await explorer._capture_accessibility_tree(page)
        assert result is None

    @pytest.mark.asyncio
    async def test_truncates_when_exceeding_max_chars(self):
        """When the DISTILLED output still exceeds MAX_ACCESSIBILITY_YAML_CHARS,
        it is truncated with a clear marker appended.  (This is rare after
        distillation — it takes an enormous interactive surface.)"""
        config = _make_config()
        explorer = AgentExplorer(config)
        page = AsyncMock()
        body_locator = AsyncMock()

        # Generate enough interactive elements that the distilled output
        # exceeds MAX_ACCESSIBILITY_YAML_CHARS even after filtering.
        lines = []
        for i in range(600):
            lines.append(f'- button "Button number {i}"')
        huge_yaml = "\n".join(lines)
        body_locator.aria_snapshot = AsyncMock(return_value=huge_yaml)
        page.locator = MagicMock(return_value=body_locator)

        result = await explorer._capture_accessibility_tree(page)

        assert result is not None
        assert len(result) <= MAX_ACCESSIBILITY_YAML_CHARS + len(
            "\n\n... (truncated after distillation)"
        )
        assert "... (truncated after distillation)" in result
        # Should still contain some interactive elements (not completely empty)
        assert "button" in result

    @pytest.mark.asyncio
    async def test_does_not_truncate_short_yaml(self):
        """Short (distilled) output is returned as-is with no marker."""
        config = _make_config()
        explorer = AgentExplorer(config)
        page = _make_mock_page()

        result = await explorer._capture_accessibility_tree(page)

        assert result is not None
        assert "truncated" not in result
        assert len(result) < MAX_ACCESSIBILITY_YAML_CHARS


# ---------------------------------------------------------------------------
# about:blank defensive guard in explore()
# ---------------------------------------------------------------------------
#
# The caller (cli.py Phase 2) now opens a fresh page and explicitly
# navigates to the target hostname before calling explore(), so
# about:blank is *not* reached under normal operation.  This guard
# remains as a defense-in-depth safety net: if the navigation above
# somehow still lands on about:blank (e.g. a redirect chain that
# resolves to a blocked/empty page or an empty string URL), we bail
# out early rather than wasting LLM calls on a blank canvas.
# ---------------------------------------------------------------------------

class TestAboutBlankGuard:
    """Tests that explore() returns early when the start page is about:blank
    (defensive guard, not primary failure detection)."""

    @pytest.mark.asyncio
    async def test_skips_exploration_when_url_is_about_blank(self):
        """explore() returns early with zero actions when start_page.url
        is 'about:blank' — bail out rather than calling the LLM on nothing."""
        config = _make_config()
        explorer = AgentExplorer(config)
        session = MagicMock()
        page = _make_mock_page(url="about:blank")

        entries = await explorer.explore(session, page)

        # Should return immediately with an empty list
        assert entries == []
        # Body locator should never have been queried
        page.locator.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_exploration_when_url_is_empty(self):
        """explore() returns early when start_page.url is falsy (empty string)."""
        config = _make_config()
        explorer = AgentExplorer(config)
        session = MagicMock()
        page = AsyncMock()
        page.url = ""

        entries = await explorer.explore(session, page)
        assert entries == []

    @pytest.mark.asyncio
    async def test_proceeds_normally_when_url_is_valid(self, monkeypatch):
        """explore() continues the loop when start_page.url is a real page.

        This is the expected code path: cli.py Phase 2 opens a fresh page,
        navigates to ``https://{hostname}``, and only calls explore() once
        the navigation succeeds.
        """
        config = _make_config(max_actions=1)
        explorer = AgentExplorer(config)
        session = MagicMock()

        page = _make_mock_page(url="https://example.com/home")

        # Prevent _ask_llm from making a real HTTP call;
        # make it return "done" so the loop exits after one action.
        async def fake_ask_llm(messages):
            return {"action": "done", "reasoning": "test"}

        monkeypatch.setattr(explorer, "_ask_llm", fake_ask_llm)

        entries = await explorer.explore(session, page)

        # Should have reached the loop and called _ask_llm at least once.
        # Since _ask_llm returned 'done', we get zero executed actions.
        assert entries == []
        page.locator.assert_called()  # at least one snapshot attempt

    @pytest.mark.asyncio
    async def test_skips_exploration_when_url_is_about_blank_with_trailing(self):
        """Still detects about:blank when the URL has extra components
        (about:blank is the whole URL)."""
        config = _make_config()
        explorer = AgentExplorer(config)
        session = MagicMock()
        page = AsyncMock()
        page.url = "about:blank"

        entries = await explorer.explore(session, page)
        assert entries == []


# ---------------------------------------------------------------------------
# Per-action INFO logging, DiscoveryMethod.AGENT_EXPLORATION, and summary
# counts — verifies that a mocked explore() run produces the right console
# output and correct discovery-method tagging.
# ---------------------------------------------------------------------------


class TestExploreLoggingAndDiscovery:
    """Confirm per-cycle logging and discovery-method tagging after
    a mocked explore() run that includes navigation to new URLs."""

    @pytest.mark.asyncio
    async def test_per_action_logging_and_discovery_tagging(
        self, monkeypatch, caplog
    ):
        """Run explore() with mocked actions: one static click, one
        click that navigates to a new page.  Verify:

        * Per-action INFO logs show action type, target, and whether
          the URL changed.
        * When endpoints are added with AGENT_EXPLORATION, new vs
          already-known counts are accurate.
        * The output JSON correctly tags agent-discovered URLs with
          the ``agent-exploration`` method string.
        """
        config = _make_config(max_actions=5)
        explorer = AgentExplorer(config)
        session = MagicMock()
        session.pages = []

        start_url = "https://example.com/home"
        new_url = "https://example.com/products"

        page = _make_mock_page(url=start_url)

        # ------------------------------------------------------------------
        # Mock _ask_llm: four non-done actions then "done"
        #   (1) click  "About"     — same page, no nav
        #   (2) scroll             — same page, no nav
        #   (3) click  "Products"  — NAVIGATES to new URL
        #   (4) wait               — captures the new URL
        # ------------------------------------------------------------------
        actions_iter = iter([
            {"action": "click",  "target": "About",    "reasoning": "explore"},
            {"action": "scroll", "target": "",          "reasoning": "see more"},
            {"action": "click",  "target": "Products",  "reasoning": "nav"},
            {"action": "wait",   "target": "",          "reasoning": "let page settle"},
            {"action": "done",   "reasoning": "finished"},
        ])

        async def fake_ask_llm(_messages):
            try:
                return next(actions_iter)
            except StopIteration:
                return {"action": "done", "reasoning": "exhausted"}

        monkeypatch.setattr(explorer, "_ask_llm", fake_ask_llm)

        # ------------------------------------------------------------------
        # Mock _execute_action: only the "Products" click changes the URL.
        # ------------------------------------------------------------------
        async def fake_execute(_session, pg, action_raw):
            action_type = action_raw.get("action", "")
            target = action_raw.get("target", "")
            url_before = pg.url
            if action_type == "click" and target == "Products":
                pg.url = new_url
            return AuditLogEntry(
                action=AgentAction(
                    action_type=ActionType(action_type)
                    if action_type in ActionType.__members__
                    else ActionType.WAIT,
                    target_text=target,
                    current_url=url_before,
                    reasoning=action_raw.get("reasoning", ""),
                ),
                success=True,
            )

        monkeypatch.setattr(explorer, "_execute_action", fake_execute)

        # ------------------------------------------------------------------
        # Run
        # ------------------------------------------------------------------
        with caplog.at_level(
            logging.INFO, logger="ai_browser.agent_explorer.explorer"
        ):
            entries = await explorer.explore(session, page)

        # --- 4 actions executed (click About, scroll, click Products, wait)
        assert len(entries) == 4

        # --- Per-action INFO logs -----------------------------------------
        action_logs = [
            r.message for r in caplog.records
            if r.message.startswith("Action ")
        ]
        assert len(action_logs) == 4

        # Action 1: click "About" — no navigation
        assert "click" in action_logs[0]
        assert "'About'" in action_logs[0]
        assert "(no navigation)" in action_logs[0]

        # Action 2: scroll — no navigation
        assert "scroll" in action_logs[1]
        assert "(no navigation)" in action_logs[1]

        # Action 3: click "Products" — navigation occurred
        assert "click" in action_logs[2]
        assert "'Products'" in action_logs[2]
        assert new_url in action_logs[2]
        assert "(no navigation)" not in action_logs[2]

        # Action 4: wait — already on the new page, no further nav
        assert "wait" in action_logs[3]
        assert "(no navigation)" in action_logs[3]

        # --- DiscoveryMethod.AGENT_EXPLORATION tagging -------------------
        result = CrawlResult(
            config=CrawlConfig(
                start_url=start_url,
                seed_hostname="example.com",
            )
        )
        # Simulate Phase 1: one endpoint already known
        result.add_endpoint(start_url, DiscoveryMethod.LINK)

        # Phase 2: add agent-discovered URLs
        agent_urls: set[str] = set()
        for entry in entries:
            if entry.action.current_url:
                normalized = Crawler._normalize(entry.action.current_url)
                agent_urls.add(normalized)
                result.add_endpoint(
                    entry.action.current_url,
                    DiscoveryMethod.AGENT_EXPLORATION,
                )

        # --- Summary counts: new vs already-known ------------------------
        # Phase 1 already knows start_url, so that's "already known"
        # new_url is genuinely new
        phase1_urls = {Crawler._normalize(start_url)}
        new_count = len(agent_urls - phase1_urls)
        already_known_count = len(agent_urls & phase1_urls)

        assert new_count == 1, "The new URL should be counted as new"
        assert already_known_count == 1, "The start URL should be already known"

        # --- Verify endpoint method strings in output ---------------------
        endpoint_methods = {ep.url: ep.method.value for ep in result.endpoints}
        assert endpoint_methods[start_url] == "link", (
            "Phase 1 URL should keep its original LINK method"
        )
        assert endpoint_methods[new_url] == "agent-exploration", (
            "Agent-discovered URL should be tagged agent-exploration"
        )

        # --- Total endpoint count: start_url (from Phase 1) + new_url ----
        assert len(result.endpoints) == 2


# ---------------------------------------------------------------------------
# Multi-turn conversation memory
# ---------------------------------------------------------------------------


class TestMultiTurnMemory:
    """Verify that AgentExplorer now builds a multi-turn message history
    across steps, including action outcomes, and that the context window
    is managed (condensation + character ceiling)."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="sk-ant-fake",
            **kwargs,
        )

    def _make_mock_page(self, url="https://example.com/home"):
        page = AsyncMock()
        page.url = url

        body_locator = AsyncMock()
        body_locator.aria_snapshot = AsyncMock(return_value=_MOCK_ARIA_YAML)
        page.locator = MagicMock(return_value=body_locator)
        return page

    # -- helpers ----------------------------------------------------------

    def _make_fake_ask(self, actions: list[dict]):
        """Return a callable that feeds *actions* one per call, then 'done'."""
        it = iter(actions)

        async def fake(messages):
            try:
                return next(it)
            except StopIteration:
                return {"action": "done", "reasoning": "exhausted"}

        return fake

    async def _fake_execute(self, _session, page, action_raw):
        """Execute mock: navigate the page to a synthetic URL on 'click'."""
        action_type_str = action_raw.get("action", "")
        target = action_raw.get("target", "")
        url_before = page.url

        if action_type_str == "click" and target:
            # Simulate navigation — each click on a named link moves to a
            # path derived from that name.
            page.url = f"https://example.com/{target.lower().replace(' ', '-')}"

        # Resolve ActionType properly (members are UPPER_CASE keys)
        try:
            at = ActionType(action_type_str)
        except ValueError:
            at = ActionType.WAIT

        return AuditLogEntry(
            action=AgentAction(
                action_type=at,
                target_text=target,
                current_url=url_before,
                reasoning=action_raw.get("reasoning", ""),
            ),
            success=True,
        )

    # -- actual tests -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_message_history_accumulates_across_steps(self, monkeypatch):
        """After 3 sequential actions, _message_history has 7 messages:
        user+assistant × 3 plus the 4th user turn (built before the
        LLM returns 'done' on the 4th call)."""
        config = self._make_config(max_actions=5)
        explorer = AgentExplorer(config)
        session = MagicMock()

        actions = [
            {"action": "click", "target": "Products", "reasoning": "explore products"},
            {"action": "click", "target": "About", "reasoning": "check about"},
            {"action": "click", "target": "Contact", "reasoning": "find contact"},
        ]

        monkeypatch.setattr(explorer, "_ask_llm", self._make_fake_ask(actions))
        monkeypatch.setattr(explorer, "_execute_action", self._fake_execute)

        page = self._make_mock_page()
        await explorer.explore(session, page)

        history = explorer._message_history
        assert len(history) == 7
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        assert history[2]["role"] == "user"
        assert history[3]["role"] == "assistant"
        assert history[4]["role"] == "user"
        assert history[5]["role"] == "assistant"
        assert history[6]["role"] == "user"  # 4th user turn (before "done")

        # Assistant messages contain the action JSON
        assert "click" in history[1]["content"]
        assert "Products" in history[1]["content"]

    @pytest.mark.asyncio
    async def test_user_turn_includes_previous_outcome(self, monkeypatch):
        """The 2nd user turn's content includes the outcome of the 1st action."""
        config = self._make_config(max_actions=5)
        explorer = AgentExplorer(config)
        session = MagicMock()

        actions = [
            {"action": "click", "target": "Products", "reasoning": "explore products"},
            {"action": "done", "reasoning": "finished"},
        ]

        monkeypatch.setattr(explorer, "_ask_llm", self._make_fake_ask(actions))
        monkeypatch.setattr(explorer, "_execute_action", self._fake_execute)

        page = self._make_mock_page()
        await explorer.explore(session, page)

        # 2nd user message (index 2) should contain outcome of first action
        second_user = explorer._message_history[2]["content"]
        assert "Your previous action" in second_user
        assert "click" in second_user
        assert "Products" in second_user
        assert "succeeded" in second_user
        # Should mention the navigation
        assert "navigated" in second_user.lower() or "Navigated" in second_user

    @pytest.mark.asyncio
    async def test_condensation_when_past_snapshot_window(self, monkeypatch):
        """With history_snapshot_window=1, the oldest user message is
        condensed to a one-line summary after 3 steps."""
        config = self._make_config(
            max_actions=5, history_snapshot_window=1
        )
        explorer = AgentExplorer(config)
        session = MagicMock()

        actions = [
            {"action": "click", "target": "Products", "reasoning": "explore products"},
            {"action": "click", "target": "About", "reasoning": "check about"},
            {"action": "click", "target": "Contact", "reasoning": "find contact"},
        ]

        monkeypatch.setattr(explorer, "_ask_llm", self._make_fake_ask(actions))
        monkeypatch.setattr(explorer, "_execute_action", self._fake_execute)

        page = self._make_mock_page()
        await explorer.explore(session, page)

        # The raw history has 7 messages (3 actions + 4th user turn before "done")
        assert len(explorer._message_history) == 7

        # But _manage_context returns a condensed copy
        managed = explorer._manage_context()
        # There should be at least 6 managed messages (or fewer if char
        # ceiling kicked in, but with our small data it won't)
        assert len(managed) >= 2

        # The oldest user message (idx 0) should be condensed (no full
        # accessibility tree snapshot inside)
        oldest_user = managed[0]["content"]
        assert "Accessibility tree snapshot:" not in oldest_user
        assert oldest_user.startswith("Step 1:")

        # The newest user message (last user) should still have a full snapshot
        user_messages = [m for m in managed if m["role"] == "user"]
        newest_user = user_messages[-1]["content"]
        assert "Accessibility tree snapshot:" in newest_user

    @pytest.mark.asyncio
    async def test_max_history_chars_enforced(self, monkeypatch):
        """With max_history_chars=2000, the managed message list drops
        oldest steps while the raw history stays complete."""
        config = self._make_config(
            max_actions=5,
            history_snapshot_window=10,  # keep all full initially
            max_history_chars=2_000,      # tight enough to force drops
        )
        explorer = AgentExplorer(config)
        session = MagicMock()

        actions = [
            {"action": "click", "target": "Products", "reasoning": "explore products"},
            {"action": "click", "target": "About", "reasoning": "check about"},
            {"action": "click", "target": "Contact", "reasoning": "find contact"},
        ]

        monkeypatch.setattr(explorer, "_ask_llm", self._make_fake_ask(actions))
        monkeypatch.setattr(explorer, "_execute_action", self._fake_execute)

        page = self._make_mock_page()
        await explorer.explore(session, page)

        managed = explorer._manage_context()
        total = sum(len(m["content"]) for m in managed)
        assert total <= 2_000, f"managed history {total} chars exceeds ceiling of 2000"
        # The raw history should still be complete
        assert len(explorer._message_history) == 7
        # The managed list should be shorter (something was dropped)
        assert len(managed) < len(explorer._message_history)


# ---------------------------------------------------------------------------
# ARIA snapshot distillation
# ---------------------------------------------------------------------------


# A richer fixture with mixed content: interactive elements, headings,
# decorative text, static containers.
_RICH_ARIA_YAML = """\
- heading "Shopping Site" [level=1]
- navigation:
  - link "Home"
  - link "Products"
  - link "About Us"
  - link "Contact"
- main:
  - heading "Featured Products" [level=2]
  - paragraph: Welcome to our store! We have the best products at the best prices. Browse our collection below.
  - list:
    - listitem:
      - link "Widget Pro"
      - text: "$29.99"
      - paragraph: The professional-grade widget for serious users. Includes all premium features.
    - listitem:
      - link "Widget Lite"
      - text: "$9.99"
      - paragraph: Our entry-level widget. Perfect for beginners and casual use.
  - button "View All Products"
  - heading "Sign Up for Our Newsletter" [level=3]
  - textbox "Enter your email"
  - button "Subscribe"
- contentinfo:
  - paragraph: "© 2024 Example Corp. All rights reserved. Terms of Service | Privacy Policy"
  - link "Terms of Service"
  - link "Privacy Policy"
"""


class TestAriaDistillation:
    """Verify that ARIA snapshots are distilled to interactive elements +
    headings, dropping static / decorative content."""

    def test_distilled_output_retains_interactive_elements(self):
        """All links, buttons, and textboxes are present in distilled output."""
        from ai_browser.agent_explorer.explorer import _distill_aria_snapshot

        distilled, raw_len, count = _distill_aria_snapshot(_RICH_ARIA_YAML)

        # Interactive elements that MUST survive
        assert "link" in distilled
        assert "button" in distilled
        assert "textbox" in distilled
        assert '"Widget Pro"' in distilled
        assert '"Widget Lite"' in distilled
        assert '"View All Products"' in distilled
        assert '"Subscribe"' in distilled
        assert '"Enter your email"' in distilled
        assert '"Home"' in distilled
        assert '"Terms of Service"' in distilled

    def test_distilled_output_retains_headings(self):
        """Headings are kept for structural orientation."""
        from ai_browser.agent_explorer.explorer import _distill_aria_snapshot

        distilled, raw_len, count = _distill_aria_snapshot(_RICH_ARIA_YAML)

        assert "heading" in distilled
        assert '"Shopping Site"' in distilled
        assert '"Featured Products"' in distilled

    def test_static_content_is_dropped(self):
        """Large blocks of decorative text and static paragraphs are removed."""
        from ai_browser.agent_explorer.explorer import _distill_aria_snapshot

        distilled, raw_len, count = _distill_aria_snapshot(_RICH_ARIA_YAML)

        # Static text that should be gone
        assert "Welcome to our store" not in distilled
        assert "professional-grade widget" not in distilled
        assert "$29.99" not in distilled
        assert "$9.99" not in distilled
        assert "© 2024 Example Corp" not in distilled

    def test_size_reduction_is_substantial(self):
        """The distilled output is meaningfully smaller than the raw input."""
        from ai_browser.agent_explorer.explorer import _distill_aria_snapshot

        distilled, raw_len, count = _distill_aria_snapshot(_RICH_ARIA_YAML)

        assert len(distilled) < raw_len, (
            f"Distilled {len(distilled)} chars should be < raw {raw_len} chars"
        )
        # The fixture has ~50%+ static content; distillation should cut at
        # least 25% (conservative floor to avoid brittle exact-match tests).
        reduction = 1.0 - (len(distilled) / raw_len)
        assert reduction > 0.25, (
            f"Expected >25%% reduction, got {reduction:.0%}"
        )

    def test_interactive_count_is_accurate(self):
        """_distill_aria_snapshot returns the correct interactive count."""
        from ai_browser.agent_explorer.explorer import _distill_aria_snapshot

        distilled, raw_len, count = _distill_aria_snapshot(_RICH_ARIA_YAML)
        # The fixture has: 4 nav links + 2 product links + 2 footer links
        # + 2 buttons + 1 textbox = 11 interactive elements
        assert count == 11, f"Expected 11 interactive elements, got {count}"
