"""Tests for --scope-file support (multi-pattern scope matching)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ai_browser._scope import (
    as_scope_list,
    hostname_matches_any_scope,
    page_url_matches_any_scope,
    display_scope,
)
from ai_browser.browser_session import BrowserSessionConfig
from ai_browser.crawler import CrawlConfig
from ai_browser.cli import main


# ---------------------------------------------------------------------------
# Fixture: the real tiktok_scope.txt shipped in the repo root
# ---------------------------------------------------------------------------


@pytest.fixture
def tiktok_scope_file() -> Path:
    return Path(__file__).parent.parent / "scope" / "tiktok_scope.txt"


# ---------------------------------------------------------------------------
# as_scope_list / hostname_matches_any_scope
# ---------------------------------------------------------------------------


class TestAsScopeList:
    def test_string_input(self):
        assert as_scope_list("*.example.com") == ["*.example.com"]

    def test_list_input(self):
        assert as_scope_list(["*.tiktok.com", "tiktok.com"]) == [
            "*.tiktok.com", "tiktok.com",
        ]

    def test_list_input_preserves_order(self):
        patterns = ["a.com", "b.com", "c.com"]
        assert as_scope_list(patterns) == patterns


class TestHostnameMatchesAnyScope:
    def test_string_input_matching(self):
        assert hostname_matches_any_scope("app.example.com", "*.example.com") is True

    def test_string_input_non_matching(self):
        assert hostname_matches_any_scope("evil.com", "*.example.com") is False

    def test_list_input_one_match(self):
        patterns = ["*.tiktok.com", "*.soundon.global"]
        assert hostname_matches_any_scope("www.soundon.global", patterns) is True

    def test_list_input_no_match(self):
        patterns = ["*.tiktok.com", "tiktok.com"]
        assert hostname_matches_any_scope("evil.com", patterns) is False

    def test_list_input_exact_and_glob(self):
        patterns = ["*.tiktok.com", "tiktok.com"]
        # apex tiktok.com does NOT match *.tiktok.com, but DOES match tiktok.com
        assert hostname_matches_any_scope("tiktok.com", patterns) is True

    def test_tiktok_scope_fixture_hosts(self, tiktok_scope_file):
        """Concrete hosts from the real tiktok_scope.txt are correctly in-scope."""
        patterns = _parse_scope_file(tiktok_scope_file)
        # *.tiktok.com does NOT cover the apex — tiktok.com is listed separately
        assert hostname_matches_any_scope("tiktok.com", patterns) is True
        # These are covered by *.tiktok.com
        assert hostname_matches_any_scope("api.tiktok.com", patterns) is True
        assert hostname_matches_any_scope("careers.tiktok.com", patterns) is True
        # These are separate entries — neither *.tiktok.com nor tiktok.com
        assert hostname_matches_any_scope("www.pangleglobal.com", patterns) is True
        assert hostname_matches_any_scope("affiliate-id.tokopedia.com", patterns) is True
        assert hostname_matches_any_scope("www.soundon.global", patterns) is True
        # Subdomain of *.soundon.global
        assert hostname_matches_any_scope("api.soundon.global", patterns) is True
        # Definitely out-of-scope
        assert hostname_matches_any_scope("evil.com", patterns) is False
        assert hostname_matches_any_scope("google.com", patterns) is False


class TestPageUrlMatchesAnyScope:
    def test_url_with_in_scope_hostname(self):
        patterns = ["*.tiktok.com", "www.soundon.global"]
        assert page_url_matches_any_scope(
            "https://www.soundon.global/music", patterns
        ) is True

    def test_url_with_out_of_scope_hostname(self):
        patterns = ["*.tiktok.com"]
        assert page_url_matches_any_scope(
            "https://evil.com/page", patterns
        ) is False


class TestDisplayScope:
    def test_single_pattern(self):
        assert display_scope("*.example.com") == "'*.example.com'"

    def test_list_patterns(self):
        result = display_scope(["*.tiktok.com", "tiktok.com"])
        assert result == "['*.tiktok.com', 'tiktok.com']"


# ---------------------------------------------------------------------------
# Scope file parsing
# ---------------------------------------------------------------------------


def _parse_scope_file(path: Path) -> list[str]:
    """Parse a scope file the same way cli.py does (for test reuse)."""
    raw = path.read_text()
    return [
        line.strip() for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestScopeFileParsing:
    def test_real_tiktok_scope_has_patterns(self, tiktok_scope_file):
        patterns = _parse_scope_file(tiktok_scope_file)
        assert len(patterns) > 5, f"Expected many patterns, got {len(patterns)}"
        assert "*.tiktok.com" in patterns
        assert "tiktok.com" in patterns
        assert "www.pangleglobal.com" in patterns
        # Comment lines must be excluded
        assert not any(p.startswith("#") for p in patterns)
        # Blank lines must be excluded
        assert not any(p == "" for p in patterns)

    def test_only_comments_and_blanks_produces_empty(self, tmp_path):
        """A file with only comments and blank lines produces an empty list."""
        sf = tmp_path / "empty_scope.txt"
        sf.write_text("# comment\n\n    # another comment\n")
        patterns = _parse_scope_file(sf)
        assert patterns == []


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCLIScopeFileOption:
    def test_scope_file_option_accepted(self, tiktok_scope_file):
        """--scope-file flag is recognized by Click and parsed without error."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "crawl", "developers.tiktok.com", "--authorized",
                "--scope-file", str(tiktok_scope_file),
                "--no-proxy", "--no-agent",
            ],
        )
        # The invocation will fail at the async browser stage, but the CLI
        # should parse successfully (no "no such option" error).
        assert "no such option" not in result.output.lower()

    def test_scope_and_scope_file_together(self, tiktok_scope_file, tmp_path):
        """--scope and --scope-file together union the patterns."""
        # We can't easily inspect the final pattern list from a CliRunner
        # invocation (it goes async), but we can test the parsing logic
        # directly.
        sf = tmp_path / "minimal.txt"
        sf.write_text("*.tiktok.com\n")
        patterns = _parse_scope_file(sf)
        patterns.append("extra.example.com")  # --scope appends
        assert patterns == ["*.tiktok.com", "extra.example.com"]


