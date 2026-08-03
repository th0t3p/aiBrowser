"""Tests for .env support and envvar resolution in the CLI."""

import importlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from ai_browser.cli import main


# ---------------------------------------------------------------------------
# .env loading via python-dotenv
# ---------------------------------------------------------------------------

class TestDotenvLoading:
    """Verify that load_dotenv() loads variables from a .env file."""

    def test_load_dotenv_picks_up_file(self, tmp_path, monkeypatch):
        """A .env file in CWD is loaded into os.environ before Click runs."""
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text(
            "AIBROWSER_LLM_PROVIDER=deepseek\n"
            "AIBROWSER_LLM_MAX_TOKENS=8192\n"
        )
        monkeypatch.chdir(tmp_path)

        # Reload the dotenv loading (load_dotenv caches, so call directly)
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=dotenv_file, override=True)

        assert os.environ["AIBROWSER_LLM_PROVIDER"] == "deepseek"
        assert os.environ["AIBROWSER_LLM_MAX_TOKENS"] == "8192"


# ---------------------------------------------------------------------------
# Click envvar resolution — env vars are picked up as option defaults
# ---------------------------------------------------------------------------

class TestClickEnvvarResolution:
    """Verify that Click's envvar mechanism picks up os.environ values
    when the CLI flag is not explicitly passed."""

    def test_envvar_becomes_default_when_flag_absent(self, monkeypatch):
        """Setting AIBROWSER_LLM_PROVIDER in the environment makes it the
        default for --llm-provider when the flag is omitted."""
        monkeypatch.setenv("AIBROWSER_LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("AIBROWSER_LLM_MODEL", "deepseek-chat")
        monkeypatch.setenv("AIBROWSER_LLM_API_KEY", "sk-test-key")
        monkeypatch.setenv("AIBROWSER_LLM_MAX_TOKENS", "8192")

        runner = CliRunner()

        # We can't actually run the full crawl (needs a browser), so
        # invoke with --help to verify Click parsed the env vars without
        # error, and check the option default via a lightweight path:
        # just confirm that invoking without --llm-provider doesn't
        # complain about a missing provider.
        with patch("ai_browser.cli._run_crawl", new_callable=AsyncMock) as mock_run:
            result = runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",  # skip agent to avoid needing real LLM
                ],
            )
        # The CLI should start successfully (no missing-provider error);
        # the mocked _run_crawl should have been called with the env-provided values.
        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["llm_provider"] == "deepseek"
        assert call_kwargs["llm_max_tokens"] == 8192

    def test_explicit_flag_overrides_envvar(self, monkeypatch):
        """When both an env var AND an explicit CLI flag are present,
        the CLI flag wins (Click's documented priority)."""
        monkeypatch.setenv("AIBROWSER_LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("AIBROWSER_LLM_API_KEY", "sk-test-key")
        monkeypatch.setenv("AIBROWSER_LLM_MAX_TOKENS", "4096")

        runner = CliRunner()

        with patch("ai_browser.cli._run_crawl", new_callable=AsyncMock) as mock_run:
            result = runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",
                    "--llm-provider", "openai",          # explicit override
                    "--llm-max-tokens", "16000",          # explicit override
                ],
            )
        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        # Explicit flags override env vars
        assert call_kwargs["llm_provider"] == "openai"
        assert call_kwargs["llm_max_tokens"] == 16000

    def test_register_password_has_no_envvar(self, monkeypatch):
        """--register-password is deliberately excluded from envvar
        support — setting AIBROWSER_REGISTER_PASSWORD in the environment
        should NOT affect the generated password."""
        monkeypatch.setenv("AIBROWSER_REGISTER_PASSWORD", "should-not-be-used")
        monkeypatch.setenv("AIBROWSER_LLM_API_KEY", "sk-test-key")

        runner = CliRunner()

        with patch("ai_browser.cli._run_crawl", new_callable=AsyncMock) as mock_run:
            result = runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",
                ],
            )
        assert result.exit_code == 0, result.output
        call_kwargs = mock_run.call_args.kwargs
        password = call_kwargs["register_password"]
        # When omitted, a random password is auto-generated — it must
        # NOT be the env var value we set.
        assert isinstance(password, str)
        assert len(password) > 0
        assert password != "should-not-be-used"


# ---------------------------------------------------------------------------
# .env file is gitignored
# ---------------------------------------------------------------------------

class TestDotenvGitignore:
    """Verify that .env files are never staged by git."""

    def test_dotenv_not_staged_by_git(self, tmp_path, monkeypatch):
        """git add -A does NOT stage .env files — they stay untracked."""
        # Create a dummy git repo in tmp_path
        import subprocess
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            capture_output=True, check=True,
        )

        # Copy the real .gitignore so .env rules are active
        repo_root = Path(__file__).resolve().parent.parent
        gitignore_src = repo_root / ".gitignore"
        (tmp_path / ".gitignore").write_text(gitignore_src.read_text())

        # Create .env with dummy secrets
        (tmp_path / ".env").write_text("AI_KEY=secret123\nPASSWORD=hunter2\n")

        # git add -A
        subprocess.run(["git", "add", "-A"], capture_output=True, check=True)

        # git status — .env should NOT appear as staged
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        staged_files = [line for line in status.stdout.splitlines() if line.strip()]

        # The .gitignore itself and .env.example might be staged,
        # but .env must NOT be.
        staged_paths = {line.split()[-1] for line in staged_files}
        assert ".env" not in staged_paths, (
            f".env was staged by git! Staged files: {staged_paths}"
        )
        # .env.example SHOULD be trackable
        # (it's not in tmp_path, so it won't appear — just verify .env isn't there)
