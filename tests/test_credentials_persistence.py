"""Tests for registration credential persistence."""

import json
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from ai_browser.cli import main, _save_credentials


# ---------------------------------------------------------------------------
# Unit tests: _save_credentials helper
# ---------------------------------------------------------------------------

class TestSaveCredentialsHelper:
    """Direct unit tests for the _save_credentials function."""

    def test_creates_file_with_correct_content(self, tmp_path):
        """_save_credentials writes the expected JSON and sets 0600 perms."""
        storage_dir = tmp_path / "browser_states"

        _save_credentials(
            storage_dir=storage_dir,
            hostname="example.com",
            email="test@example.com",
            password="Str0ngP@ss!",
            confirmed=True,
        )

        cred_file = storage_dir / "credentials" / "example.com.json"
        assert cred_file.exists(), f"Expected {cred_file} to exist"

        data = json.loads(cred_file.read_text())
        assert data["hostname"] == "example.com"
        assert data["email"] == "test@example.com"
        assert data["password"] == "Str0ngP@ss!"
        assert data["confirmed"] is True
        assert "registered_at" in data

        # Permissions must be 0o600 (owner read/write only)
        file_mode = cred_file.stat().st_mode & 0o777
        assert file_mode == 0o600, (
            f"Expected 0o600, got {oct(file_mode)}"
        )

    def test_unconfirmed_saves_with_confirmed_false(self, tmp_path):
        """When confirmed=False, the file is still created and marked accordingly."""
        storage_dir = tmp_path / "browser_states"

        _save_credentials(
            storage_dir=storage_dir,
            hostname="example.com",
            email="test@example.com",
            password="Str0ngP@ss!",
            confirmed=False,
        )

        cred_file = storage_dir / "credentials" / "example.com.json"
        assert cred_file.exists()
        data = json.loads(cred_file.read_text())
        assert data["confirmed"] is False
        assert data["password"] == "Str0ngP@ss!"

    def test_secure_permissions_on_existing_dir(self, tmp_path):
        """0600 permissions are set even when the credentials dir already exists."""
        storage_dir = tmp_path / "browser_states"
        (storage_dir / "credentials").mkdir(parents=True)

        _save_credentials(
            storage_dir=storage_dir,
            hostname="example.com",
            email="test@example.com",
            password="p",
            confirmed=True,
        )

        cred_file = storage_dir / "credentials" / "example.com.json"
        file_mode = cred_file.stat().st_mode & 0o777
        assert file_mode == 0o600


# ---------------------------------------------------------------------------
# Integration test: credentials saved / not-saved during Phase 3
# ---------------------------------------------------------------------------

