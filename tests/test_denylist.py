"""Tests for the denylist checking actual DOM element text (Fix #2)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.agent_explorer import AgentExplorer, ExplorerConfig


class TestDenylistElementCheck:
    """Test that denylist gates on actual resolved DOM element text, not just LLM self-report."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            anthropic_api_key="sk-ant-fake",
            **kwargs,
        )

    @staticmethod
    def _make_mock_element(inner_text="", aria_label="", visible=True):
        """Return a mock Playwright ElementHandle."""
        el = AsyncMock()
        el.inner_text = AsyncMock(return_value=inner_text)
        el.get_attribute = AsyncMock(return_value=aria_label)
        el.is_visible = AsyncMock(return_value=visible)
        el.click = AsyncMock()
        el.fill = AsyncMock()
        return el

    @staticmethod
    def _make_mock_page():
        page = AsyncMock()
        page.url = "https://example.com"
        page.evaluate = AsyncMock(return_value="")
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.wait_for_load_state.return_value = None
        return page

    # ------------------------------------------------------------------
    # _element_matches_denylist
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_element_matches_denylist_detects_destructive_text(self):
        """Element with 'Delete Account' inner text matches denylist."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        el = self._make_mock_element(inner_text="Delete Account")

        result = await explorer._element_matches_denylist(el)
        assert result is True

    @pytest.mark.asyncio
    async def test_element_matches_denylist_detects_aria_label(self):
        """Element with destructive aria-label matches denylist."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        el = self._make_mock_element(inner_text="OK", aria_label="Cancel Subscription")

        result = await explorer._element_matches_denylist(el)
        assert result is True

    @pytest.mark.asyncio
    async def test_element_matches_denylist_passes_innocuous(self):
        """Innocuous element text does not match denylist."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        el = self._make_mock_element(inner_text="View Profile", aria_label="View Profile")

        result = await explorer._element_matches_denylist(el)
        assert result is False

    @pytest.mark.asyncio
    async def test_element_matches_denylist_detects_checkout(self):
        """Element with 'Checkout' text matches denylist."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        el = self._make_mock_element(inner_text="Proceed to Checkout")

        result = await explorer._element_matches_denylist(el)
        assert result is True

    # ------------------------------------------------------------------
    # _do_click — blocks when actual element text matches, even if LLM says "OK"
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_do_click_blocks_destructive_element(self):
        """Click is blocked when the resolved element text is destructive,
        even though the LLM's reported target string is innocuous."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        page = self._make_mock_page()

        # The LLM says target="OK Button", which passes _is_denied()
        # But the actual element's inner text is "Delete Account"
        destructive_el = self._make_mock_element(
            inner_text="Delete Account", aria_label=""
        )
        page.query_selector = AsyncMock(return_value=destructive_el)

        result = await explorer._do_click(page, "OK Button")
        assert result is False  # blocked by element-level check

        # Verify click was NEVER called
        destructive_el.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_do_click_allows_innocuous_element(self):
        """Click proceeds when both LLM text and actual element text are innocuous."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        page = self._make_mock_page()

        innocuous_el = self._make_mock_element(
            inner_text="View Products", aria_label="View Products"
        )
        page.query_selector = AsyncMock(return_value=innocuous_el)

        result = await explorer._do_click(page, "View Products")
        assert result is True
        innocuous_el.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_do_click_blocks_pay_now(self):
        """Click is blocked when element says 'Pay Now'."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        page = self._make_mock_page()

        pay_el = self._make_mock_element(inner_text="Pay Now", aria_label="")
        page.query_selector = AsyncMock(return_value=pay_el)

        result = await explorer._do_click(page, "Continue")
        assert result is False
        pay_el.click.assert_not_called()

    # ------------------------------------------------------------------
    # _do_submit — blocks destructive submit buttons
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_do_submit_blocks_if_button_destructive(self):
        """Submit via Enter is blocked when the focused element text is destructive."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        page = self._make_mock_page()

        # The ENTER path checks the focused element
        page.evaluate = AsyncMock(return_value="Confirm Purchase")
        page.query_selector = AsyncMock(return_value=None)  # no form found

        result = await explorer._do_submit(page, "")
        assert result is False  # blocked by focused-element check

    @pytest.mark.asyncio
    async def test_do_submit_blocks_destructive_submit_button(self):
        """Generic form submit is blocked when the submit button text is destructive."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        page = self._make_mock_page()

        destructive_btn = self._make_mock_element(
            inner_text="Cancel Subscription", aria_label=""
        )
        form_el = self._make_mock_element(inner_text="", aria_label="")

        # Make the Enter path fail so we reach the generic form submit path
        page.keyboard.press = AsyncMock(side_effect=Exception("no focused element"))
        # query_selector calls in generic form path:
        #   1. submit_btn selector → destructive_btn
        #   2. "form" selector → form_el
        page.query_selector = AsyncMock(side_effect=[destructive_btn, form_el])

        result = await explorer._do_submit(page, "")
        assert result is False
        destructive_btn.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_do_submit_allows_innocuous(self):
        """Submit proceeds when everything is innocuous."""
        config = self._make_config()
        explorer = AgentExplorer(config)
        page = self._make_mock_page()

        innocuous_btn = self._make_mock_element(inner_text="Submit", aria_label="")
        form_el = self._make_mock_element(inner_text="", aria_label="")
        form_el.evaluate = AsyncMock()

        # Make the Enter path fail so we reach generic form submit
        page.keyboard.press = AsyncMock(side_effect=Exception("no focused element"))
        page.query_selector = AsyncMock(side_effect=[innocuous_btn, form_el])

        result = await explorer._do_submit(page, "")
        assert result is True
        form_el.evaluate.assert_called_once_with("el => el.submit()")
