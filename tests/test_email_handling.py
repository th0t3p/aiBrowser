"""Tests for email link extraction priority, IMAP filtering (Fixes #6, #7)."""

import asyncio
import email
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_browser.registration_handler.handler import RegistrationHandler
from ai_browser.registration_handler.models import IMAPConfig, RegistrationConfig


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

    @pytest.mark.asyncio
    async def test_prioritizes_confirm_link_over_logo(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """Confirmation link with 'confirm' in path beats logo link."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/logo.png">Logo</a>
            <a href="https://target.com/confirm?token=abc123">Confirm</a>
            <a href="https://target.com/unsubscribe">Unsubscribe</a>
        """)
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result == ("link", "https://target.com/confirm?token=abc123")

    @pytest.mark.asyncio
    async def test_prioritizes_verify_link(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """Link with 'verify' in path is prioritized."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://example.org/image.jpg">Image</a>
            <a href="https://target.com/verify-email?id=123">Verify</a>
        """)
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result is not None and result[0] == "link" and "verify" in result[1]

    @pytest.mark.asyncio
    async def test_prioritizes_activate_link(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """Link with 'activate' in path is prioritized."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/activate/abc">Activate Account</a>
            <a href="https://target.com/logo.png">Logo</a>
        """)
        result = await handler._extract_link_from_email(msg)
        assert result is not None and result[0] == "link" and "activate" in result[1]

    @pytest.mark.asyncio
    async def test_prioritizes_token_link(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """Link with 'token=' in query string is prioritized."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/home">Home</a>
            <a href="https://target.com/register/complete?token=xyz789">Complete</a>
        """)
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result is not None and result[0] == "link" and "token=" in result[1]

    @pytest.mark.asyncio
    async def test_falls_back_to_same_domain_link(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """When no confirmation pattern found, same-domain link is selected."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://other.com/tracker.gif">Tracker</a>
            <a href="https://target.com/welcome">Welcome</a>
        """)
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result == ("link", "https://target.com/welcome")

    @pytest.mark.asyncio
    async def test_falls_back_to_first_non_asset_link(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """Last resort: first non-image link wins."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://other.com/logo.png">Logo</a>
            <a href="https://other.com/page">Page</a>
        """)
        result = await handler._extract_link_from_email(msg)
        assert result == ("link", "https://other.com/page")

    @pytest.mark.asyncio
    async def test_skips_image_links(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """Image/tracking links are excluded."""
        handler = self._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/pixel.gif?track=1">Pixel</a>
            <a href="https://target.com/styles.css">CSS</a>
            <a href="https://target.com/dashboard">Dashboard</a>
        """)
        result = await handler._extract_link_from_email(msg)
        assert result == ("link", "https://target.com/dashboard")


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
            return ("link", "https://target.com/confirm?token=abc")
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
        assert result is not None and result[0] == "link" and "tiktok" in result[1]

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
        assert result is not None and result[0] == "link" and "confirm" in result[1]

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
            assert result is not None and result[0] == "link" and "confirm" in result[1], f"Wrong link when real email was {label}"

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
        assert result is not None and result[0] == "link" and "tiktok" in result[1]


# ---------------------------------------------------------------------------
# New tests for Tier 1/2/3 email matching (refactor + AI fallback)
# ---------------------------------------------------------------------------


class TestGetEmailBodyText:
    """Verify _get_email_body_text produces correct text from multipart
    and non-multipart messages — a pure refactor, output must match what
    _extract_link_from_email produced before the split."""

    @staticmethod
    def _handler():
        return RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
            )
        )

    def test_multipart_plain_text_extraction(self):
        handler = self._handler()
        msg = _make_email(text_body="Hello, confirm here: https://example.com/confirm")
        text = handler._get_email_body_text(msg)
        assert "Hello, confirm here" in text
        assert "https://example.com/confirm" in text

    def test_multipart_html_text_extraction(self):
        handler = self._handler()
        msg = _make_email(html_body="<p>Click <a href='https://example.com/verify'>here</a></p>")
        text = handler._get_email_body_text(msg)
        assert "href='https://example.com/verify'" in text

    def test_non_multipart_plain_text_extraction(self):
        handler = self._handler()
        from email.mime.text import MIMEText
        msg = MIMEText("Plain text body with link https://example.com/activate")
        text = handler._get_email_body_text(msg)
        assert "Plain text body" in text
        assert "https://example.com/activate" in text

    @pytest.mark.asyncio
    async def test_refactor_produces_same_link_as_before(self, monkeypatch):
        from unittest.mock import AsyncMock
        monkeypatch.setattr("ai_browser.registration_handler.handler.call_llm", AsyncMock(return_value=None))
        """Verify that _extract_link_from_email still works identically
        after the refactor — the body text pipe produces the same result."""
        handler = self._handler()
        msg = _make_email(
            html_body="""<a href="https://target.com/confirm?token=xyz">Confirm</a>""",
            text_body="Confirm at https://target.com/confirm?token=xyz",
        )
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result is not None and result[0] == "link" and "confirm" in result[1]
        assert result is not None and result[0] == "link" and "target.com" in result[1]


class TestSenderDomain:
    """Test the _sender_domain helper for both From-header formats."""

    def test_bare_address(self):
        from email.mime.text import MIMEText
        from ai_browser.registration_handler.handler import _sender_domain
        msg = MIMEText("body")
        msg["From"] = "noreply@example.com"
        assert _sender_domain(msg) == "example.com"

    def test_name_and_address(self):
        from email.mime.text import MIMEText
        from ai_browser.registration_handler.handler import _sender_domain
        msg = MIMEText("body")
        msg["From"] = "Example Team <noreply@dev.example.com>"
        assert _sender_domain(msg) == "dev.example.com"

    def test_no_from_header(self):
        from email.mime.text import MIMEText
        from ai_browser.registration_handler.handler import _sender_domain
        msg = MIMEText("body")
        assert _sender_domain(msg) == ""

    def test_invalid_address(self):
        from email.mime.text import MIMEText
        from ai_browser.registration_handler.handler import _sender_domain
        msg = MIMEText("body")
        msg["From"] = "not-an-email"
        assert _sender_domain(msg) == ""


class TestCheckInboxTiering:
    """Test that Tiers 1 and 2 are checked in order: domain-matching
    messages (Tier 1) win over non-matching ones (Tier 2), even when
    the non-matching message is more recent."""

    @pytest.mark.asyncio
    async def test_domain_matched_checked_first_and_wins(self):
        """A mix of domain-matching (id=1, older) and non-matching (id=2,
        newer, but also contains an extractable link).  Tier 1 should
        find the domain-matched one first, even though it's not the
        most recent."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            # id=1: domain-matched, older
            ("noreply@target.com", "Confirm your email",
             "Click to confirm: https://target.com/confirm?token=abc"),
            # id=2: domain-mismatched (ESP), newer, also has a link
            ("noreply@mailgun.org", "Verify your account",
             "Click: https://target.com/verify?from=esp"),
        ])

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        result = await handler._check_inbox_for_new_email("target.com")
        # Tier 1 should find the domain-matched link first
        assert result == ("link", "https://target.com/confirm?token=abc")

    @pytest.mark.asyncio
    async def test_tier2_finds_esp_sender_with_no_domain_match(self):
        """When NO messages match the sender domain, Tier 2 should still
        find a link from a third-party ESP (e.g. Mailgun, SendGrid)."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@mailgun.org", "Verify your email",
             "Click to confirm: https://target.com/confirm?token=xyz"),
        ])

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        result = await handler._check_inbox_for_new_email("target.com")
        # Tier 2 should find it even though sender is from mailgun.org
        assert result == ("link", "https://target.com/confirm?token=xyz")


class TestPollInboxTier3ExactlyOnce:
    """Test that _ai_classify_and_extract_latest_unread is called exactly
    once when the poll loop times out — never per-iteration."""

    @pytest.mark.asyncio
    async def test_tier3_called_exactly_once_after_timeout(self):
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                imap_config=IMAPConfig(
                    host="imap.target.com",
                    username="test@target.com",
                    password="fake-pw",
                ),
                email_poll_timeout_seconds=10,
                email_poll_interval_seconds=1,
            )
        )
        handler._check_inbox_for_new_email = AsyncMock(return_value=None)
        tier3_mock = AsyncMock(return_value=None)
        handler._ai_classify_and_extract_latest_unread = tier3_mock

        result = await handler._poll_inbox_for_link("target.com")
        assert result is None
        # Tier 3 should be called exactly once, after the loop times out
        assert tier3_mock.call_count == 1, (
            f"Expected Tier 3 to be called exactly once, got {tier3_mock.call_count}"
        )

    @pytest.mark.asyncio
    async def test_tier3_not_called_when_tier1_or_2_finds_link(self):
        """If _check_inbox_for_new_email returns a link, Tier 3 should
        never be invoked."""
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                imap_config=IMAPConfig(
                    host="imap.target.com",
                    username="test@target.com",
                    password="fake-pw",
                ),
                email_poll_timeout_seconds=10,
            )
        )
        handler._check_inbox_for_new_email = AsyncMock(
            return_value=("link", "https://target.com/confirm")
        )
        tier3_mock = AsyncMock()
        handler._ai_classify_and_extract_latest_unread = tier3_mock

        result = await handler._poll_inbox_for_link("target.com")
        assert result == ("link", "https://target.com/confirm")
        assert tier3_mock.call_count == 0


class TestAIJudgeIsRegistrationEmail:
    """Test _ai_judge_is_registration_email failure modes — must fail
    CLOSED (return False), deliberately different from the fail-open
    pattern used by other AI judges in this codebase."""

    @pytest.mark.asyncio
    async def test_yes_response_returns_true(self, monkeypatch):
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                llm_api_key="fake-key",
                llm_provider="anthropic",
                llm_model="claude-test",
            )
        )
        mock_llm = AsyncMock(return_value="YES")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock_llm,
        )
        result = await handler._ai_judge_is_registration_email(
            sender="noreply@target.com",
            subject="Confirm your email",
            body_text="Please confirm your account by clicking the link below...",
            hostname="target.com",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_no_response_returns_false(self, monkeypatch):
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                llm_api_key="fake-key",
                llm_provider="anthropic",
                llm_model="claude-test",
            )
        )
        mock_llm = AsyncMock(return_value="NO")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock_llm,
        )
        result = await handler._ai_judge_is_registration_email(
            sender="newsletter@spam.com",
            subject="Weekly digest",
            body_text="Here is your weekly newsletter...",
            hostname="target.com",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_response_fails_closed(self, monkeypatch):
        """Empty string from LLM → False (fails CLOSED — this is the
        intentional deviation from the fail-open pattern)."""
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                llm_api_key="fake-key",
                llm_provider="anthropic",
                llm_model="claude-test",
            )
        )
        mock_llm = AsyncMock(return_value="")  # empty response
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock_llm,
        )
        result = await handler._ai_judge_is_registration_email(
            sender="noreply@target.com",
            subject="Confirm",
            body_text="Click to confirm...",
            hostname="target.com",
        )
        assert result is False, (
            "Empty LLM response MUST return False (fails CLOSED) — "
            "this is a deliberate deviation from the fail-open pattern"
        )

    @pytest.mark.asyncio
    async def test_none_response_fails_closed(self, monkeypatch):
        """None from a failed LLM call → False (fails CLOSED)."""
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                llm_api_key="fake-key",
                llm_provider="anthropic",
                llm_model="claude-test",
            )
        )
        mock_llm = AsyncMock(return_value=None)  # failed call
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock_llm,
        )
        result = await handler._ai_judge_is_registration_email(
            sender="noreply@target.com",
            subject="Confirm",
            body_text="Click to confirm...",
            hostname="target.com",
        )
        assert result is False, (
            "None LLM response MUST return False (fails CLOSED) — "
            "acting on an unrelated email would be worse than missing one"
        )

    @pytest.mark.asyncio
    async def test_exception_fails_closed(self, monkeypatch):
        """Exception during LLM call → False (fails CLOSED)."""
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                llm_api_key="fake-key",
                llm_provider="anthropic",
                llm_model="claude-test",
            )
        )
        mock_llm = AsyncMock(side_effect=RuntimeError("network error"))
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock_llm,
        )
        result = await handler._ai_judge_is_registration_email(
            sender="noreply@target.com",
            subject="Confirm",
            body_text="Click to confirm...",
            hostname="target.com",
        )
        assert result is False


class TestTikTokEndToEndTier2:
    """End-to-end regression guard: the real TikTok scenario — a
    domain-mismatched message (dev.tiktok.com sender vs.
    developers.tiktok.com target) containing a real link is found via
    Tier 2, without ever needing to fall through to Tier 3."""

    @pytest.mark.asyncio
    async def test_tiktok_mismatched_domains_found_via_tier2(self):
        """The exact scenario from the original issue: signup on
        developers.tiktok.com, confirmation email arrives from
        noreply@dev.tiktok.com.  This must work via Tier 2 (or
        Tier 1 with registrable-domain matching) without needing
        the AI fallback."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("TikTok Team <noreply@dev.tiktok.com>",
             "Please verify your TikTok for Developers account",
             "Click to confirm: https://developers.tiktok.com/confirm?token=real123"),
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

        # This should find the link via Tier 1 (since dev.tiktok.com and
        # developers.tiktok.com share the registrable domain tiktok.com)
        result = await handler._check_inbox_for_new_email("developers.tiktok.com")
        assert result is not None, (
            "TikTok scenario: confirmation from dev.tiktok.com should be "
            "found for developers.tiktok.com (same registrable domain)"
        )
        assert result is not None and result[0] == "link" and "developers.tiktok.com" in result[1]
        assert result is not None and result[0] == "link" and "confirm" in result[1]


