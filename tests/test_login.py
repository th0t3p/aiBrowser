"""Tests for LoginHandler and shared form helpers (Fix #3 section 2)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.login_handler import LoginHandler, LoginConfig
from ai_browser.registration_handler import RegistrationHandler
from ai_browser.registration_handler.models import RegistrationConfig
from ai_browser._form_helpers import fill_form_fields, submit_form, check_captcha


class TestLoginHandler:
    """Test LoginHandler form filling and CAPTCHA detection."""

    @staticmethod
    def _make_handler(**kwargs):
        cfg = LoginConfig(
            login_url="https://target.com/login",
            email="test@target.com",
            password="Password123!",
            **kwargs,
        )
        return LoginHandler(cfg)

    @pytest.mark.asyncio
    async def test_fill_login_form_uses_field_mappings(self):
        """LoginHandler fills email/password using shared fill_form_fields."""
        handler = self._make_handler()
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)  # no fields found

        await handler._fill_login_form(page)
        # Should have attempted to fill fields (even if none found)
        assert page.query_selector.called

    @pytest.mark.asyncio
    async def test_check_captcha_delegates_to_shared(self):
        """Login CAPTCHA check uses shared check_captcha helper."""
        handler = self._make_handler()
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)  # no CAPTCHA

        await handler._check_captcha(page, "test")
        assert page.query_selector.called


class TestSharedHelpersIdentity:
    """Confirm login and registration handlers use the SAME shared functions."""

    def test_fill_form_fields_is_same_object(self):
        """fill_form_fields used by login handler is the shared function."""
        from ai_browser.login_handler import handler as lh
        # Login handler still imports fill_form_fields from _form_helpers
        assert lh.fill_form_fields is fill_form_fields

    def test_submit_form_is_same_object(self):
        """submit_form used by both handlers is the identical function object."""
        from ai_browser.login_handler import handler as lh
        from ai_browser.registration_handler import handler as rh
        assert lh.submit_form is submit_form
        assert rh.submit_form is submit_form
        assert lh.submit_form is rh.submit_form

    def test_check_captcha_is_same_object(self):
        """check_captcha used by both handlers is the identical function object."""
        from ai_browser.login_handler import handler as lh
        from ai_browser.registration_handler import handler as rh
        assert lh.check_captcha is check_captcha
        assert rh.check_captcha is check_captcha
        assert lh.check_captcha is rh.check_captcha


class TestCookiesFileParsing:
    """Test _parse_cookies_file with both accepted shapes."""

    def test_bare_array_accepted(self):
        from ai_browser.browser_session.session import _parse_cookies_file
        raw = [
            {"name": "session", "value": "abc123", "domain": ".example.com", "path": "/"},
        ]
        result = _parse_cookies_file(raw)
        assert result == raw

    def test_storage_state_dict_accepted(self):
        from ai_browser.browser_session.session import _parse_cookies_file
        raw = {
            "cookies": [
                {"name": "session", "value": "abc", "domain": ".example.com", "path": "/"},
            ],
            "origins": [],
        }
        result = _parse_cookies_file(raw)
        assert result == raw["cookies"]

    def test_dict_without_cookies_key_errors(self):
        from ai_browser.browser_session.session import _parse_cookies_file
        import pytest
        with pytest.raises(ValueError, match="Unrecognized cookies-file shape"):
            _parse_cookies_file({"not_cookies": []})

    def test_string_errors(self):
        from ai_browser.browser_session.session import _parse_cookies_file
        import pytest
        with pytest.raises(ValueError, match="Unrecognized cookies-file shape"):
            _parse_cookies_file("not json at all")

    def test_empty_array_accepted(self):
        from ai_browser.browser_session.session import _parse_cookies_file
        result = _parse_cookies_file([])
        assert result == []


class TestPlainCookieParser:
    """Test _parse_plain_cookie_lines for 'name=value' per line format."""

    def test_simple_name_value_lines(self):
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "session_id=abc123\ncsrf_token=def456"
        result = _parse_plain_cookie_lines(text)
        assert len(result) == 2
        assert result[0] == {"name": "session_id", "value": "abc123"}
        assert result[1] == {"name": "csrf_token", "value": "def456"}

    def test_value_contains_equals_sign(self):
        """Values with '=' (base64 padding, JWTs) are preserved whole."""
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "token=eyJhbGciOiJIUzI1NiJ9.xyz=="
        result = _parse_plain_cookie_lines(text)
        assert len(result) == 1
        assert result[0]["name"] == "token"
        assert result[0]["value"] == "eyJhbGciOiJIUzI1NiJ9.xyz=="

    def test_value_with_multiple_equals(self):
        """Only the first '=' splits — values with multiple '=' stay intact."""
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "kv=base64=encoded==thing"
        result = _parse_plain_cookie_lines(text)
        assert result[0]["name"] == "kv"
        assert result[0]["value"] == "base64=encoded==thing"

    def test_blank_lines_skipped(self):
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "a=b\n\n\nc=d\n"
        result = _parse_plain_cookie_lines(text)
        assert len(result) == 2
        assert result[0]["name"] == "a"
        assert result[1]["name"] == "c"

    def test_comment_lines_skipped(self):
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "# this is a comment\na=b\n# another comment\nc=d"
        result = _parse_plain_cookie_lines(text)
        assert len(result) == 2
        assert result[0]["name"] == "a"
        assert result[1]["name"] == "c"

    def test_no_equals_sign_skipped(self):
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "a=b\nline_without_equals\nc=d"
        result = _parse_plain_cookie_lines(text)
        assert len(result) == 2

    def test_empty_name_skipped(self):
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "=value_only\na=b"
        result = _parse_plain_cookie_lines(text)
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_whitespace_stripped_around_name_and_value(self):
        from ai_browser.browser_session.session import _parse_plain_cookie_lines
        text = "  name  =  value with spaces  "
        result = _parse_plain_cookie_lines(text)
        assert result[0] == {"name": "name", "value": "value with spaces"}


class TestApplyCookiesFilePlainFormat:
    """Integration tests for _apply_cookies_file with plain-text format."""

    @pytest.mark.asyncio
    async def test_plain_text_format_detected_and_domain_applied(self, tmp_path, caplog):
        """Plain-text file gets detected as 'plain text (name=value)' and
        default_domain is applied to every cookie."""
        import logging
        from unittest.mock import AsyncMock, MagicMock
        from ai_browser.browser_session import BrowserSession, BrowserSessionConfig

        config = BrowserSessionConfig(
            authorized_hostname="example.com",
            cookies_file=tmp_path / "cookies.txt",
            cookies_domain=".example.com",
        )
        cookie_file = config.cookies_file
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_file.write_text("session_id=abc123\ncsrf=def456")

        session = BrowserSession(config)
        session._context = MagicMock()
        session._context.add_cookies = AsyncMock()

        with caplog.at_level(logging.INFO):
            await session._apply_cookies_file(cookie_file, default_domain=".example.com")

        call_args = session._context.add_cookies.call_args[0][0]
        assert len(call_args) == 2
        for c in call_args:
            assert c["domain"] == ".example.com"
            assert c["path"] == "/"
        assert "detected format: plain text" in caplog.text

    @pytest.mark.asyncio
    async def test_plain_text_no_default_domain_raises(self, tmp_path):
        """Plain-text cookies with no default_domain → ValueError naming
        the cookies that are missing a domain."""
        from unittest.mock import AsyncMock, MagicMock
        from ai_browser.browser_session import BrowserSession, BrowserSessionConfig

        config = BrowserSessionConfig(
            authorized_hostname="example.com",
            cookies_file=tmp_path / "cookies.txt",
        )
        cookie_file = config.cookies_file
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_file.write_text("session_id=abc123\ncsrf=def456")

        session = BrowserSession(config)
        session._context = MagicMock()
        session._context.add_cookies = AsyncMock()

        with pytest.raises(ValueError, match="session_id"):
            await session._apply_cookies_file(cookie_file, default_domain=None)

    @pytest.mark.asyncio
    async def test_json_format_still_works_and_logs_json(self, tmp_path, caplog):
        """JSON file is detected as JSON, plain-text parser is never invoked."""
        import logging
        from unittest.mock import AsyncMock, MagicMock
        from ai_browser.browser_session import BrowserSession, BrowserSessionConfig

        config = BrowserSessionConfig(
            authorized_hostname="example.com",
            cookies_file=tmp_path / "cookies.json",
        )
        cookie_file = config.cookies_file
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_file.write_text(
            '[{"name": "s", "value": "v", "domain": ".example.com", "path": "/"}]'
        )

        session = BrowserSession(config)
        session._context = MagicMock()
        session._context.add_cookies = AsyncMock()

        with caplog.at_level(logging.INFO):
            await session._apply_cookies_file(cookie_file, default_domain=None)

        call_args = session._context.add_cookies.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]["name"] == "s"
        assert "detected format: JSON" in caplog.text

    @pytest.mark.asyncio
    async def test_invalid_file_neither_json_nor_plain_text(self, tmp_path):
        """A file that's not JSON and has no name=value lines raises ValueError."""
        from unittest.mock import AsyncMock, MagicMock
        from ai_browser.browser_session import BrowserSession, BrowserSessionConfig

        config = BrowserSessionConfig(
            authorized_hostname="example.com",
            cookies_file=tmp_path / "bad.txt",
        )
        cookie_file = config.cookies_file
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_file.write_text("not json and also no equals sign anywhere")

        session = BrowserSession(config)
        session._context = MagicMock()
        session._context.add_cookies = AsyncMock()

        with pytest.raises(ValueError, match="neither valid JSON"):
            await session._apply_cookies_file(cookie_file, default_domain=".example.com")


