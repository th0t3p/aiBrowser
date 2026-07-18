"""Tests for human-confirmation fail-closed default (Fix #3)."""

from unittest.mock import AsyncMock

import pytest

from ai_browser.agent_explorer import AgentExplorer, ExplorerConfig


class TestConfirmationFailClosed:
    """Test that _needs_confirmation and _request_confirmation default to fail-closed."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            anthropic_api_key="sk-ant-fake",
            **kwargs,
        )

    # ------------------------------------------------------------------
    # _needs_confirmation — defaults to requiring confirmation for borderline
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_borderline_save_requires_confirmation_by_default(self):
        """'Save' action requires confirmation when allow_unattended=False (default)."""
        config = self._make_config()  # allow_unattended defaults to False
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "Save Settings", "reasoning": "save state"}
        assert explorer._needs_confirmation(action) is True

    @pytest.mark.asyncio
    async def test_borderline_confirm_requires_confirmation_by_default(self):
        """'Confirm' action requires confirmation by default."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "Confirm Changes", "reasoning": "..."}
        assert explorer._needs_confirmation(action) is True

    @pytest.mark.asyncio
    async def test_borderline_update_requires_confirmation_by_default(self):
        """'Update' action requires confirmation by default."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "Update Profile", "reasoning": "..."}
        assert explorer._needs_confirmation(action) is True

    @pytest.mark.asyncio
    async def test_borderline_submit_requires_confirmation_by_default(self):
        """'Submit' action requires confirmation by default."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        action = {"action": "submit", "target": "Submit Form", "reasoning": "..."}
        assert explorer._needs_confirmation(action) is True

    @pytest.mark.asyncio
    async def test_innocuous_action_does_not_need_confirmation(self):
        """Innocuous actions like 'View Products' don't trigger confirmation."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "View Products", "reasoning": "explore"}
        assert explorer._needs_confirmation(action) is False

    # ------------------------------------------------------------------
    # allow_unattended=True skips confirmation
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_allow_unattended_skips_confirmation(self):
        """When allow_unattended=True, borderline actions skip confirmation."""
        config = self._make_config(allow_unattended=True)
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "Save Settings", "reasoning": "save state"}
        assert explorer._needs_confirmation(action) is False

    # ------------------------------------------------------------------
    # _request_confirmation — fail-closed when no callback
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_request_confirmation_denies_when_no_callback(self):
        """Without a callback, _request_confirmation returns False (deny)."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        action = {"action": "click", "target": "Save", "reasoning": "save"}
        result = await explorer._request_confirmation(action)
        assert result is False

    @pytest.mark.asyncio
    async def test_request_confirmation_uses_callback_when_set(self):
        """When a callback is set, _request_confirmation uses it."""
        config = self._make_config()
        explorer = AgentExplorer(config)

        callback = AsyncMock(return_value=True)
        explorer.set_confirmation_callback(callback)

        action = {"action": "click", "target": "Save", "reasoning": "save"}
        result = await explorer._request_confirmation(action)
        assert result is True
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_confirmation_callback_can_deny(self):
        """A callback returning False denies the action."""
        config = self._make_config()
        explorer = AgentExplorer(config)

        callback = AsyncMock(return_value=False)
        explorer.set_confirmation_callback(callback)

        action = {"action": "click", "target": "Save", "reasoning": "save"}
        result = await explorer._request_confirmation(action)
        assert result is False
