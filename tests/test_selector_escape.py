"""Tests for CSS selector escaping (Fix #5)."""

from ai_browser.agent_explorer.explorer import _escape_css_string
from ai_browser.registration_handler.handler import _escape_css_string as _handler_escape


class TestCSSEscape:
    def test_escapes_single_quote(self):
        result = _escape_css_string("O'Brien")
        # The single quote should be backslash-escaped
        assert "\\'" in result
        assert result.endswith("Brien")

    def test_escapes_backslash(self):
        result = _escape_css_string("path\\to")
        # Backslash should be doubled
        assert result.count("\\\\") >= 1

    def test_handles_apostrophe_in_target(self):
        """Target strings with apostrophes are properly escaped for CSS."""
        result = _escape_css_string("What's New")
        assert "What" in result
        assert "New" in result
        assert "\\'" in result  # quote is backslash-escaped

    def test_preserves_normal_string(self):
        assert _escape_css_string("normal") == "normal"

    def test_handler_escape_same_behavior(self):
        assert "\\'" in _handler_escape("O'Brien")
        assert _handler_escape("normal") == "normal"