class TestPhase3CredentialsFlow:
    """Verify that the CLI Phase 3 registration flow saves credentials
    after a successful register() call, and does NOT save them on error."""

    def test_credentials_saved_after_confirmed_registration(self, tmp_path):
        """A successful, confirmed registration saves the credentials file."""
        storage_dir = tmp_path / "browser_states"
        storage_dir.mkdir(parents=True)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.pages = []

        mock_crawler_run = AsyncMock()
        fake_result = MagicMock()
        fake_result.endpoints = []
        fake_result.total_pages_crawled = 0
        fake_result.total_links_discovered = 0
        fake_result.total_js_endpoints = 0
        fake_result.unique_urls = []
        fake_result.errors = []
        mock_crawler_run.return_value = fake_result

        mock_page = MagicMock()
        mock_page.url = "https://example.com/welcome"

        mock_handler_register = AsyncMock(return_value=mock_page)
        mock_handler = MagicMock()
        mock_handler.confirmed = True
        mock_handler.register = mock_handler_register

        runner = CliRunner()

        with patch(
            "ai_browser.cli.BrowserSession", return_value=mock_session
        ), patch(
            "ai_browser.cli.Crawler.run", new=mock_crawler_run
        ), patch(
            "ai_browser.cli.RegistrationHandler", return_value=mock_handler
        ):
            result = runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",
                    "--register",
                    "--register-email", "test@example.com",
                    "--register-password", "Str0ngP@ss!",
                    "--storage-dir", str(storage_dir),
                ],
            )

        assert result.exit_code == 0, result.output

        # Verify credentials file was created
        cred_file = storage_dir / "credentials" / "example.com.json"
        assert cred_file.exists(), (
            f"Credentials file not found at {cred_file}\nOutput:\n{result.output}"
        )
        data = json.loads(cred_file.read_text())
        assert data["email"] == "test@example.com"
        assert data["password"] == "Str0ngP@ss!"
        assert data["confirmed"] is True

        # Verify permissions
        file_mode = cred_file.stat().st_mode & 0o777
        assert file_mode == 0o600

    def test_credentials_saved_when_confirmed_false(self, tmp_path):
        """Even when confirmation fails, the credentials file is saved
        so the user can complete confirmation manually."""
        storage_dir = tmp_path / "browser_states"
        storage_dir.mkdir(parents=True)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.pages = []

        fake_result = MagicMock()
        fake_result.endpoints = []
        fake_result.total_pages_crawled = 0
        fake_result.total_links_discovered = 0
        fake_result.total_js_endpoints = 0
        fake_result.unique_urls = []
        fake_result.errors = []

        mock_page = MagicMock()
        mock_page.url = "https://example.com/pending"

        mock_handler = MagicMock()
        mock_handler.confirmed = False
        mock_handler.register = AsyncMock(return_value=mock_page)

        runner = CliRunner()

        with patch(
            "ai_browser.cli.BrowserSession", return_value=mock_session
        ), patch(
            "ai_browser.cli.Crawler.run",
            new=AsyncMock(return_value=fake_result),
        ), patch(
            "ai_browser.cli.RegistrationHandler", return_value=mock_handler
        ):
            result = runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",
                    "--register",
                    "--register-email", "test@example.com",
                    "--register-password", "Str0ngP@ss!",
                    "--storage-dir", str(storage_dir),
                ],
            )

        assert result.exit_code == 0, result.output

        cred_file = storage_dir / "credentials" / "example.com.json"
        assert cred_file.exists()
        data = json.loads(cred_file.read_text())
        assert data["confirmed"] is False
        assert data["password"] == "Str0ngP@ss!"

        # The WARNING message should appear in output
        assert "WARNING" in result.output

    def test_credentials_not_saved_on_registration_error(self, tmp_path):
        """If handler.register() raises, no credentials file is created."""
        storage_dir = tmp_path / "browser_states"
        storage_dir.mkdir(parents=True)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.pages = []

        fake_result = MagicMock()
        fake_result.endpoints = []
        fake_result.total_pages_crawled = 0
        fake_result.total_links_discovered = 0
        fake_result.total_js_endpoints = 0
        fake_result.unique_urls = []
        fake_result.errors = []

        mock_handler = MagicMock()
        mock_handler.register = AsyncMock(
            side_effect=RuntimeError("signup form not found")
        )

        runner = CliRunner()

        with patch(
            "ai_browser.cli.BrowserSession", return_value=mock_session
        ), patch(
            "ai_browser.cli.Crawler.run",
            new=AsyncMock(return_value=fake_result),
        ), patch(
            "ai_browser.cli.RegistrationHandler", return_value=mock_handler
        ):
            result = runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",
                    "--register",
                    "--register-email", "test@example.com",
                    "--register-password", "Str0ngP@ss!",
                    "--storage-dir", str(storage_dir),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Registration error" in result.output

        cred_file = storage_dir / "credentials" / "example.com.json"
        assert not cred_file.exists(), (
            "Credentials file should NOT exist when registration fails"
        )


# ---------------------------------------------------------------------------
# Auto-generated password vs explicit password
# ---------------------------------------------------------------------------


