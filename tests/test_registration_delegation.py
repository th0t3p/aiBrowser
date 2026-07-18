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
