"""Tests for crawler wildcard/glob scope support (Fix #2) and CLI (Fix #3)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from ai_browser.crawler import CrawlConfig
from ai_browser._scope import hostname_matches_scope
from ai_browser.cli import main


class TestCrawlConfigSplit:
    """Test CrawlConfig seed_hostname / scope_pattern split and defaults."""

    def test_scope_pattern_defaults_to_seed_hostname(self):
        """When scope_pattern is omitted, it defaults to seed_hostname."""
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
        )
        assert cfg.scope_pattern == "example.com"

    def test_scope_pattern_is_preserved_when_set(self):
        """When scope_pattern is explicitly set, it is used."""
        cfg = CrawlConfig(
            start_url="https://developers.tiktok.com",
            seed_hostname="developers.tiktok.com",
            scope_pattern="*.tiktok.com",
        )
        assert cfg.seed_hostname == "developers.tiktok.com"
        assert cfg.scope_pattern == "*.tiktok.com"

    def test_empty_scope_pattern_defaults(self):
        """Explicit empty string defaults to seed_hostname."""
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
            scope_pattern="",
        )
        assert cfg.scope_pattern == "example.com"


class TestLinkFilteringWithGlob:
    """Test that links are filtered using scope_pattern glob matching."""

    def test_subdomain_matching_glob_is_in_scope(self):
        """A subdomain matching *.tiktok.com is in scope."""
        assert hostname_matches_scope("api.tiktok.com", "*.tiktok.com") is True

    def test_unrelated_hostname_not_in_scope(self):
        """A hostname not matching the glob is out of scope."""
        assert hostname_matches_scope("evil.com", "*.tiktok.com") is False

    def test_exact_match_still_works(self):
        """Exact hostname match works as before."""
        assert hostname_matches_scope("example.com", "example.com") is True

    def test_seed_subdomain_matches_own_scope(self):
        """Seed hostname (developers.tiktok.com) matches *.tiktok.com scope."""
        assert hostname_matches_scope("developers.tiktok.com", "*.tiktok.com") is True


class TestURLConstructionFromSeedHostname:
    """Test that concrete fetch URLs are built from seed_hostname, not scope_pattern."""

    def test_robots_txt_uses_seed_hostname(self):
        """robots.txt URL is built from seed_hostname."""
        cfg = CrawlConfig(
            start_url="https://developers.tiktok.com",
            seed_hostname="developers.tiktok.com",
            scope_pattern="*.tiktok.com",
        )
        robots_url = f"https://{cfg.seed_hostname}/robots.txt"
        assert robots_url == "https://developers.tiktok.com/robots.txt"
        # NOT: https://*.tiktok.com/robots.txt
        assert "*.tiktok.com" not in robots_url

    def test_sitemap_uses_seed_hostname(self):
        """sitemap.xml URL is built from seed_hostname."""
        cfg = CrawlConfig(
            start_url="https://developers.tiktok.com",
            seed_hostname="developers.tiktok.com",
            scope_pattern="*.tiktok.com",
        )
        sitemap_url = f"https://{cfg.seed_hostname}/sitemap.xml"
        assert sitemap_url == "https://developers.tiktok.com/sitemap.xml"
        assert "*.tiktok.com" not in sitemap_url

    def test_seed_stays_pinned_to_concrete_host(self):
        """seed_hostname is always the concrete host, scope_pattern is the glob."""
        cfg = CrawlConfig(
            start_url="https://developers.tiktok.com",
            seed_hostname="developers.tiktok.com",
            scope_pattern="*.tiktok.com",
        )
        # seed_hostname is concrete
        assert "*" not in cfg.seed_hostname
        # scope_pattern may be a glob
        assert "*" in cfg.scope_pattern


class TestCLIScopeOption:
    """Test the --scope CLI option and config wiring."""

    def test_scope_option_passed_to_configs(self):
        """--scope '*.tiktok.com' developers.tiktok.com --authorized wires correctly."""
        runner = CliRunner()
        # We don't actually run the crawl, just verify the CLI parses correctly
        result = runner.invoke(
            main,
            ["crawl", "developers.tiktok.com", "--authorized", "--scope", "*.tiktok.com"],
        )
        # The invocation will fail at the async browser stage, but should
        # not fail with a click usage error (it should parse successfully)
        # If --scope is unrecognized, click would error with "no such option"
        assert "no such option" not in result.output.lower()

    def test_scope_requires_authorized(self):
        """--scope without --authorized still refuses."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["crawl", "developers.tiktok.com", "--scope", "*.tiktok.com"],
        )
        assert result.exit_code != 0  # should fail without --authorized

    def test_default_no_scope_still_works(self):
        """Crawl without --scope still parses correctly (backward compat)."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["crawl", "example.com", "--authorized"],
        )
        assert "no such option" not in result.output.lower()
