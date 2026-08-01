"""Tests for --skip-existing resume-crawl feature."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from ai_browser.crawler import Crawler, CrawlConfig, CrawlResult, DiscoveredEndpoint, DiscoveryMethod
from ai_browser.cli import main


class TestCrawlerSeedVisited:
    """Test that seeding _visited prevents re-crawling of prior URLs."""

    def test_seed_visited_pre_populates_visited_set(self):
        """_visited contains the seeded normalized URLs from the start."""
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
        )
        seed = {
            Crawler._normalize("https://example.com/page1"),
            Crawler._normalize("https://example.com/page2?q=1"),
        }
        crawler = Crawler(cfg, seed_visited=seed)
        assert "https://example.com/page1" in crawler._visited
        assert "https://example.com/page2?q=1" in crawler._visited

    def test_seed_visited_normalizes_consistently(self):
        """Seeded URLs are normalized so variants map to the same key."""
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
        )
        seed = {Crawler._normalize("https://EXAMPLE.COM/Page1/")}
        crawler = Crawler(cfg, seed_visited=seed)
        # The normalized form (lowercase, no trailing slash) is what gets stored
        assert "https://example.com/page1" in crawler._visited
        # A different casing or trailing slash variant would normalize to the same key
        assert Crawler._normalize("https://EXAMPLE.COM/Page1/") == "https://example.com/page1"

    def test_none_seed_visited_results_in_empty_set(self):
        """None seed_visited (the default) gives an empty visited set."""
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
        )
        crawler = Crawler(cfg)
        assert crawler._visited == set()

    def test_empty_seed_visited_results_in_empty_set(self):
        """Empty set seed_visited gives an empty visited set."""
        cfg = CrawlConfig(
            start_url="https://example.com",
            seed_hostname="example.com",
        )
        crawler = Crawler(cfg, seed_visited=set())
        assert crawler._visited == set()


class TestCLISkipExistingOption:
    """Test the --skip-existing CLI flag."""

    def test_skip_existing_flag_is_recognized(self):
        """--skip-existing is a valid click option and parses without error."""
        runner = CliRunner()
        # Using --help to verify option is recognized
        result = runner.invoke(main, ["crawl", "--help"])
        assert "--skip-existing" in result.output

    def test_invalid_json_prints_clear_error(self):
        """A valid path with invalid JSON content produces a clear error."""
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("this is not valid JSON {{{")
            bad_path = f.name

        try:
            result = runner.invoke(
                main,
                [
                    "crawl",
                    "example.com",
                    "--authorized",
                    "--skip-existing",
                    bad_path,
                ],
            )
            assert result.exit_code != 0
            assert "not valid JSON" in result.output
        finally:
            Path(bad_path).unlink()

    def test_valid_json_empty_endpoints(self):
        """A valid JSON without an endpoints key still works."""
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"hostname": "example.com", "endpoints": []}, f)
            good_path = f.name

        try:
            result = runner.invoke(
                main,
                [
                    "crawl",
                    "example.com",
                    "--authorized",
                    "--skip-existing",
                    good_path,
                ],
            )
            # Will fail at browser stage, but should parse and report loaded=0
            assert "Loaded 0 previously-discovered endpoints" in result.output
        finally:
            Path(good_path).unlink()

    def test_valid_json_with_endpoints(self):
        """A valid JSON with endpoints reports them as loaded."""
        runner = CliRunner()
        prior = {
            "hostname": "example.com",
            "endpoints": [
                {"url": "https://example.com/a", "method": "link", "source_url": None, "discovered_at": "2026-01-01T00:00:00"},
                {"url": "https://example.com/b", "method": "sitemap", "source_url": None, "discovered_at": "2026-01-01T00:00:00"},
            ],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(prior, f)
            good_path = f.name

        try:
            result = runner.invoke(
                main,
                [
                    "crawl",
                    "example.com",
                    "--authorized",
                    "--skip-existing",
                    good_path,
                ],
            )
            assert "Loaded 2 previously-discovered endpoints" in result.output
        finally:
            Path(good_path).unlink()


class TestOutputMerging:
    """Test that prior endpoints are merged into the final output correctly."""

    @pytest.mark.asyncio
    async def test_output_includes_prior_plus_new_endpoints(self):
        """Output JSON merges prior endpoints with newly-discovered ones."""
        from ai_browser.cli import _run_crawl
        from ai_browser.browser_session import BrowserSessionConfig

        prior_endpoints = [
            {"url": "https://example.com/page1", "method": "link", "source_url": None, "discovered_at": "2026-01-01T00:00:00"},
            {"url": "https://example.com/page2", "method": "sitemap", "source_url": None, "discovered_at": "2026-01-01T00:00:00"},
        ]

        # Build a fake result with 3 endpoints — one of which duplicates a prior
        fake_result = CrawlResult(
            config=CrawlConfig(start_url="https://example.com", seed_hostname="example.com"),
            endpoints=[
                DiscoveredEndpoint(url="https://example.com/page1", method=DiscoveryMethod.LINK),
                DiscoveredEndpoint(url="https://example.com/page3", method=DiscoveryMethod.LINK),
                DiscoveredEndpoint(url="https://example.com/page4", method=DiscoveryMethod.JS_REGEX),
            ],
            total_pages_crawled=5,
            total_links_discovered=10,
            total_js_endpoints=1,
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            output_path = f.name

        try:
            # Mock the BrowserSession context manager and the crawler
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "ai_browser.cli.BrowserSession",
                return_value=mock_session,
            ), patch.object(
                Crawler, "run", new_callable=AsyncMock, return_value=fake_result
            ):

                await _run_crawl(
                    session_config=BrowserSessionConfig(authorized_hostname="example.com"),
                    crawl_config=CrawlConfig(
                        start_url="https://example.com",
                        seed_hostname="example.com",
                    ),
                    seed_visited={Crawler._normalize(ep["url"]) for ep in prior_endpoints},
                    prior_endpoints=prior_endpoints,
                    run_agent=False,
                    llm_provider="anthropic",
                    llm_model=None,
                    llm_api_key=None,
                    llm_base_url=None,
                    do_register=False,
                    register_email=None,
                    register_password="",
                    register_name="",
                    do_login=False,
                    login_email=None,
                    login_password=None,
                    imap_host=None,
                    imap_port=993,
                    imap_username=None,
                    imap_password=None,
                    email_timeout=120,
                    output_file=output_path,
                    hostname="example.com",
                    scope_pattern="example.com",
                )

            # Verify output file contents
            output_data = json.loads(Path(output_path).read_text())
            assert len(output_data["endpoints"]) == 4  # 2 prior + 2 genuinely new (page3, page4)
            assert output_data["skipped_existing_count"] == 2
            assert output_data["newly_discovered_count"] == 2

            # Verify prior entries are at the beginning
            urls = [ep["url"] for ep in output_data["endpoints"]]
            assert urls[0] == "https://example.com/page1"
            assert urls[1] == "https://example.com/page2"

            # Verify the duplicate page1 was not included as a new endpoint
            assert urls[2] in ("https://example.com/page3", "https://example.com/page4")
            assert urls[3] in ("https://example.com/page3", "https://example.com/page4")
            assert urls[2] != urls[3]  # page3 and page4 are distinct

        finally:
            Path(output_path).unlink()

    @pytest.mark.asyncio
    async def test_output_without_prior_has_no_skip_counts(self):
        """Output without prior endpoints omits the skipped/newly_discovered counts."""
        from ai_browser.cli import _run_crawl
        from ai_browser.browser_session import BrowserSessionConfig

        fake_result = CrawlResult(
            config=CrawlConfig(start_url="https://example.com", seed_hostname="example.com"),
            endpoints=[
                DiscoveredEndpoint(url="https://example.com/a", method=DiscoveryMethod.LINK),
            ],
            total_pages_crawled=1,
            total_links_discovered=2,
            total_js_endpoints=0,
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            output_path = f.name

        try:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "ai_browser.cli.BrowserSession",
                return_value=mock_session,
            ), patch.object(
                Crawler, "run", new_callable=AsyncMock, return_value=fake_result
            ):

                await _run_crawl(
                    session_config=BrowserSessionConfig(authorized_hostname="example.com"),
                    crawl_config=CrawlConfig(
                        start_url="https://example.com",
                        seed_hostname="example.com",
                    ),
                    seed_visited=None,
                    prior_endpoints=[],
                    run_agent=False,
                    llm_provider="anthropic",
                    llm_model=None,
                    llm_api_key=None,
                    llm_base_url=None,
                    do_register=False,
                    register_email=None,
                    register_password="",
                    register_name="",
                    do_login=False,
                    login_email=None,
                    login_password=None,
                    imap_host=None,
                    imap_port=993,
                    imap_username=None,
                    imap_password=None,
                    email_timeout=120,
                    output_file=output_path,
                    hostname="example.com",
                    scope_pattern="example.com",
                )

            output_data = json.loads(Path(output_path).read_text())
            assert "skipped_existing_count" not in output_data
            assert "newly_discovered_count" not in output_data
            assert len(output_data["endpoints"]) == 1

        finally:
            Path(output_path).unlink()
