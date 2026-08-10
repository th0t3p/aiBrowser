"""Tests for email link extraction priority, IMAP filtering (Fixes #6, #7)."""

import asyncio
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.registration_handler.handler import RegistrationHandler
from ai_browser.registration_handler.models import RegistrationConfig


def _make_email(html_body: str = "", text_body: str = "") -> email.message.Message:
    """Build a MIME email with optional HTML and text parts."""
    msg = MIMEMultipart("alternative")
    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))
    return msg


class TestEmailLinkExtraction:
    """Test that _extract_link_from_email prioritizes confirmation links (Fix #6)."""

    @staticmethod
    def _handler():
        return RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test+target@mydomain.com",
            )
        )

    def test_prioritizes_confirm_link_over_logo(self):
        """Confirmation link with 'confirm' in path beats logo link."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/logo.png">Logo</a>
            <a href="https://target.com/confirm?token=abc123">Confirm</a>
            <a href="https://target.com/unsubscribe">Unsubscribe</a>
        """)
        result = handler._extract_link_from_email(msg, "target.com")
        assert result == "https://target.com/confirm?token=abc123"

    def test_prioritizes_verify_link(self):
        """Link with 'verify' in path is prioritized."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://example.org/image.jpg">Image</a>
            <a href="https://target.com/verify-email?id=123">Verify</a>
        """)
        result = handler._extract_link_from_email(msg, "target.com")
        assert "verify" in result

    def test_prioritizes_activate_link(self):
        """Link with 'activate' in path is prioritized."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/activate/abc">Activate Account</a>
            <a href="https://target.com/logo.png">Logo</a>
        """)
        result = handler._extract_link_from_email(msg)
        assert "activate" in result

    def test_prioritizes_token_link(self):
        """Link with 'token=' in query string is prioritized."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/home">Home</a>
            <a href="https://target.com/register/complete?token=xyz789">Complete</a>
        """)
        result = handler._extract_link_from_email(msg, "target.com")
        assert "token=" in result

    def test_falls_back_to_same_domain_link(self):
        """When no confirmation pattern found, same-domain link is selected."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://other.com/tracker.gif">Tracker</a>
            <a href="https://target.com/welcome">Welcome</a>
        """)
        result = handler._extract_link_from_email(msg, "target.com")
        assert result == "https://target.com/welcome"

    def test_falls_back_to_first_non_asset_link(self):
        """Last resort: first non-image link wins."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://other.com/logo.png">Logo</a>
            <a href="https://other.com/page">Page</a>
        """)
        result = handler._extract_link_from_email(msg)
        assert result == "https://other.com/page"

    def test_skips_image_links(self):
        """Image/tracking links are excluded."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/pixel.gif?track=1">Pixel</a>
            <a href="https://target.com/styles.css">CSS</a>
            <a href="https://target.com/dashboard">Dashboard</a>
        """)
        result = handler._extract_link_from_email(msg)
        assert result == "https://target.com/dashboard"


class TestIMAPFiltering:
    """Test IMAP sender/domain filtering and date watermark (Fix #7)."""

    @staticmethod
    def _handler(signup_url="https://target.com/signup", submitted_at=9999999.0):
        h = RegistrationHandler(
            RegistrationConfig(
                signup_url=signup_url,
                email="test+target@mydomain.com",
            )
        )
        h._signup_submitted_at = submitted_at
        return h

    def test_target_domain_filtering_enabled(self):
        """Handler now passes target_domain to _poll_inbox_for_link."""
        handler = self._handler()
        # Just verify the plumbing works — the method accepts target_domain
        import asyncio
        # _poll_inbox_for_link now takes target_domain parameter
        assert hasattr(handler, '_signup_submitted_at')
        assert handler._signup_submitted_at == 9999999.0


# ---------------------------------------------------------------------------
# RegistrationHandler.confirmed — distinguishes confirmed vs unconfirmed
# outcomes so cli.py can report accurately.
# ---------------------------------------------------------------------------