class TestAutoGeneratedPassword:
    """Verify that omitting --register-password auto-generates a fresh
    random password, and that an explicit value is used as-is."""

    def test_omitting_flag_generates_different_passwords(self):
        """Two invocations without --register-password produce two
        distinct auto-generated passwords."""
        runner = CliRunner()

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.pages = []

        fake_result = MagicMock()
        fake_result.endpoints = []
        fake_result.total_pages_crawled = 0
        fake_result.total_links_discovered = 0
        fake_result.total_js_endpoints = 0
        fake_result.unique_urls = []
        fake_result.errors = []

        passwords = []
        for _ in range(2):
            with patch(
                "ai_browser.cli.BrowserSession", return_value=mock_session
            ), patch(
                "ai_browser.cli.Crawler.run",
                new=AsyncMock(return_value=fake_result),
            ), patch(
                "ai_browser.cli._run_crawl", new_callable=AsyncMock
            ) as mock_run:
                runner.invoke(
                    main,
                    [
                        "crawl", "example.com", "--authorized",
                        "--no-agent",
                    ],
                )
                passwords.append(mock_run.call_args.kwargs["register_password"])

        assert len(passwords[0]) > 0
        assert len(passwords[1]) > 0
        assert passwords[0] != passwords[1], (
            "Two invocations should generate different passwords"
        )

    def test_explicit_password_used_as_is(self):
        """When --register-password is explicitly provided, that value
        is used without being overridden."""
        runner = CliRunner()

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.pages = []

        fake_result = MagicMock()
        fake_result.endpoints = []
        fake_result.total_pages_crawled = 0
        fake_result.total_links_discovered = 0
        fake_result.total_js_endpoints = 0
        fake_result.unique_urls = []
        fake_result.errors = []

        with patch(
            "ai_browser.cli.BrowserSession", return_value=mock_session
        ), patch(
            "ai_browser.cli.Crawler.run",
            new=AsyncMock(return_value=fake_result),
        ), patch(
            "ai_browser.cli._run_crawl", new_callable=AsyncMock
        ) as mock_run:
            runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",
                    "--register-password", "MyExplicitP@ss!",
                ],
            )

        assert mock_run.call_args.kwargs["register_password"] == "MyExplicitP@ss!"

    def test_generated_password_saved_to_credentials(self, tmp_path):
        """When password is auto-generated, the saved credentials file
        contains the generated password (not the old hardcoded default)."""
        storage_dir = tmp_path / "browser_states"
        storage_dir.mkdir(parents=True)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.pages = []

        fake_result = MagicMock()
        fake_result.endpoints = []
        fake_result.total_pages_crawled = 0
        fake_result.total_links_discovered = 0
        fake_result.total_js_endpoints = 0
        fake_result.unique_urls = []
        fake_result.errors = []

        mock_page = MagicMock()
        mock_page.url = "https://example.com/welcome"

        mock_handler = MagicMock()
        mock_handler.confirmed = True
        mock_handler.register = AsyncMock(return_value=mock_page)

        runner = CliRunner()

        with patch(
            "ai_browser.cli.BrowserSession", return_value=mock_session
        ), patch(
            "ai_browser.cli.Crawler.run",
            new=AsyncMock(return_value=fake_result),
        ), patch(
            "ai_browser.cli.RegistrationHandler", return_value=mock_handler
        ):
            result = runner.invoke(
                main,
                [
                    "crawl", "example.com", "--authorized",
                    "--no-agent",
                    "--register",
                    "--register-email", "test@example.com",
                    "--storage-dir", str(storage_dir),
                ],
            )

        assert result.exit_code == 0, result.output

        cred_file = storage_dir / "credentials" / "example.com.json"
        assert cred_file.exists()
        data = json.loads(cred_file.read_text())
        saved_password = data["password"]

        # Must be a generated password, not the old hardcoded default
        assert saved_password != "Test1234!@#$"
        assert len(saved_password) > 0
        assert data["email"] == "test@example.com"
        assert data["confirmed"] is True