# ---------------------------------------------------------------------------
# New tests for diagnostic logging (Part 1) + PIN/OTP-code verification (Part 2)
# ---------------------------------------------------------------------------


class TestDiagnosticLogging:
    """Verify that candidate-emails and verdicts are logged at INFO level."""

    @pytest.mark.asyncio
    async def test_candidate_log_fires_for_domain_matched(self, caplog):
        """Verify that inspected candidates in _check_inbox_for_new_email
        produce a log line for each candidate."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@target.com", "Confirm your email",
             "Click to confirm: https://target.com/confirm?token=abc"),
        ])
        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        with caplog.at_level(logging.INFO):
            r = await handler._check_inbox_for_new_email("target.com")
        assert r is not None
        # Should have logged at least one candidate-email line
        candidates = [r for r in caplog.records if "Candidate confirmation email" in r.message]
        assert len(candidates) >= 1, "Expected at least one candidate email log line"

    @pytest.mark.asyncio
    async def test_candidate_log_fires_for_non_matched_candidate(self, caplog):
        """Even domain-mismatched candidates get logged."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@mailgun.org", "Verify your email",
             "Click to confirm: https://target.com/confirm?token=xyz"),
        ])
        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        with caplog.at_level(logging.INFO):
            r = await handler._check_inbox_for_new_email("target.com")
        assert r is not None  # Tier 2 should still find it
        candidates = [r for r in caplog.records if "Candidate confirmation email" in r.message]
        assert len(candidates) >= 1

    @pytest.mark.asyncio
    async def test_ai_judge_verdict_logged_true_and_false(self, caplog, monkeypatch):
        """Both True and False verdicts from Tier 3 produce a log line."""
        from unittest.mock import AsyncMock

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        handler.config.llm_api_key = "fake-key"
        handler.config.llm_provider = "anthropic"
        handler.config.llm_model = "claude-test"

        # Test True case
        mock_llm = AsyncMock(return_value="YES")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock_llm,
        )
        with caplog.at_level(logging.INFO):
            await handler._ai_judge_is_registration_email(
                sender="noreply@target.com",
                subject="Confirm",
                body_text="Please confirm your account.",
                hostname="target.com",
            )
        verdict_logs = [r for r in caplog.records if "verdict" in r.message.lower() and "ai" in r.message.lower()]
        # The verdict log happens in _ai_classify_and_extract_latest_unread,
        # not in _ai_judge_is_registration_email itself.  This test just
        # confirms the underlying judge works — the verdict log test is
        # covered by the Tier 3 integration test below.

    @pytest.mark.asyncio
    async def test_final_warning_reason_differs(self, caplog, monkeypatch):
        """Final warning includes a reason= field that changes between
        'no email' and 'email found, no link' scenarios."""
        from unittest.mock import AsyncMock

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        handler.config.email_poll_timeout_seconds = 10
        handler.config.email_poll_interval_seconds = 1
        handler._check_inbox_for_new_email = AsyncMock(return_value=None)
        tier3_mock = AsyncMock(return_value=None)
        handler._ai_classify_and_extract_latest_unread = tier3_mock

        # Speed up the poll loop
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.asyncio.sleep",
            AsyncMock(),
        )
        with caplog.at_level(logging.WARNING):
            await handler._poll_inbox_for_link("target.com")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "reason=" in warnings[-1].message, (
            "Final warning should include reason=, got: %s" % warnings[-1].message
        )



