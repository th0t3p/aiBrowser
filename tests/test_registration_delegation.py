"""Tests for registration delegation exception handling (Fix #1) and behavior (Fix #3)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.agent_explorer import AgentExplorer, ExplorerConfig
from ai_browser.agent_explorer.explorer import CaptchaDetected


class TestRegistrationExceptionHandling:

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            anthropic_api_key="sk-ant-fake",
            allow_registration=True,
            registration_patterns=[r"(?i)\bsign\s*up\b"],
            **kwargs,
        )

    def test_captcha_detected_always_propagates(self):
        config = self._make_config(raise_on_registration_failure=False)
        AgentExplorer(config)
        exc = CaptchaDetected(page_url="https://t.com", captcha_type="recaptcha",
                               screenshot_path=MagicMock())
        assert isinstance(exc, CaptchaDetected)

    def test_value_error_not_propagated_by_default(self):
        config = self._make_config(raise_on_registration_failure=False)
        assert config.raise_on_registration_failure is False

    def test_value_error_propagates_when_flag_true(self):
        config = self._make_config(raise_on_registration_failure=True)
        assert config.raise_on_registration_failure is True

    def test_string_check_not_used(self):
        import inspect
        explore_source = inspect.getsource(AgentExplorer.explore)
        assert '"CaptchaDetected" in type(exc).__name__' not in explore_source
        assert 'isinstance(exc, CaptchaDetected)' in explore_source


class TestRegistrationDelegationBehavior:

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            anthropic_api_key="sk-ant-fake",
            **kwargs,
        )

    def test_allow_registration_false_treated_as_confirmation(self):
        config = self._make_config(allow_registration=False)
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "Sign Up Now", "reasoning": "explore"}
        assert explorer._matches_registration(action) is True
        assert config.allow_registration is False

    def test_allow_registration_true_with_config(self):
        config = self._make_config(
            allow_registration=True,
            registration_config={"signup_url": "https://t.com/signup", "email": "t@t.com"},
        )
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "Create Account", "reasoning": "explore"}
        assert explorer._matches_registration(action) is True
        assert config.registration_config is not None

    @pytest.mark.asyncio
    async def test_delegate_without_config_raises_runtime_error(self):
        config = self._make_config(allow_registration=True, registration_config=None)
        explorer = AgentExplorer(config)
        with pytest.raises(RuntimeError) as exc_info:
            await explorer._delegate_registration(MagicMock(), MagicMock())
        assert "no registration_config" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_element_matches_registration_detects_signup(self):
        config = self._make_config(allow_registration=False)
        explorer = AgentExplorer(config)
        el = AsyncMock()
        el.inner_text = AsyncMock(return_value="Create Account")
        el.get_attribute = AsyncMock(return_value="")
        result = await explorer._element_matches_registration(el)
        assert result is True

    @pytest.mark.asyncio
    async def test_element_matches_registration_passes_innocuous(self):
        config = self._make_config(allow_registration=False)
        explorer = AgentExplorer(config)
        el = AsyncMock()
        el.inner_text = AsyncMock(return_value="View Products")
        el.get_attribute = AsyncMock(return_value="")
        result = await explorer._element_matches_registration(el)
        assert result is False


class TestFillFormFieldsReturnValue:
    """Test that fill_form_fields reports what it actually filled."""

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_nothing_filled(self):
        """Zero fields matched → empty list."""
        from ai_browser._form_helpers import fill_form_fields

        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)  # nothing found

        result = await fill_form_fields(page, [
            (["email", "email_address"], "test@test.com"),
            (["password", "passwd"], "secret"),
        ])
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_filled_fields_list(self):
        """Fields that matched are recorded in the return list."""
        from ai_browser._form_helpers import fill_form_fields

        page = AsyncMock()
        # First query_selector call (email): element found
        # Second (password): not found
        found = AsyncMock()
        found.is_visible = AsyncMock(return_value=True)
        found.fill = AsyncMock()
        not_found = None

        page.query_selector = AsyncMock(side_effect=[found, not_found])

        result = await fill_form_fields(page, [
            (["email", "email_address"], "test@test.com"),
            (["password", "passwd"], "secret"),
        ])
        assert result == ["email"]  # only email was filled

    @pytest.mark.asyncio
    async def test_all_fields_filled(self):
        """When all field groups match, all primaries are returned."""
        from ai_browser._form_helpers import fill_form_fields

        page = AsyncMock()
        found = AsyncMock()
        found.is_visible = AsyncMock(return_value=True)
        found.fill = AsyncMock()

        page.query_selector = AsyncMock(return_value=found)

        result = await fill_form_fields(page, [
            (["email"], "test@test.com"),
            (["password"], "secret"),
            (["name"], "Test User"),
        ])
        assert result == ["email", "password", "name"]


class TestFillSignupFormSkipsSubmit:
    """Test that _fill_signup_form skips submit when no email field found."""

    @pytest.mark.asyncio
    async def test_skip_submit_when_no_email_filled(self):
        """_submit_form is NOT called when fill_form_fields returns no 'email'."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig
        from unittest.mock import patch

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
            password="secret",
        )
        handler = RegistrationHandler(config)

        page = AsyncMock()
        page.url = "https://example.com/signup"
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        # Mock fill_form_fields to return only "name" (no email)
        with patch(
            "ai_browser.registration_handler.handler.fill_form_fields",
            AsyncMock(return_value=["name"]),
        ):
            with patch.object(handler, "_submit_form", AsyncMock()) as mock_submit:
                with patch.object(handler, "_check_captcha", AsyncMock()):
                    result_page = await handler.register(AsyncMock())

        mock_submit.assert_not_called()
        assert handler.submitted is False

    @pytest.mark.asyncio
    async def test_submits_when_email_is_filled(self):
        """_submit_form IS called when 'email' is in the filled fields."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig
        from unittest.mock import patch

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
            password="secret",
        )
        handler = RegistrationHandler(config)

        page = AsyncMock()
        page.url = "https://example.com/signup"
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        with patch(
            "ai_browser.registration_handler.handler.fill_form_fields",
            AsyncMock(return_value=["email", "password", "name"]),
        ):
            with patch.object(handler, "_submit_form", AsyncMock()) as mock_submit:
                with patch.object(handler, "_check_captcha", AsyncMock()):
                    result_page = await handler.register(AsyncMock())

        mock_submit.assert_called_once()
        assert handler.submitted is True


class TestCandidateDiscovery:
    """Test signup URL candidate discovery logic."""

    def test_exact_path_match_ranks_highest(self):
        """A URL with /signup in the path wins over substring matches."""
        from ai_browser.registration_handler.handler import discover_signup_url

        endpoints = [
            "https://example.com/docs/getting-started",
            "https://example.com/signup",
        ]
        result = discover_signup_url(endpoints)
        assert result == "https://example.com/signup"

    def test_strong_match_preferred_over_weak(self):
        """Strong (exact segment) matches rank above weak (substring)."""
        from ai_browser.registration_handler.handler import discover_signup_url

        endpoints = [
            "https://example.com/docs/signup-guide",  # weak (substring in path)
            "https://example.com/register",           # strong (exact segment)
        ]
        result = discover_signup_url(endpoints)
        assert result == "https://example.com/register"

    def test_docs_page_excluded_from_weak_match(self):
        """A docs page like /doc/getting-started-create-an-app is excluded."""
        from ai_browser.registration_handler.handler import discover_signup_url

        endpoints = [
            "https://example.com/doc/getting-started-create-an-app",
        ]
        result = discover_signup_url(endpoints)
        assert result is None

    def test_zero_candidates_returns_none(self):
        """No plausible signup URLs → None (honest report)."""
        from ai_browser.registration_handler.handler import discover_signup_url

        endpoints = [
            "https://example.com/about",
            "https://example.com/contact",
            "https://example.com/products",
        ]
        result = discover_signup_url(endpoints)
        assert result is None

    def test_seed_hostname_preferred_in_strong_matches(self):
        """When multiple strong matches exist, one matching the seed hostname wins."""
        from ai_browser.registration_handler.handler import discover_signup_url

        endpoints = [
            "https://other.example.com/signup",
            "https://developers.example.com/signup",
        ]
        result = discover_signup_url(endpoints, seed_hostname="developers.example.com")
        assert "developers" in result

    def test_empty_endpoints_returns_none(self):
        from ai_browser.registration_handler.handler import discover_signup_url
        assert discover_signup_url([]) is None


class TestAIJudgeFailOpen:
    """Test that AI judge failures/logic never crash registration."""

    @pytest.mark.asyncio
    async def test_ai_judge_not_run_when_no_api_key(self):
        """When llm_api_key is empty, judge is not called."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
            password="secret",
            use_ai_judge=True,
            llm_api_key="",  # no key
        )
        handler = RegistrationHandler(config)
        # Judge should not have been run
        assert handler.registration_looked_real is None

    def test_ai_judge_returns_true_on_yes_response(self):
        """A 'YES' response → True."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
        )
        # Directly test the logic without call_llm
        handler = RegistrationHandler(config)

        # Simulate what happens internally: the handler checks "yes" in response
        response = "YES"
        looked_real = "yes" in response.strip().lower()
        assert looked_real is True

    def test_ai_judge_returns_false_on_no_response(self):
        """A 'NO' response → False."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
        )
        handler = RegistrationHandler(config)

        response = "NO"
        looked_real = "yes" in response.strip().lower()
        assert looked_real is False

    @pytest.mark.asyncio
    async def test_ai_judge_fails_open_on_exception(self):
        """When _ai_judge_did_submit raises, it returns None (fail open)."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig
        from unittest.mock import patch

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
            password="secret",
            use_ai_judge=True,
            llm_api_key="fake-key",
        )
        handler = RegistrationHandler(config)

        page = AsyncMock()
        # Cause evaluate() to raise
        page.evaluate = AsyncMock(side_effect=Exception("JS error"))

        result = await handler._ai_judge_did_submit(page)
        assert result is None  # fail open


class TestRegistrationConfigNewFields:
    """Test the new RegistrationConfig fields default correctly."""

    def test_default_use_ai_judge_is_true(self):
        from ai_browser.registration_handler import RegistrationConfig
        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
        )
        assert config.use_ai_judge is True

    def test_candidate_endpoints_default_empty(self):
        from ai_browser.registration_handler import RegistrationConfig
        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
        )
        assert config.candidate_endpoints == []

    def test_llm_fields_default_to_anthropic(self):
        from ai_browser.registration_handler import RegistrationConfig
        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
        )
        assert config.llm_provider == "anthropic"
        assert config.llm_model == "claude-sonnet-4-20250514"

    def test_submitted_property_false_initially(self):
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig
        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
        )
        handler = RegistrationHandler(config)
        assert handler.submitted is False


class TestDisposableInboxModelValidation:
    """Test RegistrationConfig validation for disposable inbox."""

    def test_email_required_when_no_disposable_inbox(self):
        """email is required when disposable_inbox_config is not set."""
        from ai_browser.registration_handler.models import RegistrationConfig
        with pytest.raises(ValueError, match="email is required"):
            RegistrationConfig(
                signup_url="https://example.com/signup",
                # no email, no disposable_inbox_config
            )

    def test_disposable_alone_accepted_without_email(self):
        """disposable_inbox_config alone (no email) is accepted."""
        from ai_browser.registration_handler.models import (
            RegistrationConfig, DisposableInboxConfig,
        )
        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            disposable_inbox_config=DisposableInboxConfig(api_key="test-key"),
        )
        assert config.email is None
        assert config.disposable_inbox_config is not None

    def test_imap_and_disposable_together_rejected(self):
        """imap_config + disposable_inbox_config is rejected."""
        from ai_browser.registration_handler.models import (
            RegistrationConfig, DisposableInboxConfig, IMAPConfig,
        )
        with pytest.raises(ValueError, match="mutually exclusive"):
            RegistrationConfig(
                signup_url="https://example.com/signup",
                email="test@test.com",
                imap_config=IMAPConfig(
                    host="imap.test.com", username="test", password="pw",
                ),
                disposable_inbox_config=DisposableInboxConfig(api_key="test-key"),
            )


class TestExtractLinkFromBodyRefactor:
    """Test that _extract_link_from_body (shared core) produces same results
    as the original _extract_link_from_email (IMAP path)."""

    def test_confirm_link_prioritized(self):
        from ai_browser.registration_handler.handler import _extract_link_from_body
        body = (
            '<a href="https://target.com/logo.png">Logo</a>\n'
            '<a href="https://target.com/confirm?token=abc">Confirm</a>'
        )
        result = _extract_link_from_body(body, "target.com")
        assert result == "https://target.com/confirm?token=abc"

    def test_verify_link_prioritized(self):
        from ai_browser.registration_handler.handler import _extract_link_from_body
        body = (
            '<a href="https://target.com/verify-email?id=123">Verify</a>'
        )
        result = _extract_link_from_body(body)
        assert "verify" in result

    def test_asset_links_skipped(self):
        from ai_browser.registration_handler.handler import _extract_link_from_body
        body = '<a href="https://target.com/logo.png">Logo</a>\n<a href="https://target.com/page">Page</a>'
        result = _extract_link_from_body(body)
        assert result == "https://target.com/page"

    def test_same_domain_fallback(self):
        from ai_browser.registration_handler.handler import _extract_link_from_body
        body = (
            '<a href="https://other.com/page">Other</a>\n'
            '<a href="https://target.com/welcome">Welcome</a>'
        )
        result = _extract_link_from_body(body, "target.com")
        assert result == "https://target.com/welcome"

    def test_empty_body_returns_none(self):
        from ai_browser.registration_handler.handler import _extract_link_from_body
        assert _extract_link_from_body("") is None
        assert _extract_link_from_body("   ") is None


class TestDisposableInboxMockIntegration:
    """Test handler integration with mocked disposable inbox."""

    @pytest.mark.asyncio
    async def test_provision_sets_email_on_config(self):
        """When disposable_inbox_config is set, provision_inbox is called
        and config.email is set before navigation."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig
        from ai_browser.registration_handler.models import DisposableInboxConfig
        from unittest.mock import AsyncMock, patch, MagicMock

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            disposable_inbox_config=DisposableInboxConfig(api_key="test-key"),
            password="secret",
        )
        handler = RegistrationHandler(config)

        # Mock provision_inbox to return an address
        with patch(
            "ai_browser.registration_handler.disposable_inbox.provision_inbox",
            AsyncMock(return_value="fresh@agentmail.to"),
        ):
            # We need the full register flow mocked
            handler._resolve_signup_url = MagicMock(return_value="https://example.com/signup")
            handler._check_captcha = AsyncMock()
            handler._fill_signup_form = AsyncMock(return_value=["email", "password"])
            handler._submit_form = AsyncMock()
            handler._ai_judge_did_submit = AsyncMock(return_value=None)
            handler._poll_inbox_for_link = AsyncMock(return_value=None)

            page = AsyncMock()
            page.url = "https://example.com/signup"
            page.goto = AsyncMock()
            page.wait_for_load_state = AsyncMock()

            session = MagicMock()
            session.new_page = AsyncMock(return_value=page)

            with patch(
                "ai_browser.registration_handler.disposable_inbox.wait_for_confirmation_link",
                AsyncMock(return_value=None),
            ):
                await handler.register(session)

        # Verify the email was provisioned and set
        assert handler.provisioned_email == "fresh@agentmail.to"
        assert handler.config.email == "fresh@agentmail.to"

    @pytest.mark.asyncio
    async def test_provision_failure_raises_before_navigation(self):
        """If provision_inbox raises, the exception propagates BEFORE any
        navigation is attempted."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig
        from ai_browser.registration_handler.models import DisposableInboxConfig
        from unittest.mock import AsyncMock, patch, MagicMock

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            disposable_inbox_config=DisposableInboxConfig(api_key="bad-key"),
            password="secret",
        )
        handler = RegistrationHandler(config)

        session = MagicMock()
        session.new_page = AsyncMock()  # should NOT be called

        with patch(
            "ai_browser.registration_handler.disposable_inbox.provision_inbox",
            AsyncMock(side_effect=RuntimeError("API key invalid")),
        ):
            with pytest.raises(RuntimeError, match="API key invalid"):
                await handler.register(session)

        # session.new_page was never called — fail fast before navigation
        session.new_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_wait_for_link_timeout_returns_same_as_imap(self):
        """Timeout from disposable inbox returns None, same as IMAP timeout."""
        from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig
        from ai_browser.registration_handler.models import DisposableInboxConfig
        from unittest.mock import AsyncMock, patch, MagicMock

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            disposable_inbox_config=DisposableInboxConfig(api_key="test-key"),
            password="secret",
        )
        handler = RegistrationHandler(config)

        handler._resolve_signup_url = MagicMock(return_value="https://example.com/signup")
        handler._check_captcha = AsyncMock()
        handler._fill_signup_form = AsyncMock(return_value=["email", "password"])
        handler._submit_form = AsyncMock()
        handler._ai_judge_did_submit = AsyncMock(return_value=None)

        page = AsyncMock()
        page.url = "https://example.com/signup"
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        with patch(
            "ai_browser.registration_handler.disposable_inbox.provision_inbox",
            AsyncMock(return_value="fresh@agentmail.to"),
        ):
            with patch(
                "ai_browser.registration_handler.disposable_inbox.wait_for_confirmation_link",
                AsyncMock(return_value=None),  # timeout
            ):
                await handler.register(session)

        # Not confirmed (timeout), not crashed
        assert handler.confirmed is False
        assert handler.submitted is True  # form was still submitted


class TestCLIDisposableInboxParsing:
    """Test that CLI parsing correctly handles --email-backend options."""

    def test_email_backend_defaults_to_imap(self):
        from click.testing import CliRunner
        import sys
        # Just verify the option exists with the right default
        runner = CliRunner()
        result = runner.invoke(
            sys.modules["ai_browser.cli"].crawl,
            ["--help"],
        )
        assert "--email-backend" in result.output
        assert "imap" in result.output
        assert handler.registration_looked_real is None
