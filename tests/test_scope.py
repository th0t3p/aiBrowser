"""Tests for glob-pattern scope matching and AgentExplorer scope verification (Fix #4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser._scope import hostname_matches_scope, page_url_matches_scope, ScopeError
from ai_browser.agent_explorer import AgentExplorer, ExplorerConfig


class TestHostnameMatchesScope:
    """Test the shared glob-pattern scope matching utility."""

    def test_exact_match(self):
        assert hostname_matches_scope("example.com", "example.com") is True

    def test_case_insensitive_match(self):
        assert hostname_matches_scope("Example.COM", "example.com") is True

    def test_wildcard_subdomain(self):
        """``*.example.com`` matches any subdomain, at any depth."""
        assert hostname_matches_scope("app.example.com", "*.example.com") is True
        assert hostname_matches_scope("api.example.com", "*.example.com") is True
        # fnmatch treats * as matching any characters including dots,
        # so *.example.com also matches deeper subdomains
        assert hostname_matches_scope("a.b.example.com", "*.example.com") is True

    def test_wildcard_matches_exact_too(self):
        """``*.example.com`` does NOT match example.com itself (fnmatch semantics)."""
        assert hostname_matches_scope("example.com", "*.example.com") is False

    def test_question_mark_wildcard(self):
        assert hostname_matches_scope("cdn1.example.com", "cdn?.example.com") is True
        assert hostname_matches_scope("cdn12.example.com", "cdn?.example.com") is False

    def test_unrelated_hostname_rejected(self):
        assert hostname_matches_scope("evil.com", "example.com") is False
        assert hostname_matches_scope("example.com.evil.com", "example.com") is False

    def test_empty_inputs(self):
        assert hostname_matches_scope("", "example.com") is False
        assert hostname_matches_scope("example.com", "") is False

    def test_page_url_matches_scope(self):
        assert page_url_matches_scope("https://app.example.com/page", "*.example.com") is True
        assert page_url_matches_scope("https://evil.com/page", "*.example.com") is False


class TestAgentExplorerScopeVerification:
    """Test that AgentExplorer._verify_scope independently checks page hostname."""

    @staticmethod
    def _make_config(hostname="*.example.com"):
        return ExplorerConfig(
            authorized_hostname=hostname,
            anthropic_api_key="sk-ant-fake",
        )

    def test_verify_scope_allows_matching_subdomain(self):
        """Page on app.example.com passes verification when scope is *.example.com."""
        config = self._make_config("*.example.com")
        explorer = AgentExplorer(config)
        page = MagicMock()
        page.url = "https://app.example.com/dashboard"
        explorer._verify_scope(page)  # should not raise

    def test_verify_scope_rejects_unrelated_hostname(self):
        """Page on evil.com fails verification when scope is *.example.com."""
        config = self._make_config("*.example.com")
        explorer = AgentExplorer(config)
        page = MagicMock()
        page.url = "https://evil.com/page"
        with pytest.raises(ScopeError):
            explorer._verify_scope(page)

    def test_verify_scope_exact_match(self):
        """Page on example.com passes when scope is example.com exactly."""
        config = self._make_config("example.com")
        explorer = AgentExplorer(config)
        page = MagicMock()
        page.url = "https://example.com/home"
        explorer._verify_scope(page)  # should not raise

    def test_verify_scope_rejects_subdomain_when_exact_match(self):
        """Page on app.example.com fails when scope is example.com (exact only)."""
        config = self._make_config("example.com")
        explorer = AgentExplorer(config)
        page = MagicMock()
        page.url = "https://app.example.com/page"
        with pytest.raises(ScopeError):
            explorer._verify_scope(page)
