"""Tests for --start-url resolution and integration."""

from __future__ import annotations

import click
import pytest

from ai_browser.cli import _resolve_start_url


# ---------------------------------------------------------------------------
# _resolve_start_url — unit tests
# ---------------------------------------------------------------------------

class TestResolveStartUrl:
    """Pure-function tests for _resolve_start_url."""

    def test_none_returns_default(self):
        """When start_url_opt is None, return f'https://{hostname}'."""
        result = _resolve_start_url(None, "example.com", "example.com")
        assert result == "https://example.com"

    def test_full_url_returned_unchanged(self):
        """Full URL with scheme is returned unchanged."""
        result = _resolve_start_url(
            "https://example.com/portal", "example.com", "example.com",
        )
        assert result == "https://example.com/portal"

    def test_url_without_scheme_prepends_https(self):
        """Missing scheme → https:// is prepended."""
        result = _resolve_start_url(
            "example.com/portal", "example.com", "example.com",
        )
        assert result == "https://example.com/portal"

    def test_different_host_raises_click_exception(self):
        """Cross-host start URL raises ClickException."""
        with pytest.raises(click.ClickException, match="does not match"):
            _resolve_start_url(
                "https://evil.com/x", "example.com", "example.com",
            )

    def test_out_of_scope_host_rejected(self):
        """Host outside scope raises ClickException."""
        with pytest.raises(click.ClickException, match="not within"):
            _resolve_start_url(
                "https://other.com/x",
                "other.com",
                "example.com",  # scope only covers example.com
            )

    def test_same_host_within_scope_passes(self):
        """Host matches and is within scope — should pass."""
        result = _resolve_start_url(
            "https://example.com/deep/page", "example.com",
            "example.com",
        )
        assert result == "https://example.com/deep/page"

    @pytest.mark.parametrize("hostname", ["example.com", "mysite.io", "target.org"])
    def test_default_backward_compat(self, hostname):
        """Absent --start-url, behavior is byte-for-byte unchanged."""
        result = _resolve_start_url(None, hostname, hostname)
        expected = f"https://{hostname}"
        assert result == expected

    def test_default_no_scope_validation(self):
        """Default (None) doesn't validate scope — just builds the URL."""
        result = _resolve_start_url(None, "example.com", "other.com")
        assert result == "https://example.com"

    def test_url_with_path_and_query_preserved(self):
        """Path and query components are preserved through resolution."""
        result = _resolve_start_url(
            "https://example.com/api/v2?x=1&y=2",
            "example.com", "example.com",
        )
        assert result == "https://example.com/api/v2?x=1&y=2"

    def test_trailing_whitespace_stripped(self):
        """Whitespace around the URL is stripped."""
        result = _resolve_start_url(
            "  https://example.com/portal  ", "example.com", "example.com",
        )
        assert result == "https://example.com/portal"