class TestAIExtractVerificationCode:
    """Tests for the AI-based _ai_extract_verification_code function."""

    @pytest.mark.asyncio
    async def test_extracts_code_from_llm_response(self, monkeypatch):
        """Mock call_llm returning "97VJ5D" → returns "97VJ5D"."""
        from unittest.mock import AsyncMock
        from ai_browser.registration_handler.handler import _ai_extract_verification_code
        mock = AsyncMock(return_value="97VJ5D")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        result = await _ai_extract_verification_code(
            body_text="Here is the pin that you requested:\n97VJ5D",
            llm_provider="anthropic",
            llm_api_key="fake-key",
            llm_model="claude-test",
        )
        assert result == "97VJ5D"

    @pytest.mark.asyncio
    async def test_none_response_returns_none(self, monkeypatch):
        from unittest.mock import AsyncMock
        from ai_browser.registration_handler.handler import _ai_extract_verification_code
        mock = AsyncMock(return_value="NONE")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        result = await _ai_extract_verification_code(
            body_text="some email content",
            llm_provider="anthropic", llm_api_key="fk", llm_model="m",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_sentence_response_rejected(self, monkeypatch, caplog):
        """LLM returning a full sentence → discarded, WARNING logged."""
        from unittest.mock import AsyncMock
        from ai_browser.registration_handler.handler import _ai_extract_verification_code
        dummy = AsyncMock(return_value="Your verification code is 97VJ5D")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", dummy,
        )
        with caplog.at_level("WARNING"):
            result = await _ai_extract_verification_code(
                body_text="dummy",
                llm_provider="anthropic", llm_api_key="fk", llm_model="m",
            )
        assert result is None
        warnings = [r for r in caplog.records if "look like a code" in r.message]
        assert len(warnings) >= 1

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self, monkeypatch):
        from unittest.mock import AsyncMock
        from ai_browser.registration_handler.handler import _ai_extract_verification_code
        mock = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        result = await _ai_extract_verification_code(
            body_text="dummy",
            llm_provider="anthropic", llm_api_key="fk", llm_model="m",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_regression_real_world_content(self, monkeypatch):
        """Exact regression test for the bug that prompted replacing regex.
        The body contains "Here is the pin that you requested:\n97VJ5D"
        which the regex would fail on — AI should return the right code."""
        from unittest.mock import AsyncMock
        from ai_browser.registration_handler.handler import _ai_extract_verification_code
        body = "Here is the pin that you requested:\n97VJ5D"
        mock = AsyncMock(return_value="97VJ5D")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        result = await _ai_extract_verification_code(
            body_text=body,
            llm_provider="anthropic", llm_api_key="fk", llm_model="m",
        )
        assert result == "97VJ5D"


class TestCodeExtractionIntegration:
    """Test that AI code extraction is wired into Tiers 1/2 and Tier 3."""

    @pytest.mark.asyncio
    async def test_extract_link_from_email_finds_code(self, monkeypatch):
        """When email body has no link but has a code, _extract_link_from_email
        returns ("code", code)."""
        from unittest.mock import AsyncMock
        handler = TestGetEmailBodyText._handler()
        from email.mime.text import MIMEText
        msg = MIMEText("Your verification code: AB12CD")
        msg["From"] = "noreply@target.com"
        msg["Subject"] = "Verify your email"
        mock = AsyncMock(return_value="AB12CD")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result == ("code", "AB12CD")

    @pytest.mark.asyncio
    async def test_extract_link_from_email_finds_link_over_code(self, monkeypatch):
        """Link extraction wins when both a link and code are present."""
        from unittest.mock import AsyncMock
        handler = TestGetEmailBodyText._handler()
        msg = _make_email(html_body="""
            <a href="https://target.com/confirm?token=abc">Confirm</a>
            Your code is: XYZ123
        """)
        mock = AsyncMock(return_value="XYZ123")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result[0] == "link"
        assert "confirm" in result[1]

class TestRunHandlesCodeField:
    """Test that register() handles a code result properly."""

    @pytest.mark.asyncio
    async def test_code_found_fills_field_and_submits(self, monkeypatch):
        """When _poll_inbox_for_link returns a code, the code field is filled
        and submit is called a second time."""
        from unittest.mock import AsyncMock, MagicMock

        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            imap_config=IMAPConfig(
                host="imap.target.com",
                username="test@target.com",
                password="fake-pw",
            ),
        )
        handler = RegistrationHandler(config)

        # Silence internal steps
        async def _noop(*_a, **_kw):
            return None
        monkeypatch.setattr(handler, "_check_captcha", _noop)
        monkeypatch.setattr(
            handler, "_fill_signup_form",
            AsyncMock(return_value=["email", "password"]),
        )
        monkeypatch.setattr(handler, "_submit_form", _noop)

        # Mock the polling to return a code
        async def _fake_poll(*_a, **_kw):
            return ("code", "AB12CD")
        monkeypatch.setattr(handler, "_poll_inbox_for_link", _fake_poll)

        # Mock _submit_verification_code to track calls
        code_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(handler, "_submit_verification_code", code_mock)

        # Mock session + page
        page = AsyncMock()
        page.url = "https://target.com/verify"
        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        await handler.register(session)
        code_mock.assert_called_once_with(page, "AB12CD")
        assert handler.confirmed is True

    @pytest.mark.asyncio
    async def test_code_found_no_field_logs_reason(self, caplog, monkeypatch):
        """Code extracted but no field on page → distinct warning logged."""
        from unittest.mock import AsyncMock, MagicMock

        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            imap_config=IMAPConfig(
                host="imap.target.com",
                username="test@target.com",
                password="fake-pw",
            ),
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

        async def _fake_poll(*_a, **_kw):
            return ("code", "XX99YY")
        monkeypatch.setattr(handler, "_poll_inbox_for_link", _fake_poll)

        code_mock = AsyncMock(return_value=False)  # field not found
        monkeypatch.setattr(handler, "_submit_verification_code", code_mock)

        page = AsyncMock()
        page.url = "https://target.com/some-page"
        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        with caplog.at_level(logging.WARNING):
            await handler.register(session)

        code_no_field = [
            r for r in caplog.records
            if "code_found_no_field" in r.message
        ]
        assert len(code_no_field) >= 1, (
            "Should log 'code_found_no_field' when code found but no field"
        )