class TestLoginHandlerAuthenticated:
    """Test LoginHandler.authenticated detection logic."""

    def test_authenticated_starts_none(self):
        handler = LoginHandler(LoginConfig(
            login_url="https://example.com/login",
            email="test@example.com",
            password="pw",
        ))
        assert handler.authenticated is None

    @pytest.mark.asyncio
    async def test_url_change_plus_new_cookies_returns_true(self):
        """Deterministic check: URL changed away from login + new cookies → True."""
        handler = LoginHandler(LoginConfig(
            login_url="https://example.com/login",
            email="test@example.com",
            password="pw",
            use_ai_judge=False,
        ))
        page = AsyncMock()
        page.url = "https://example.com/dashboard"

        async def fake_cookies(urls=None):
            return [{"name": "session", "value": "abc", "domain": ".example.com", "path": "/"}]
        page.context = AsyncMock()
        page.context.cookies = AsyncMock(side_effect=fake_cookies)

        result = await handler._check_authenticated(
            page, "https://example.com/login", set(),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_still_on_login_no_new_cookies_returns_false(self):
        """Deterministic: same URL, no new cookies → False."""
        handler = LoginHandler(LoginConfig(
            login_url="https://example.com/login",
            email="test@example.com",
            password="pw",
            use_ai_judge=False,
        ))
        page = AsyncMock()
        page.url = "https://example.com/login"

        page.context = AsyncMock()
        page.context.cookies = AsyncMock(return_value=[
            {"name": "session", "value": "abc", "domain": ".example.com", "path": "/"},
        ])

        result = await handler._check_authenticated(
            page, "https://example.com/login", {"session"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_ambiguous_falls_back_to_none_without_ai_judge(self):
        """URL changed but no new cookies → ambiguous, returns None without AI."""
        handler = LoginHandler(LoginConfig(
            login_url="https://example.com/login",
            email="test@example.com",
            password="pw",
            use_ai_judge=False,
        ))
        page = AsyncMock()
        page.url = "https://example.com/dashboard"
        page.context = AsyncMock()
        page.context.cookies = AsyncMock(return_value=[])

        result = await handler._check_authenticated(
            page, "https://example.com/login", set(),
        )
        assert result is None

    def test_discover_login_url_finds_exact_match(self):
        from ai_browser.login_handler.handler import _discover_login_url
        result = _discover_login_url([
            "https://example.com/about",
            "https://example.com/login",
            "https://example.com/contact",
        ])
        assert result == "https://example.com/login"

    def test_discover_login_url_finds_signin(self):
        from ai_browser.login_handler.handler import _discover_login_url
        result = _discover_login_url([
            "https://example.com/sign-in",
        ])
        assert result == "https://example.com/sign-in"

    def test_discover_login_url_returns_none_for_no_match(self):
        from ai_browser.login_handler.handler import _discover_login_url
        result = _discover_login_url([
            "https://example.com/about",
            "https://example.com/contact",
        ])
        assert result is None


class TestLoginConfigNewFields:
    """Test the new LoginConfig fields."""

    def test_candidate_endpoints_defaults_empty(self):
        config = LoginConfig(
            login_url="https://example.com/login",
            email="test@example.com",
            password="pw",
        )
        assert config.candidate_endpoints == []

    def test_ai_judge_defaults_true(self):
        config = LoginConfig(
            login_url="https://example.com/login",
            email="test@example.com",
            password="pw",
        )
        assert config.use_ai_judge is True

    def test_llm_fields_have_defaults(self):
        config = LoginConfig(
            login_url="https://example.com/login",
            email="test@example.com",
            password="pw",
        )
        assert config.llm_provider == "anthropic"
        assert config.llm_api_key == ""


class TestPhase0NameErrorRegression:
    """Regression: login_authenticated must be initialized before Phase 0
    so it never raises NameError when --login is not passed."""

    @pytest.mark.asyncio
    async def test_no_login_no_name_error(self):
        """Run _run_crawl with do_login=False — must not raise NameError."""
        import asyncio as asyncio_mod
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from ai_browser.cli import _run_crawl
        from ai_browser.browser_session import BrowserSessionConfig

        session_config = BrowserSessionConfig(
            authorized_hostname="example.com",
            expose_cdp=False,
        )
        crawl_config = MagicMock()
        crawl_config.start_url = "https://example.com"

        # Mock BrowserSession to avoid launching a real browser
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.pages = []

        with patch("ai_browser.cli.BrowserSession", return_value=mock_session):
            with patch("ai_browser.cli.TrafficCapture", return_value=MagicMock()):
                with patch("ai_browser.cli.Crawler") as mock_crawler_cls:
                    mock_crawler = MagicMock()
                    mock_result = MagicMock()
                    mock_result.endpoints = []
                    mock_result.total_pages_crawled = 0
                    mock_result.total_links_discovered = 0
                    mock_result.total_js_endpoints = 0
                    mock_result.unique_urls = 0
                    mock_result.errors = []
                    mock_crawler.run = AsyncMock(return_value=mock_result)
                    mock_crawler_cls.return_value = mock_crawler

                    # This must NOT raise NameError
                    await _run_crawl(
                        session_config=session_config,
                        crawl_config=crawl_config,
                        seed_visited=None,
                        prior_endpoints=[],
                        run_agent=False,
                        agent_backend="custom",
                        max_actions=20,
                        agent_task=None,
                        llm_provider="anthropic",
                        llm_model=None,
                        llm_api_key=None,
                        llm_base_url=None,
                        llm_max_tokens=None,
                        do_register=False,
                        register_email=None,
                        register_password="pw",
                        register_name="Test",
                        signup_url=None,
                        login_verify_url=None,
                        do_login=False,  # <-- this is the key: no login
                        login_email=None,
                        login_password=None,
                        imap_host=None,
                        imap_port=993,
                        imap_username=None,
                        imap_password=None,
                        email_timeout=120,
                        output_file=None,
                        no_crawl=False,
                        hostname="example.com",
                        scope_pattern="example.com",
                        traffic_dir=None,
                        no_traffic_capture=True,
                        email_backend="imap",
                        disposable_inbox_api_key=None,
                        disposable_inbox_domain=None,
                        cookies_file=None,
                    )
        # If we got here without a NameError, the fix works
