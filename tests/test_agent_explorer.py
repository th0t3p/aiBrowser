"""Tests for AgentExplorer — ARIA snapshot capture and about:blank guard."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.agent_explorer.explorer import (
    AgentExplorer,
    MAX_ACCESSIBILITY_YAML_CHARS,
)
from ai_browser.agent_explorer import ExplorerConfig


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
# about:blank guard in explore()
# ---------------------------------------------------------------------------

class TestAboutBlankGuard:
    """Tests that explore() returns early when the start page is about:blank."""

    @pytest.mark.asyncio
    async def test_skips_exploration_when_url_is_about_blank(self):
        """explore() returns early with zero actions when start_page.url
        is 'about:blank'."""
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
        """explore() continues the loop when start_page.url is a real page."""
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