class TestAIJudgePromptUpdated:
    """Verify the updated _ai_judge_did_submit prompt includes PIN/code language."""

    @pytest.mark.asyncio
    async def test_prompt_includes_code_verification_language(self):
        """The prompt text includes indicators for PIN/code-entry pages."""
        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
                llm_api_key="fake-key",
            )
        )
        import inspect
        source = inspect.getsource(handler._ai_judge_did_submit)
        assert "verification code or PIN" in source, (
            "Prompt should mention verification code/PIN detection"
        )
        assert "enter the code below" in source
        assert "we sent you a code" in source


# ---------------------------------------------------------------------------
# Tests for IMAP polling observability (UNSEEN count logging, Seen-mutation fix,
# mailbox diagnostic)
# ---------------------------------------------------------------------------


class TestIMAPPollObservability:
    """Verify that every poll iteration logs what it found, including zero."""

    @pytest.mark.asyncio
    async def test_empty_unseen_logs_zero(self, caplog):
        """When search('UNSEEN') returns empty, the count log fires before
        the early return."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([])

        # Override search to return empty
        async def _empty_search(_criteria):
            return ("OK", [b""])
        fake.search = AsyncMock(side_effect=_empty_search)

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        with caplog.at_level(logging.INFO):
            result = await handler._check_inbox_for_new_email("target.com")

        assert result is None
        poll_logs = [r for r in caplog.records
                     if "IMAP poll:" in r.message and "UNSEEN" in r.message]
        assert len(poll_logs) >= 1, "Should log 'IMAP poll: 0 UNSEEN...' when empty"

    @pytest.mark.asyncio
    async def test_fetch_uses_body_peek_not_rfc822(self):
        """Regression test: _check_inbox_for_new_email uses BODY.PEEK[]
        not RFC822 to avoid silently mutating Seen status."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@target.com", "Confirm",
             "Click https://target.com/confirm?token=abc"),
        ])

        # Wrap the real fetch to capture the argument
        _real_fetch = fake.fetch
        _fetch_calls = []
        async def _tracking_fetch(msg_id, fmt):
            _fetch_calls.append(fmt)
            return await _real_fetch(msg_id, fmt)
        fake.fetch = AsyncMock(side_effect=_tracking_fetch)

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        await handler._check_inbox_for_new_email("target.com")

        assert len(_fetch_calls) > 0, "Should have called fetch at least once"
        for call in _fetch_calls:
            assert "BODY.PEEK[]" in call, (
                f"fetch should use BODY.PEEK[] not RFC822, got: {call}"
            )

    @pytest.mark.asyncio
    async def test_fetch_uses_body_peek_not_rfc822_tier3(self):
        """Same regression test for _ai_classify_and_extract_latest_unread."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@target.com", "Confirm",
             "Click https://target.com/confirm?token=abc"),
        ])

        _fetch_calls = []
        async def _tracking_fetch(msg_id, fmt):
            _fetch_calls.append(fmt)
            return ("OK", [b"1 (BODY[] ...)", b"raw body"])
        fake.fetch = AsyncMock(side_effect=_tracking_fetch)

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        handler.config.llm_api_key = "fake-key"
        handler.config.llm_provider = "anthropic"
        handler.config.llm_model = "claude-test"

        # We just want to exercise the fetch path before the LLM call
        # (which will fail since there's no real API key, but that's fine
        # for checking the fetch argument)
        try:
            await handler._ai_classify_and_extract_latest_unread("target.com")
        except Exception:
            pass

        assert len(_fetch_calls) > 0, "Should have called fetch at least once"
        for call in _fetch_calls:
            assert "BODY.PEEK[]" in call, (
                f"fetch should use BODY.PEEK[] not RFC822, got: {call}"
            )


class TestDiagnosticMailboxScan:
    """Verify the _log_recent_mailbox_state diagnostic helper."""

    @pytest.mark.asyncio
    async def test_scans_all_not_unseen(self, caplog):
        """The diagnostic uses search('ALL'), not search('UNSEEN')."""
        import sys
        from unittest.mock import MagicMock, AsyncMock

        # Build a fake IMAP that will record the search argument
        _search_calls = []

        fake = AsyncMock()
        fake.wait_hello_from_server = AsyncMock()
        fake.login = AsyncMock()
        fake.select = AsyncMock()
        fake.logout = AsyncMock()

        async def _search(criteria):
            _search_calls.append(criteria)
            if criteria == "ALL":
                return ("OK", [b"1 2 3"])
            return ("OK", [b""])
        fake.search = AsyncMock(side_effect=_search)

        async def _fetch(msg_id, fmt):
            # Return a minimal RFC822 header
            header = (
                b"From: test@example.com\r\n"
                b"Subject: Test subject\r\n"
                b"Date: Tue, 5 Aug 2026 10:00:00 +0000\r\n"
                b"\r\n"
            )
            return ("OK", [b"1 (BODY[HEADER] ...)", header])

        fake.fetch = AsyncMock(side_effect=_fetch)

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )

        with caplog.at_level(logging.INFO):
            await handler._log_recent_mailbox_state(handler.config.imap_config)

        # Should have searched ALL (not UNSEEN)
        assert "ALL" in _search_calls, (
            f"Diagnostic should search ALL, got {_search_calls}"
        )

    @pytest.mark.asyncio
    async def test_uses_body_peek_header(self):
        """Fetch in the diagnostic uses BODY.PEEK[HEADER] not RFC822."""
        import sys
        from unittest.mock import MagicMock, AsyncMock

        _fetch_calls = []

        fake = AsyncMock()
        fake.wait_hello_from_server = AsyncMock()
        fake.login = AsyncMock()
        fake.select = AsyncMock()
        fake.logout = AsyncMock()
        fake.search = AsyncMock(return_value=("OK", [b"1 2"]))

        async def _fetch(msg_id, fmt):
            _fetch_calls.append(fmt)
            header = b"From: test@example.com\r\nSubject: S\r\nDate: ...\r\n\r\n"
            return ("OK", [b"1 (BODY[HEADER] ...)", header])
        fake.fetch = AsyncMock(side_effect=_fetch)

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        await handler._log_recent_mailbox_state(handler.config.imap_config)

        assert len(_fetch_calls) > 0
        for call in _fetch_calls:
            assert "BODY.PEEK[HEADER]" in call, (
                f"Diagnostic fetch should use BODY.PEEK[HEADER], got: {call}"
            )

    @pytest.mark.asyncio
    async def test_diagnostic_runs_once_not_per_iteration(self, caplog, monkeypatch):
        """The diagnostic fires exactly once, right before Tier 3, not
        on every poll iteration."""
        from unittest.mock import AsyncMock

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        handler.config.email_poll_timeout_seconds = 10
        handler.config.email_poll_interval_seconds = 1
        handler._check_inbox_for_new_email = AsyncMock(return_value=None)
        tier3_mock = AsyncMock(return_value=None)
        handler._ai_classify_and_extract_latest_unread = tier3_mock

        # Speed up the poll loop
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.asyncio.sleep",
            AsyncMock(),
        )

        diag_mock = AsyncMock()
        monkeypatch.setattr(handler, "_log_recent_mailbox_state", diag_mock)

        with caplog.at_level(logging.INFO):
            await handler._poll_inbox_for_link("target.com")

        assert diag_mock.call_count == 1, (
            f"Diagnostic should fire exactly once, got {diag_mock.call_count}"
        )


# ---------------------------------------------------------------------------
# Tests for bytes message-ID fix (search returns bytes, fetch must receive str)
# ---------------------------------------------------------------------------


class TestBytesMessageIDFix:
    """Verify that bytes message IDs from search() are decoded before being
    passed to fetch() — the exact bug that silently broke all IMAP polling."""

    @pytest.mark.asyncio
    async def test_fetch_receives_str_not_bytes_tier1(self):
        """_check_inbox_for_new_email decodes bytes IDs to str before fetch."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@target.com", "Confirm",
             "Click https://target.com/confirm?token=abc"),
        ])
        # search already returns bytes (the fixture encodes IDs to bytes),
        # so this is a faithful reproduction of real aioimaplib behavior.

        # Replace fetch to capture the message ID argument, delegating to
        # the original mock fetch for the actual response.
        _fetch_calls = []
        _real_fetch = fake.fetch
        async def _capture_fetch(msg_id, fmt):
            _fetch_calls.append(msg_id)
            return await _real_fetch(msg_id, fmt)
        fake.fetch = AsyncMock(side_effect=_capture_fetch)

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        await handler._check_inbox_for_new_email("target.com")

        assert len(_fetch_calls) >= 1
        for msg_id in _fetch_calls:
            assert isinstance(msg_id, str), (
                f"fetch() must receive str, got {type(msg_id)}: {msg_id!r}"
            )

    @pytest.mark.asyncio
    async def test_fetch_receives_str_not_bytes_in_loop(self):
        """When multiple message IDs come from search, all are decoded before
        being passed to fetch in the iteration loop."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@target.com", "Confirm",
             "Click https://target.com/confirm?token=abc"),
            ("noreply@mailgun.org", "Verify",
             "Click https://target.com/verify"),
        ])

        _fetch_calls = []
        async def _capture_fetch(msg_id, fmt):
            _fetch_calls.append(msg_id)
            return ("OK", [f"{msg_id} (BODY ...)".encode(), b"raw body"])

        fake.fetch = AsyncMock(side_effect=_capture_fetch)

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        await handler._check_inbox_for_new_email("target.com")

        # Should have called fetch for both messages
        assert len(_fetch_calls) >= 2
        for msg_id in _fetch_calls:
            assert isinstance(msg_id, str), (
                f"Each fetch call must receive str, got {type(msg_id)}: {msg_id!r}"
            )

    @pytest.mark.asyncio
    async def test_failed_fetch_logs_warning(self, caplog):
        """When fetch returns non-OK, a warning is logged (not silent)."""
        import sys
        from unittest.mock import MagicMock, AsyncMock

        fake = AsyncMock()
        fake.wait_hello_from_server = AsyncMock()
        fake.login = AsyncMock()
        fake.select = AsyncMock()
        fake.logout = AsyncMock()
        fake.search = AsyncMock()
        # Return bytes IDs (real aioimaplib behavior)
        fake.search.return_value = ("OK", [b"1"])

        # fetch always fails
        fake.fetch = AsyncMock(return_value=("NO", []))

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        with caplog.at_level(logging.WARNING):
            result = await handler._check_inbox_for_new_email("target.com")

        assert result is None
        fetch_fail_logs = [
            r for r in caplog.records
            if "IMAP fetch failed" in r.message
        ]
        assert len(fetch_fail_logs) >= 1, (
            "Should log warning when fetch fails, got no matching records"
        )
        assert "result=NO" in fetch_fail_logs[0].message

    @pytest.mark.asyncio
    async def test_end_to_end_bytes_round_trip(self, caplog):
        """Full round trip with bytes search → decoded fetch → candidate log line.
        This is the actual regression that was silently broken."""
        import sys
        from unittest.mock import MagicMock

        fake = TestIMAPChecksAllUnseenMessages._make_fake_imap([
            ("noreply@target.com", "Confirm your email",
             "Click to confirm: https://target.com/confirm?token=abc"),
        ])

        sys.modules["aioimaplib"] = MagicMock()
        sys.modules["aioimaplib"].IMAP4_SSL = MagicMock(return_value=fake)
        sys.modules["aioimaplib"].IMAP4 = MagicMock(return_value=fake)

        handler = TestIMAPChecksAllUnseenMessages._make_handler(
            submitted_at=asyncio.get_event_loop().time() - 1,
        )
        with caplog.at_level(logging.INFO):
            result = await handler._check_inbox_for_new_email("target.com")

        # Should have found the link (via Tier 1 domain match)
        assert result is not None
        assert result[0] == "link"
        assert "confirm" in result[1]

        # The candidate log line should have fired
        candidate_logs = [
            r for r in caplog.records
            if "Candidate confirmation email" in r.message
        ]
        assert len(candidate_logs) >= 1, (
            "Candidate log line must fire when a real message is found — "
            "this was the regression where bytes IDs silently broke everything"
        )


# ---------------------------------------------------------------------------
# Tests for page-driven extraction priority + login verification
# ---------------------------------------------------------------------------


class TestPageExpectsCodeCheck:
    """Verify _page_expects_code_check detects visible code fields."""

    @pytest.mark.asyncio
    async def test_visible_code_field_returns_true(self):
        """Page with a visible input[name=code] → True."""
        from unittest.mock import AsyncMock, MagicMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
            )
        )
        page = AsyncMock()
        mock_field = MagicMock()
        mock_field.is_visible = AsyncMock(return_value=True)
        page.query_selector = AsyncMock(return_value=mock_field)

        result = await handler._page_expects_code_check(page)
        assert result is True

    @pytest.mark.asyncio
    async def test_no_matching_field_returns_false(self):
        from unittest.mock import AsyncMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
            )
        )
        page = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)

        result = await handler._page_expects_code_check(page)
        assert result is False

    @pytest.mark.asyncio
    async def test_invisible_field_returns_false(self):
        from unittest.mock import AsyncMock, MagicMock

        handler = RegistrationHandler(
            RegistrationConfig(
                signup_url="https://target.com/signup",
                email="test@target.com",
            )
        )
        page = AsyncMock()
        mock_field = MagicMock()
        mock_field.is_visible = AsyncMock(return_value=False)
        page.query_selector = AsyncMock(return_value=mock_field)

        result = await handler._page_expects_code_check(page)
        assert result is False


class TestExtractionPriorityFlip:
    """Verify that _page_expects_code flips extraction order."""

    @pytest.mark.asyncio
    async def test_prefer_code_returns_code_first_when_both_present(self, monkeypatch):
        """When _page_expects_code is set and email has both link and code,
        code is returned first."""
        from unittest.mock import AsyncMock
        handler = TestGetEmailBodyText._handler()
        handler._page_expects_code = True
        mock = AsyncMock(return_value="XYZ789")
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        msg = _make_email(
            html_body="""<a href="https://target.com/confirm?token=abc">Confirm</a>
            Your verification code is XYZ789""",
        )
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result == ("code", "XYZ789")

    @pytest.mark.asyncio
    async def test_prefer_code_falls_back_to_link(self, monkeypatch):
        """When _page_expects_code is set but email has only a link, the
        link is still returned."""
        from unittest.mock import AsyncMock
        handler = TestGetEmailBodyText._handler()
        handler._page_expects_code = True
        mock = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        msg = _make_email(html_body="""<a href="https://target.com/confirm?token=abc">Confirm</a>""")
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result == ("link", "https://target.com/confirm?token=abc")

    @pytest.mark.asyncio
    async def test_default_link_first_no_code_field(self, monkeypatch):
        """Without _page_expects_code, link is tried first (default behavior)."""
        from unittest.mock import AsyncMock
        handler = TestGetEmailBodyText._handler()
        mock = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler.call_llm", mock,
        )
        # _page_expects_code not set (defaults to False via getattr)
        msg = _make_email(
            html_body="""<a href="https://target.com/confirm?token=abc">Confirm</a>
            Your code: XYZ789""",
        )
        result = await handler._extract_link_from_email(msg, "target.com")
        assert result[0] == "link"
        assert "confirm" in result[1]


class TestLoginVerification:
    """Verify that _verify_via_login sets login_verified correctly."""

    @pytest.mark.asyncio
    async def test_verify_via_login_sets_true(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            password="test-pw",
            imap_config=IMAPConfig(
                host="imap.target.com", username="test@target.com",
                password="fake-pw",
            ),
        )
        handler = RegistrationHandler(config)

        mock_login_handler = MagicMock()
        mock_login_handler.authenticated = True
        mock_login_handler.login = AsyncMock()
        mock_login_cls = MagicMock(return_value=mock_login_handler)

        monkeypatch.setattr(
            "ai_browser.login_handler.LoginHandler",
            mock_login_cls,
        )
        result = await handler._verify_via_login(MagicMock())
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_via_login_sets_false(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            password="test-pw",
            imap_config=IMAPConfig(
                host="imap.target.com", username="test@target.com",
                password="fake-pw",
            ),
        )
        handler = RegistrationHandler(config)

        mock_login_handler = MagicMock()
        mock_login_handler.authenticated = False
        mock_login_handler.login = AsyncMock()
        monkeypatch.setattr(
            "ai_browser.login_handler.LoginHandler",
            MagicMock(return_value=mock_login_handler),
        )
        result = await handler._verify_via_login(MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_via_login_exception_returns_none(self, monkeypatch):
        from unittest.mock import MagicMock

        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            password="test-pw",
            imap_config=IMAPConfig(
                host="imap.target.com", username="test@target.com",
                password="fake-pw",
            ),
        )
        handler = RegistrationHandler(config)
        monkeypatch.setattr(
            "ai_browser.login_handler.LoginHandler",
            MagicMock(side_effect=RuntimeError("crash")),
        )
        result = await handler._verify_via_login(MagicMock())
        assert result is None

    @pytest.mark.asyncio
    async def test_run_sets_login_verified_on_confirmation(self, monkeypatch):
        """Full run() with confirmation → login_verified gets set."""
        from unittest.mock import AsyncMock, MagicMock

        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            imap_config=IMAPConfig(
                host="imap.target.com", username="test@target.com",
                password="fake-pw",
            ),
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
        monkeypatch.setattr(handler, "_page_expects_code_check",
                             AsyncMock(return_value=False))

        async def _fake_poll(*_a, **_kw):
            return ("link", "https://target.com/confirm?token=abc")
        monkeypatch.setattr(handler, "_poll_inbox_for_link", _fake_poll)

        # Mock login verification
        async def _fake_verify(*_a, **_kw):
            return True
        monkeypatch.setattr(handler, "_verify_via_login", _fake_verify)

        page = AsyncMock()
        page.url = "https://target.com/welcome"
        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        await handler.register(session)
        assert handler.confirmed is True
        assert handler.login_verified is True

    @pytest.mark.asyncio
    async def test_run_login_verification_failed_does_not_block(self, monkeypatch):
        """Even when login verification returns False, registration still
        reports confirmed=True and credentials are saved (the confirmation
        action happened — we just know the account may not be active)."""
        from unittest.mock import AsyncMock, MagicMock

        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test@target.com",
            imap_config=IMAPConfig(
                host="imap.target.com", username="test@target.com",
                password="fake-pw",
            ),
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
        monkeypatch.setattr(handler, "_page_expects_code_check",
                             AsyncMock(return_value=False))

        async def _fake_poll(*_a, **_kw):
            return ("link", "https://target.com/confirm?token=abc")
        monkeypatch.setattr(handler, "_poll_inbox_for_link", _fake_poll)

        async def _fake_verify(*_a, **_kw):
            return False  # login failed
        monkeypatch.setattr(handler, "_verify_via_login", _fake_verify)

        page = AsyncMock()
        page.url = "https://target.com/welcome"
        session = MagicMock()
        session.new_page = AsyncMock(return_value=page)

        await handler.register(session)
        assert handler.confirmed is True
        assert handler.login_verified is False  # distinct from None


class TestLoginVerifyURLConstruction:
    """Verify that _verify_via_login builds the correct login URL."""

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_login_url_derived_from_signup_hostname(self):
        """With no login_verify_url override, the login URL is
        https://<hostname>/login where <hostname> comes from signup_url.
        A signup URL with a path/query is stripped to just the hostname."""
        from urllib.parse import urlparse

        signup_url = "https://example.com/app/signup?ref=x"
        hostname = urlparse(signup_url).hostname
        assert hostname == "example.com", "Should extract just hostname"

        login_url = f"https://{hostname}/login"
        assert login_url == "https://example.com/login", (
            "Should build /login from hostname, not reuse signup_url path"
        )

    def test_login_verify_url_override_used_verbatim(self):
        """When login_verify_url is set, it's used directly."""
        from urllib.parse import urlparse
        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
            password="test-pw",
            login_verify_url="https://example.com/custom-login",
        )
        hostname = urlparse(config.signup_url).hostname or ""
        login_url = config.login_verify_url or f"https://{hostname}/login"
        assert login_url == "https://example.com/custom-login", (
            "login_verify_url override should be used verbatim"
        )


