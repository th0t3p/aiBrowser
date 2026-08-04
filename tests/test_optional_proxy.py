"""Tests for optional proxy routing (--no-proxy / proxy=None support)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_browser.browser_session import BrowserSession, BrowserSessionConfig
from ai_browser.browser_session.models import ProxyConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(*, proxy=None, expose_cdp: bool = False, **kwargs) -> BrowserSessionConfig:
    return BrowserSessionConfig(
        authorized_hostname="example.com",
        proxy=proxy,
        expose_cdp=expose_cdp,
        headless=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Config-model tests
# ---------------------------------------------------------------------------


class TestOptionalProxyConfig:
    def test_proxy_none_accepted(self):
        """Model accepts proxy=None and reads it back as None."""
        config = BrowserSessionConfig(
            authorized_hostname="example.com",
            proxy=None,
        )
        assert config.proxy is None

    def test_default_proxy_still_burp(self):
        """Default construction (no proxy= passed) still produces a
        ProxyConfig pointed at http://127.0.0.1:8080."""
        config = BrowserSessionConfig(authorized_hostname="example.com")
        assert isinstance(config.proxy, ProxyConfig)
        assert config.proxy.server == "http://127.0.0.1:8080"


# ---------------------------------------------------------------------------
# Propagation tests: proxy=None must reach Playwright's launch calls
# ---------------------------------------------------------------------------


class TestNoProxyPropagationCDP:
    @pytest.mark.asyncio
    async def test_cdp_path_with_no_proxy(self):
        """proxy=None on the expose_cdp=True path → launch() receives proxy=None."""
        config = _make_config(proxy=None, expose_cdp=True)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock(return_value=mock_browser)
        mock_chromium.launch_persistent_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_browser.contexts = [mock_context]
        mock_context.pages = []
        mock_context.on = MagicMock()

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        mock_chromium.launch.assert_called_once()
        _, kwargs = mock_chromium.launch.call_args
        assert kwargs.get("proxy") is None, (
            f"Expected proxy=None in launch() kwargs, got {kwargs.get('proxy')!r}"
        )

    @pytest.mark.asyncio
    async def test_non_cdp_path_with_no_proxy(self):
        """proxy=None on the expose_cdp=False path →
        launch_persistent_context() receives proxy=None."""
        config = _make_config(proxy=None, expose_cdp=False)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock()
        mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_context.pages = []
        mock_context.on = MagicMock()

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        mock_chromium.launch_persistent_context.assert_called_once()
        _, kwargs = mock_chromium.launch_persistent_context.call_args
        assert kwargs.get("proxy") is None, (
            f"Expected proxy=None in launch_persistent_context() kwargs, "
            f"got {kwargs.get('proxy')!r}"
        )


# ---------------------------------------------------------------------------
# Sanity-check: default Burp proxy still propagates correctly
# ---------------------------------------------------------------------------


class TestDefaultProxyPropagation:
    @pytest.mark.asyncio
    async def test_default_proxy_propagates_to_launch(self):
        """Default config (no proxy= passed) passes the real proxy dict
        through to Playwright's launch — protects against accidentally
        altering existing default behavior."""
        # Construct directly without proxy= so default_factory kicks in
        config = BrowserSessionConfig(
            authorized_hostname="example.com",
            headless=True,
        )
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock()
        mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_context.pages = []
        mock_context.on = MagicMock()

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        mock_chromium.launch_persistent_context.assert_called_once()
        _, kwargs = mock_chromium.launch_persistent_context.call_args
        assert kwargs.get("proxy") == {"server": "http://127.0.0.1:8080"}, (
            f"Expected default Burp proxy dict, got {kwargs.get('proxy')!r}"
        )
