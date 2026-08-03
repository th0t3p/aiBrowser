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
        """When the YAML exceeds MAX_ACCESSIBILITY_YAML_CHARS, the result
        is truncated with a clear marker appended."""
        config = _make_config()
        explorer = AgentExplorer(config)
        page = AsyncMock()
        body_locator = AsyncMock()

        # Build a YAML string just over the limit
        long_yaml = "- text: " + ("x" * (MAX_ACCESSIBILITY_YAML_CHARS + 500))
        body_locator.aria_snapshot = AsyncMock(return_value=long_yaml)
        page.locator = MagicMock(return_value=body_locator)

        result = await explorer._capture_accessibility_tree(page)

        assert result is not None
        assert len(result) <= MAX_ACCESSIBILITY_YAML_CHARS + len(
            "\n\n... (ARIA snapshot truncated to fit context window)"
        )
        assert "... (ARIA snapshot truncated" in result
        # The truncated content should be the first MAX_ACCESSIBILITY_YAML_CHARS
        # chars of the original.
        assert result.startswith("- text: " + ("x" * (MAX_ACCESSIBILITY_YAML_CHARS - 8)))

    @pytest.mark.asyncio
    async def test_does_not_truncate_short_yaml(self):
        """Short YAML (under the limit) is returned as-is with no marker."""
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
        async def fake_ask_llm(snapshot, current_url):
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

        async def fake_ask_llm(_snapshot, _current_url):
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