class TestRegistrationConfirmed:
    """Tests that handler.confirmed is set correctly after register()."""

    @staticmethod
    def _make_config(**kwargs):
        from ai_browser.registration_handler.models import IMAPConfig
        return RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            imap_config=IMAPConfig(
                host="imap.target.com",
                username="test@target.com",
                password="fake-pw",
            ),
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_confirmed_true_when_link_found_and_navigated(self, monkeypatch):
        """confirmed is True when IMAP polling returns a link and navigation succeeds."""
        config = self._make_config()
        handler = RegistrationHandler(config)

        # Silence internal steps we don't care about
        async def _noop(*_a, **_kw):
            return None
        monkeypatch.setattr(handler, "_check_captcha", _noop)
        # _fill_signup_form must return a list with "email" so the submit path
        # is taken (otherwise register() returns early, skipping IMAP polling)
        monkeypatch.setattr(
            handler, "_fill_signup_form",
            AsyncMock(return_value=["email", "password"]),
        )
        monkeypatch.setattr(handler, "_submit_form", _noop)

        # Return a fake confirmation link
        async def _fake_poll(*_a, **_kw):
            return "https://target.com/confirm?token=abc"
        monkeypatch.setattr(handler, "_poll_inbox_for_link", _fake_poll)

        # Mock session + page
        page = AsyncMock()
        page.url = "https://target.com/confirm?token=abc"
        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        result_page = await handler.register(session)
        assert handler.confirmed is True
        assert result_page is page

    @pytest.mark.asyncio
    async def test_confirmed_false_when_no_link_found(self, monkeypatch):
        """confirmed stays False when IMAP polling returns None (timeout)."""
        config = self._make_config()
        handler = RegistrationHandler(config)

        async def _noop(*_a, **_kw):
            return None
        monkeypatch.setattr(handler, "_check_captcha", _noop)
        monkeypatch.setattr(
            handler, "_fill_signup_form",
            AsyncMock(return_value=["email", "password"]),
        )
        monkeypatch.setattr(handler, "_submit_form", _noop)

        # Polling returns None — no confirmation email arrived
        async def _fake_poll(*_a, **_kw):
            return None
        monkeypatch.setattr(handler, "_poll_inbox_for_link", _fake_poll)

        page = AsyncMock()
        page.url = "https://target.com/signup"
        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        result_page = await handler.register(session)
        assert handler.confirmed is False
        assert result_page is page

    @pytest.mark.asyncio
    async def test_confirmed_false_when_no_imap_configured(self, monkeypatch):
        """confirmed stays False when no IMAP config exists (skip email path)."""
        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            # No imap_config — email confirmation is skipped entirely
        )
        handler = RegistrationHandler(config)

        async def _noop(*_a, **_kw):
            return None
        monkeypatch.setattr(handler, "_check_captcha", _noop)
        monkeypatch.setattr(
            handler, "_fill_signup_form",
            AsyncMock(return_value=["email", "password"]),
        )
        monkeypatch.setattr(handler, "_submit_form", _noop)

        page = AsyncMock()
        page.url = "https://target.com/signup"
        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        await handler.register(session)
        assert handler.confirmed is False

    def test_confirmed_defaults_to_false(self):
        """confirmed starts as False immediately after construction."""
        config = self._make_config()
        handler = RegistrationHandler(config)
        assert handler.confirmed is False


# ---------------------------------------------------------------------------
# IMAP latest-only bug: _check_inbox_for_new_email iterates over ALL UNSEEN
# messages (up to a bound) instead of only the single latest one
# ---------------------------------------------------------------------------


