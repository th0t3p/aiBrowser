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
        """fill_form_fields used by both handlers is the identical function object."""
        from ai_browser.login_handler import handler as lh
        from ai_browser.registration_handler import handler as rh
        # Both import fill_form_fields from _form_helpers
        assert lh.fill_form_fields is fill_form_fields
        assert rh.fill_form_fields is fill_form_fields
        assert lh.fill_form_fields is rh.fill_form_fields

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