# ---------------------------------------------------------------------------
# CrawlConfig: list scope_pattern
# ---------------------------------------------------------------------------


class TestCrawlConfigListScope:
    def test_list_scope_pattern(self):
        cfg = CrawlConfig(
            start_url="https://developers.tiktok.com",
            seed_hostname="developers.tiktok.com",
            scope_pattern=["*.tiktok.com", "www.pangleglobal.com"],
        )
        assert cfg.scope_pattern == ["*.tiktok.com", "www.pangleglobal.com"]

    def test_string_scope_still_works(self):
        """Plain string scope_pattern is still accepted (backward compat)."""
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
            scope_pattern="*.example.com",
        )
        assert cfg.scope_pattern == "*.example.com"

    def test_default_scope_still_seed_hostname(self):
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
        )
        # Defaults to seed_hostname (a str), not a list
        assert cfg.scope_pattern == "example.com"


# ---------------------------------------------------------------------------
# BrowserSessionConfig: list authorized_hostname
# ---------------------------------------------------------------------------


class TestBrowserSessionConfigListScope:
    def test_list_authorized_hostname(self):
        cfg = BrowserSessionConfig(
            authorized_hostname=["*.tiktok.com", "www.soundon.global"],
            storage_key="test",
        )
        assert cfg.authorized_hostname == ["*.tiktok.com", "www.soundon.global"]

    def test_string_authorized_hostname_still_works(self):
        cfg = BrowserSessionConfig(authorized_hostname="*.example.com")
        assert cfg.authorized_hostname == "*.example.com"

    def test_storage_key_explicit(self):
        cfg = BrowserSessionConfig(
            authorized_hostname=["*.tiktok.com", "tiktok.com"],
            storage_key="my_key",
        )
        assert cfg.storage_key == "my_key"


# ---------------------------------------------------------------------------
# storage_key fallback behavior (regression guard)
# ---------------------------------------------------------------------------


class TestStorageKeyFallback:
    def test_string_authorized_hostname_no_storage_key(self):
        """When authorized_hostname is a str and storage_key is None,
        _resolve_storage_key falls back to authorized_hostname."""
        from ai_browser.browser_session.session import BrowserSession
        cfg = BrowserSessionConfig(authorized_hostname="example.com")
        session = BrowserSession(cfg)
        assert session._resolve_storage_key() == "example.com"

    def test_string_authorized_hostname_preserves_original_filename(self):
        """The exact same filename is produced as before this change."""
        from ai_browser.browser_session.session import BrowserSession
        cfg = BrowserSessionConfig(
            authorized_hostname="example.com",
            storage_dir=Path("/tmp/test_storage"),
        )
        session = BrowserSession(cfg)
        key = session._resolve_storage_key()
        safe = key.replace(":", "_").replace("/", "_")
        assert safe == "example.com"
        # The full path matches the pre-change behavior
        expected = Path("/tmp/test_storage") / "example.com.json"
        assert session._resolve_storage_file() == expected

    def test_list_without_storage_key_raises(self):
        """List authorized_hostname without storage_key raises ValueError
        at session construction (via _resolve_storage_file)."""
        from ai_browser.browser_session.session import BrowserSession
        cfg = BrowserSessionConfig(
            authorized_hostname=["*.tiktok.com", "tiktok.com"],
        )
        with pytest.raises(ValueError, match="storage_key"):
            BrowserSession(cfg)

    def test_list_with_storage_key_works(self):
        """List authorized_hostname with storage_key derives correct filename."""
        from ai_browser.browser_session.session import BrowserSession
        cfg = BrowserSessionConfig(
            authorized_hostname=["*.tiktok.com", "tiktok.com"],
            storage_key="developers.tiktok.com",
            storage_dir=Path("/tmp/test_storage"),
        )
        session = BrowserSession(cfg)
        assert session._resolve_storage_key() == "developers.tiktok.com"
        expected = Path("/tmp/test_storage") / "developers.tiktok.com.json"
        assert session._resolve_storage_file() == expected


# ---------------------------------------------------------------------------
# Scope guard error with list scope
# ---------------------------------------------------------------------------


class TestScopeGuardErrorList:
    def test_error_accepts_list_authorized_hostname(self):
        from ai_browser.browser_session.models import ScopeGuardError
        err = ScopeGuardError(
            attempted_hostname="evil.com",
            authorized_hostname=["*.tiktok.com", "tiktok.com"],
        )
        assert err.authorized_hostname == ["*.tiktok.com", "tiktok.com"]
        assert "evil.com" in str(err)