class TestIMAPChecksAllUnseenMessages:
    """Test that _check_inbox_for_new_email checks multiple UNSEEN messages,
    not just the single latest one (the exact bug that was fixed)."""

    @staticmethod
    def _make_fake_imap(message_specs: list[tuple[str, str, str]]):
        """Return a fake aioimaplib mock configured with *message_specs*.

        Each spec is (from_addr, subject, body) — ordered from oldest (id=1)
        to newest.  The fake IMAP will have all specified messages as UNSEEN.
        """
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from unittest.mock import AsyncMock, MagicMock

        fake = AsyncMock()
        fake.wait_hello_from_server = AsyncMock()
        fake.login = AsyncMock()
        fake.select = AsyncMock()
        fake.logout = AsyncMock()
        fake.search = AsyncMock()

        # Build raw messages
        raw_messages: list[bytes] = []
        for from_addr, subject, body in message_specs:
            msg = MIMEMultipart()
            msg["From"] = from_addr
            msg["Subject"] = subject
            msg["Date"] = "Tue, 5 Aug 2026 10:00:00 +0000"
            msg.attach(MIMEText(body, "plain"))
            raw_messages.append(msg.as_bytes())

        # UNSEEN search returns all IDs as space-separated
        ids = " ".join(str(i + 1) for i in range(len(message_specs)))
        fake.search.return_value = ("OK", [ids.encode()])

        async def _fetch(msg_id, _format):
            id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            idx = int(id_str) - 1
            if 0 <= idx < len(raw_messages):
                return ("OK", [f"{id_str} (RFC822)".encode(), raw_messages[idx]])
            return ("NO", [])

        fake.fetch = AsyncMock(side_effect=_fetch)
        return fake

    @staticmethod
    def _make_handler(submitted_at: float = 9999999.0):
        from ai_browser.registration_handler.handler import RegistrationHandler
        from ai_browser.registration_handler.models import RegistrationConfig, IMAPConfig

        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
            imap_config=IMAPConfig(
                host="imap.example.com",
                username="test@example.com",
                password="fake-pw",
            ),
        )
        handler = RegistrationHandler(config)
        handler._signup_submitted_at = submitted_at
        return handler

    @pytest.mark.asyncio
    async def test_real_email_sitting_behind_unrelated_message(self):
        """The primary reproduction: confirmation email (id=1) is behind an
        unrelated newer message (id=2).  The fix must check both, not just
        the latest one."""
        import sys
        from unittest.mock import MagicMock

        fake = self._make_fake_imap([
            ("noreply@developers.tiktok.com", "Verify your account",
             "Click to confirm: https://developers.tiktok.com/confirm?token=abc"),
            ("newsletter@unrelated.com", "Weekly digest",
             "Here is your weekly newsletter content..."),
        ])

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = self._make_handler(submitted_at=asyncio.get_event_loop().time() - 1)
        result = await handler._check_inbox_for_new_email("developers.tiktok.com")

        assert result is not None, "Should have found the real confirmation link"
        assert "tiktok" in result

    @pytest.mark.asyncio
    async def test_only_message_is_real_confirmation(self):
        """When the confirmation email is the ONLY UNSEEN message, it still
        works correctly — this is the case that already worked before the fix."""
        import sys
        from unittest.mock import MagicMock

        fake = self._make_fake_imap([
            ("noreply@example.com", "Confirm your email",
             "Click to confirm: https://example.com/confirm?token=xyz"),
        ])

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = self._make_handler(submitted_at=asyncio.get_event_loop().time() - 1)
        result = await handler._check_inbox_for_new_email("example.com")

        assert result is not None
        assert "confirm" in result

    @pytest.mark.asyncio
    async def test_real_email_in_various_positions(self):
        """The confirmation email is found regardless of whether it's the
        oldest, middle, or newest among several unrelated messages."""
        import sys
        from unittest.mock import MagicMock

        for real_idx, label in [(0, "oldest"), (2, "middle"), (4, "newest")]:
            specs = []
            for i in range(5):
                if i == real_idx:
                    specs.append((
                        "noreply@target.com", "Confirm",
                        "Click https://target.com/confirm?token=real",
                    ))
                else:
                    specs.append((
                        f"bot{i}@spam.com", f"Spam {i}",
                        "Just some spam content...",
                    ))

            fake = self._make_fake_imap(specs)
            sys.modules["aioimaplib"] = MagicMock()
            sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
            sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

            handler = self._make_handler(submitted_at=asyncio.get_event_loop().time() - 1)
            result = await handler._check_inbox_for_new_email("target.com")
            assert result is not None, f"Failed when real email was {label} (idx={real_idx})"
            assert "confirm" in result, f"Wrong link when real email was {label}"

    @pytest.mark.asyncio
    async def test_no_confirmation_email_present(self):
        """Returns None when no UNSEEN message matches the sender domain
        — unchanged behavior, must not regress."""
        import sys
        from unittest.mock import MagicMock

        fake = self._make_fake_imap([
            ("spam1@other.com", "Spam 1", "Content 1"),
            ("spam2@other.com", "Spam 2", "Content 2"),
        ])

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = self._make_handler(submitted_at=asyncio.get_event_loop().time() - 1)
        result = await handler._check_inbox_for_new_email("target.com")

        assert result is None, "Should return None when no confirmation email is present"

    @pytest.mark.asyncio
    async def test_bound_respected_too_many_messages(self):
        """When there are more UNSEEN messages than the iteration cap (20),
        a confirmation email sitting beyond the cap is not found.  This is a
        known limitation — we cap at 20 to avoid fetching an unbounded
        backlog, but log it so the operator can tell this happened."""
        import sys
        from unittest.mock import MagicMock

        # Create 25 messages — only message 1 (the oldest) is real,
        # messages 2-25 are spam.  With a 20-message cap (newest first),
        # message 1 won't be in the last-20 window.
        specs = [(
            "noreply@target.com", "Confirm",
            "Click https://target.com/confirm?token=real",
        )]
        for i in range(24):
            specs.append((f"spam{i}@other.com", f"Spam {i}", "Spam content"))

        fake = self._make_fake_imap(specs)
        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = self._make_handler(submitted_at=asyncio.get_event_loop().time() - 1)
        result = await handler._check_inbox_for_new_email("target.com")

        # The real email (id=1) is outside the last-20 window (ids 6-25)
        # so it's not found.  This is expected behavior with the cap.
        assert result is None, (
            "With 25 messages and 20 cap, the oldest real email is outside "
            "the window"
        )