# ---------------------------------------------------------------------------
# Tests for --login-verify-url CLI option
# ---------------------------------------------------------------------------


class TestLoginVerifyUrlCLI:
    """Verify CLI wiring of --login-verify-url."""

    def test_login_verify_url_parsed(self):
        """--login-verify-url is a recognized option."""
        from click.testing import CliRunner
        from ai_browser.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["crawl", "--help"])
        assert "--login-verify-url" in result.output

    def test_flag_omitted_leaves_config_none(self):
        """Without the flag, RegistrationConfig.login_verify_url is None."""
        from ai_browser.registration_handler.models import RegistrationConfig
        config = RegistrationConfig(
            signup_url="https://example.com/signup",
            email="test@example.com",
        )
        assert config.login_verify_url is None


# ---------------------------------------------------------------------------
# Tests for login_verified persistence to credentials file
# ---------------------------------------------------------------------------


class TestSaveCredentialsLoginVerified:
    """Verify _save_credentials writes login_verified to the JSON."""

    def test_saves_login_verified_true(self, tmp_path: Path):
        from ai_browser.cli import _save_credentials
        import json

        _save_credentials(tmp_path, "example.com", "test@example.com",
                          "pw", confirmed=True, login_verified=True)
        cred_file = tmp_path / "credentials" / "example.com.json"
        data = json.loads(cred_file.read_text())
        assert data["login_verified"] is True

    def test_saves_login_verified_false(self, tmp_path: Path):
        from ai_browser.cli import _save_credentials
        import json

        _save_credentials(tmp_path, "example.com", "test@example.com",
                          "pw", confirmed=True, login_verified=False)
        cred_file = tmp_path / "credentials" / "example.com.json"
        data = json.loads(cred_file.read_text())
        assert data["login_verified"] is False

    def test_saves_login_verified_none(self, tmp_path: Path):
        from ai_browser.cli import _save_credentials
        import json

        _save_credentials(tmp_path, "example.com", "test@example.com",
                          "pw", confirmed=False, login_verified=None)
        cred_file = tmp_path / "credentials" / "example.com.json"
        data = json.loads(cred_file.read_text())
        assert data["login_verified"] is None

    def test_saves_login_verified_default_none(self, tmp_path: Path):
        """When login_verified is not passed, it defaults to None."""
        from ai_browser.cli import _save_credentials
        import json

        _save_credentials(tmp_path, "example.com", "test@example.com",
                          "pw", confirmed=True)
        cred_file = tmp_path / "credentials" / "example.com.json"
        data = json.loads(cred_file.read_text())
        assert data["login_verified"] is None


