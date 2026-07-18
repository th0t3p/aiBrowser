"""Tests for the scope guard violation tracking and propagation (Fix #1)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.browser_session import BrowserSession, BrowserSessionConfig, ScopeGuardError


class TestScopeGuardViolationTracking:
    """Test that scope guard violations are recorded and can be checked."""

    @staticmethod
    def _make_config(hostname="example.com"):
        return BrowserSessionConfig(authorized_hostname=hostname)

    @staticmethod
    def _make_mock_page():
        """Return a mock Playwright Page whose route() captures the handler."""
        page = AsyncMock()
        page.url = "https://example.com"
        page.main_frame = MagicMock()
        page.main_frame.url = "https://example.com"
        page._routes = {}

        async def mock_route(pattern, handler):
            page._routes[pattern] = handler

        page.route = mock_route
        page.on = MagicMock()
        return page

    @staticmethod
    def _make_mock_context(page):
        ctx = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.pages = [page]
        ctx.add_cookies = AsyncMock()
        ctx.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        ctx.close = AsyncMock()
        return ctx

    @staticmethod
    def _make_session(config, context):
        """Create a BrowserSession primed with a mocked context (no real browser needed)."""
        session = BrowserSession(config)
        session._playwright = AsyncMock()
        session._playwright.stop = AsyncMock()
        session._context = context
        session._browser = context
        return session

    async def _install_guard(self, session):
        """Install the scope guard on the mock page within the session."""
        mock_page = session._context.pages[0]
        await session._install_scope_guard(mock_page)

    async def _trigger_violation(self, session, url: str) -> None:
        """Simulate a Playwright route hitting the guard for an unauthorized URL."""
        mock_page = session._context.pages[0]
        handler = mock_page._routes.get("**/*")
        assert handler is not None, "Scope guard route was not installed"

        mock_route = AsyncMock()
        mock_route.request = MagicMock()
        mock_route.request.url = url
        mock_route.request.resource_type = "document"  # simulate top-level navigation
        await handler(mock_route)

    async def _trigger_subresource_block(self, session, url: str, resource_type: str = "script") -> None:
        """Simulate a sub-resource (JS, CSS, image) being blocked."""
        mock_page = session._context.pages[0]
        handler = mock_page._routes.get("**/*")
        assert handler is not None, "Scope guard route was not installed"

        mock_route = AsyncMock()
        mock_route.request = MagicMock()
        mock_route.request.url = url
        mock_route.request.resource_type = resource_type
        await handler(mock_route)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_violation_initially(self):
        """A fresh session starts with an empty violations list."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        assert session.violations == []

    @pytest.mark.asyncio
    async def test_authorized_navigation_not_recorded(self):
        """Navigating to the authorized hostname does not record a violation."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)

        handler = page._routes.get("**/*")
        assert handler is not None

        mock_route = AsyncMock()
        mock_route.request = MagicMock()
        mock_route.request.url = "https://example.com/page"
        await handler(mock_route)

        mock_route.continue_.assert_called_once()
        assert session.violations == []

    @pytest.mark.asyncio
    async def test_violation_recorded_in_list(self):
        """An unauthorized URL is recorded in violations and the request is aborted."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)
        await self._trigger_violation(session, "https://evil.com/tracker.js")

        assert len(session.violations) == 1
        violation = session.violations[0]
        assert isinstance(violation, ScopeGuardError)
        assert violation.attempted_hostname == "evil.com"
        assert violation.authorized_hostname == "example.com"

    @pytest.mark.asyncio
    async def test_check_violations_raises(self):
        """check_violations() raises the most recent ScopeGuardError."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)
        await self._trigger_violation(session, "https://evil.com/tracker.js")

        with pytest.raises(ScopeGuardError) as exc_info:
            session.check_violations()
        assert exc_info.value.attempted_hostname == "evil.com"

    @pytest.mark.asyncio
    async def test_check_violations_noop_when_empty(self):
        """check_violations() does nothing when no violations exist."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        session.check_violations()  # should not raise

    @pytest.mark.asyncio
    async def test_violation_event_is_set(self):
        """The _violation_event is set when a violation occurs."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)

        assert not session._get_violation_event().is_set()
        await self._trigger_violation(session, "https://evil.com/tracker.js")
        assert session._get_violation_event().is_set()

    @pytest.mark.asyncio
    async def test_multiple_violations_all_recorded(self):
        """Multiple violations are all appended to the list."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)
        await self._trigger_violation(session, "https://cdn.evil.com/beacon.js")
        await self._trigger_violation(session, "https://ads.evil.com/pixel.gif")

        assert len(session.violations) == 2
        assert session.violations[0].attempted_hostname == "cdn.evil.com"
        assert session.violations[1].attempted_hostname == "ads.evil.com"

        with pytest.raises(ScopeGuardError) as exc_info:
            session.check_violations()
        assert exc_info.value.attempted_hostname == "ads.evil.com"

    @pytest.mark.asyncio
    async def test_goto_wrapper_checks_violations(self):
        """session.goto() calls page.goto() and then check_violations()."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        page.goto = AsyncMock()

        # Pre-populate a violation so check_violations will find one
        session.violations.append(
            ScopeGuardError(attempted_hostname="evil.com", authorized_hostname="example.com")
        )

        with pytest.raises(ScopeGuardError):
            await session.goto(page, "https://example.com")

        page.goto.assert_called_once_with("https://example.com")

    @pytest.mark.asyncio
    async def test_new_page_checks_violations(self):
        """new_page() calls check_violations() after creating the page."""
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)

        # Pre-populate a violation
        session.violations.append(
            ScopeGuardError(attempted_hostname="evil.com", authorized_hostname="example.com")
        )

        with pytest.raises(ScopeGuardError):
            await session.new_page()


class TestSubresourceBlocking:
    """Test that blocked sub-resources don't kill the crawl."""

    @staticmethod
    def _make_config(hostname="example.com"):
        return BrowserSessionConfig(authorized_hostname=hostname)

    @staticmethod
    def _make_mock_page():
        page = AsyncMock()
        page.url = "https://example.com"
        page.main_frame = MagicMock()
        page.main_frame.url = "https://example.com"
        page._routes = {}

        async def mock_route(pattern, handler):
            page._routes[pattern] = handler

        page.route = mock_route
        page.on = MagicMock()
        return page

    @staticmethod
    def _make_mock_context(page):
        ctx = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.pages = [page]
        ctx.add_cookies = AsyncMock()
        ctx.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        ctx.close = AsyncMock()
        return ctx

    @staticmethod
    def _make_session(config, context):
        session = BrowserSession(config)
        session._playwright = AsyncMock()
        session._playwright.stop = AsyncMock()
        session._context = context
        session._browser = context
        return session

    async def _install_guard(self, session):
        mock_page = session._context.pages[0]
        await session._install_scope_guard(mock_page)

    async def _trigger_subresource(self, session, url, resource_type="script"):
        mock_page = session._context.pages[0]
        handler = mock_page._routes.get("**/*")
        assert handler is not None
        mock_route = AsyncMock()
        mock_route.request = MagicMock()
        mock_route.request.url = url
        mock_route.request.resource_type = resource_type
        await handler(mock_route)

    @pytest.mark.asyncio
    async def test_subresource_blocked_but_not_violation(self):
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)
        await self._trigger_subresource(session, "https://cdn.evil.com/app.js", "script")
        assert session.violations == []
        assert len(session.blocked_subresources) == 1
        assert session.blocked_subresources[0].hostname == "cdn.evil.com"
        session.check_violations()

    @pytest.mark.asyncio
    async def test_document_navigation_still_records_violation(self):
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)
        await self._trigger_subresource(session, "https://evil.com", "document")
        assert len(session.violations) == 1

    @pytest.mark.asyncio
    async def test_mixed_subresources_and_document(self):
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)
        await self._trigger_subresource(session, "https://cdn.evil.com/app.js", "script")
        await self._trigger_subresource(session, "https://img.evil.com/pixel.png", "image")
        assert len(session.blocked_subresources) == 2
        assert session.violations == []
        await self._trigger_subresource(session, "https://evil.com", "document")
        assert len(session.violations) == 1

    @pytest.mark.asyncio
    async def test_get_blocked_subresource_summary(self):
        config = self._make_config()
        page = self._make_mock_page()
        context = self._make_mock_context(page)
        session = self._make_session(config, context)
        await self._install_guard(session)
        await self._trigger_subresource(session, "https://cdn.evil.com/a.js", "script")
        await self._trigger_subresource(session, "https://cdn.evil.com/b.js", "script")
        await self._trigger_subresource(session, "https://img.evil.com/pixel.png", "image")
        count, hosts = session.get_blocked_subresource_summary()
        assert count == 3
        assert "cdn.evil.com" in hosts
        assert "img.evil.com" in hosts
        assert len(hosts) == 2


class TestCrawlResultSubresourceFields:
    """Test CrawlResult blocked_subresource fields."""

    def test_crawl_result_has_subresource_fields(self):
        from ai_browser.crawler import CrawlResult, CrawlConfig
        config = CrawlConfig(start_url="https://example.com", seed_hostname="example.com")
        result = CrawlResult(config=config)
        assert result.blocked_subresource_count == 0
        assert result.blocked_subresource_hostnames == []
