"""Tests for browser-use CLI integration helpers and safety guards."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from ai_browser.cli import _extract_urls_from_browser_use_result


# ---------------------------------------------------------------------------
# Helper: build a mock browser-use result with .urls()
# ---------------------------------------------------------------------------


def _make_mock_result(urls: list[str | None]) -> MagicMock:
    result = MagicMock()
    result.urls = MagicMock(return_value=urls)
    return result


# ---------------------------------------------------------------------------
# _extract_urls_from_browser_use_result
# ---------------------------------------------------------------------------


class TestExtractURLs:
    def test_none_input_returns_empty(self):
        assert _extract_urls_from_browser_use_result(None) == []

    def test_no_urls_method_returns_empty(self):
        obj = MagicMock(spec=[])  # no .urls() method
        assert _extract_urls_from_browser_use_result(obj) == []

    def test_empty_input_list(self):
        result = _make_mock_result([])
        assert _extract_urls_from_browser_use_result(result) == []

    def test_filters_none_entries(self):
        result = _make_mock_result([None, "https://example.com", None])
        assert _extract_urls_from_browser_use_result(result) == ["https://example.com"]

    def test_filters_empty_string(self):
        result = _make_mock_result(["", "https://example.com", ""])
        assert _extract_urls_from_browser_use_result(result) == ["https://example.com"]

    def test_filters_about_blank(self):
        result = _make_mock_result(["about:blank", "https://example.com", "about:blank"])
        assert _extract_urls_from_browser_use_result(result) == ["https://example.com"]

    def test_realistic_combined_shape(self):
        """The exact shape observed in the live run: leading empty string,
        real URLs, trailing about:blank, and a None."""
        result = _make_mock_result([
            "",                          # leading empty — observed
            "https://developers.tiktok.com",
            "https://developers.tiktok.com/doc/overview",
            "about:blank",               # initial nav state
            None,                        # failed step
        ])
        assert _extract_urls_from_browser_use_result(result) == [
            "https://developers.tiktok.com",
            "https://developers.tiktok.com/doc/overview",
        ]

    def test_does_not_dedupe(self):
        """Deduplication is the merge loop's job (via set()), not this function's."""
        result = _make_mock_result([
            "https://example.com/a",
            "https://example.com/a",  # duplicate — not filtered
            "https://example.com/b",
        ])
        assert _extract_urls_from_browser_use_result(result) == [
            "https://example.com/a",
            "https://example.com/a",
            "https://example.com/b",
        ]


# ---------------------------------------------------------------------------
# disable_security safety guard
# ---------------------------------------------------------------------------


class TestDisableSecurityNotSet:
    def test_disable_security_true_not_in_run_phase2_browser_use_source(self):
        """The literal assignment ``disable_security=True`` must never appear
        in _run_phase2_browser_use's source, since it's dead code that
        would silently activate if browser-use's context-reuse behavior
        ever changes during a version bump.

        The explanatory comment deliberately mentioning the name is fine
        — this test only checks for the enabling assignment.
        """
        from ai_browser.cli import _run_phase2_browser_use

        source = inspect.getsource(_run_phase2_browser_use)
        # Look for the enabling assignment specifically — the explanatory
        # comment uses the substring too, but without "= True".
        assert "disable_security=True" not in source, (
            "disable_security=True found in _run_phase2_browser_use source.  "
            "It is dead code in the CDP-reuse path and would silently "
            "activate if browser-use ever changes its context-reuse behavior.  "
            "Remove it and replace with the explanatory comment."
        )