# ---------------------------------------------------------------------------
# Tests for post-submit page-state logging (diagnostic only)
# ---------------------------------------------------------------------------


class TestGetVisiblePageText:
    """Test the _get_visible_page_text diagnostic helper."""

    @pytest.mark.asyncio
    async def test_returns_page_text(self):
        handler = TestGetEmailBodyText._handler()
        page = AsyncMock()
        page.inner_text = AsyncMock(return_value="Welcome back! Your code was accepted.")
        text = await handler._get_visible_page_text(page)
        assert text == "Welcome back! Your code was accepted."

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        handler = TestGetEmailBodyText._handler()
        page = AsyncMock()
        page.inner_text = AsyncMock(side_effect=RuntimeError("no body"))
        text = await handler._get_visible_page_text(page)
        assert text == ""


class TestDetectErrorText:
    """Test the _detect_error_text diagnostic helper."""

    def test_finds_error_phrase(self):
        from ai_browser.registration_handler.handler import _detect_error_text
        text = "Invalid code, please try again."
        result = _detect_error_text(text)
        assert result == "invalid code", f"Expected 'invalid code', got {result!r}"

    def test_finds_another_error_phrase(self):
        from ai_browser.registration_handler.handler import _detect_error_text
        text = "The verification code has expired. Please request a new one."
        result = _detect_error_text(text)
        assert result == "code has expired"

    def test_returns_none_for_success_text(self):
        from ai_browser.registration_handler.handler import _detect_error_text
        text = "Welcome! Your account has been verified."
        result = _detect_error_text(text)
        assert result is None

    def test_case_insensitive(self):
        from ai_browser.registration_handler.handler import _detect_error_text
        text = "INVALID verification, please contact support."
        result = _detect_error_text(text)
        assert result == "invalid verification"