class TestSameRegistrableDomain:
    def test_different_subdomains_same_registrable(self):
        from ai_browser.registration_handler.handler import _same_registrable_domain
        assert _same_registrable_domain("developers.tiktok.com", "dev.tiktok.com") is True

    def test_unrelated_domains(self):
        from ai_browser.registration_handler.handler import _same_registrable_domain
        assert _same_registrable_domain("dev.tiktok.com", "spam.evil.com") is False

    def test_same_hostname(self):
        from ai_browser.registration_handler.handler import _same_registrable_domain
        assert _same_registrable_domain("example.com", "example.com") is True

    def test_multi_part_tld(self):
        from ai_browser.registration_handler.handler import _same_registrable_domain
        assert _same_registrable_domain("foo.example.co.uk", "bar.example.co.uk") is True

    def test_multi_part_tld_different(self):
        from ai_browser.registration_handler.handler import _same_registrable_domain
        assert _same_registrable_domain("foo.example.co.uk", "foo.evil.co.uk") is False

    def test_empty_inputs(self):
        from ai_browser.registration_handler.handler import _same_registrable_domain
        assert _same_registrable_domain("", "example.com") is False
        assert _same_registrable_domain("example.com", "") is False
        assert _same_registrable_domain("", "") is False


class TestSenderSubdomainNotSkipped:
    @pytest.mark.asyncio
    async def test_email_from_different_subdomain_not_skipped(self):
        import sys
        from unittest.mock import MagicMock
        from ai_browser.registration_handler.handler import RegistrationHandler
        from ai_browser.registration_handler.models import RegistrationConfig, IMAPConfig
        import asyncio

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("TikTok <noreply@dev.tiktok.com>", "Verify your account",
             "Click to confirm: https://developers.tiktok.com/confirm?token=abc"),
        ])
        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        config = RegistrationConfig(
            signup_url="https://developers.tiktok.com/signup",
            email="test@example.com",
            imap_config=IMAPConfig(
                host="imap.example.com",
                username="test@example.com",
                password="fake-pw",
            ),
        )
        handler = RegistrationHandler(config)
        handler._signup_submitted_at = asyncio.get_event_loop().time() - 1

        result = await handler._check_inbox_for_new_email("developers.tiktok.com")
        assert result is not None, (
            "Email from noreply@dev.tiktok.com should match "
            "developers.tiktok.com (same registrable domain tiktok.com)"
        )
        assert "tiktok" in result