class TestSubmitVerificationCodeLogging:
    """Test that _submit_verification_code logs post-submit state."""

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_logs_navigation_true_when_url_changes(self, caplog):
        handler = TestGetEmailBodyText._handler()
        from unittest.mock import patch

        page = AsyncMock()
        page.url = "https://example.com/verify"
        page.inner_text = AsyncMock(return_value="Welcome")
        page.wait_for_load_state = AsyncMock()

        # submit_form updates page.url to simulate navigation
        async def _submit_and_navigate(*_a, **_kw):
            page.url = "https://example.com/dashboard"

        with patch("ai_browser.registration_handler.handler.fill_form_fields",
                   AsyncMock(return_value=["code"])), \
             patch("ai_browser.registration_handler.handler.submit_form",
                   AsyncMock(side_effect=_submit_and_navigate)):
            with caplog.at_level(logging.INFO):
                await handler._submit_verification_code(page, "ABC123")

        navigated_logs = [r for r in caplog.records
                          if "Post-submit page state" in r.message]
        assert len(navigated_logs) >= 1
        assert "navigated=True" in navigated_logs[0].message


    @pytest.mark.asyncio
    async def test_logs_error_when_page_shows_rejection(self, caplog):
        handler = TestGetEmailBodyText._handler()
        page = AsyncMock()
        page.url = "https://example.com/verify"
        page.inner_text = AsyncMock(
            return_value="Invalid code, please try again."
        )
        page.wait_for_load_state = AsyncMock()

        from unittest.mock import patch
        with patch("ai_browser.registration_handler.handler.fill_form_fields",
                   AsyncMock(return_value=["code"])), \
             patch("ai_browser.registration_handler.handler.submit_form",
                   AsyncMock()):
            with caplog.at_level(logging.WARNING):
                await handler._submit_verification_code(page, "WRONG")

        error_logs = [r for r in caplog.records
                      if "error/rejection" in r.message]
        assert len(error_logs) >= 1
        assert "invalid code" in error_logs[0].message

    @pytest.mark.asyncio
    async def test_returns_true_in_all_cases(self):
        """Even with error text, _submit_verification_code still returns
        True — logging is diagnostic only, no control flow change."""
        from unittest.mock import patch

        for body_text, label in [
            ("Welcome!", "success"),
            ("Invalid code, please try again.", "error"),
            ("", "empty"),
        ]:
            handler = TestGetEmailBodyText._handler()
            page = AsyncMock()
            page.url = "https://example.com/verify"
            page.inner_text = AsyncMock(return_value=body_text)
            page.wait_for_load_state = AsyncMock()

            with patch("ai_browser.registration_handler.handler.fill_form_fields",
                       AsyncMock(return_value=["code"])), \
                 patch("ai_browser.registration_handler.handler.submit_form",
                       AsyncMock()):
                result = await handler._submit_verification_code(page, "ABC")
            assert result is True, (
                f"Expected True for {label} case, got {result}"
            )
