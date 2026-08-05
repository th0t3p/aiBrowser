"""Reproduce: _check_inbox_for_new_email only checks the single latest UNSEEN
message, skipping older unread messages that may contain the real confirmation
email."""

import email
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Build a fake aioimaplib so we don't need a real IMAP server
# ---------------------------------------------------------------------------

_fake_imap = AsyncMock()
_fake_imap.wait_hello_from_server = AsyncMock()
_fake_imap.login = AsyncMock()
_fake_imap.select = AsyncMock()
_fake_imap.logout = AsyncMock()
_fake_imap.search = AsyncMock()
_fake_imap.fetch = AsyncMock()

# Queue up two UNSEEN messages:
#   id '1': the REAL TikTok confirmation email (arrived first)
#   id '2': an unrelated newsletter (arrived after, making it the "latest")
_fake_imap.search.return_value = ("OK", [b"1 2"])

# Build the two fake messages
def _make_raw_email(from_addr: str, subject: str, body: str) -> bytes:
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = "Tue, 5 Aug 2026 10:00:00 +0000"
    msg.attach(MIMEText(body, "plain"))
    return msg.as_bytes()

raw_1 = _make_raw_email(
    "noreply@developers.tiktok.com",
    "Verify your TikTok for Developers account",
    "Click here to confirm: https://developers.tiktok.com/confirm?token=abc123",
)
raw_2 = _make_raw_email(
    "newsletter@unrelated.com",
    "Your weekly digest",
    "Here is your weekly newsletter content...",
)

# fetch returns different raw messages depending on the message ID
async def _fetch(ids, _format):
    id_str = ids.decode() if isinstance(ids, bytes) else str(ids)
    if (isinstance(ids, bytes) and b"1" in ids) or "1" in id_str:
        return ("OK", [b"1 (RFC822)", raw_1])
    elif (isinstance(ids, bytes) and b"2" in ids) or "2" in id_str:
        return ("OK", [b"2 (RFC822)", raw_2])
    return ("NO", [])

_fake_imap.fetch = AsyncMock(side_effect=_fetch)

_fake_imap4_ssl = MagicMock(return_value=_fake_imap)
_fake_imap4 = MagicMock(return_value=_fake_imap)

# Patch aioimaplib into sys.modules
sys.modules["aioimaplib"] = MagicMock()
sys.modules["aioimaplib"].IMAP4_SSL = _fake_imap4_ssl
sys.modules["aioimaplib"].IMAP4 = _fake_imap4

# ---------------------------------------------------------------------------
# Now actually call _check_inbox_for_new_email
# ---------------------------------------------------------------------------

import asyncio
from ai_browser.registration_handler.handler import RegistrationHandler
from ai_browser.registration_handler.models import RegistrationConfig, IMAPConfig


async def main():
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
    # Pretend the signup was submitted 1 second ago
    handler._signup_submitted_at = asyncio.get_event_loop().time() - 1

    result = await handler._check_inbox_for_new_email("developers.tiktok.com")

    if result is not None and "tiktok" in result:
        print("FIXED — confirmation link found:", result)
        return 0
    else:
        print(
            "BUG REPRODUCED: the real TikTok confirmation email (id '1') was "
            "sitting unread in the mailbox, but the function only ever checked "
            "the LATEST unread message (id '2', an unrelated email) and gave "
            "up. It never looked at id '1' at all."
        )
        print(f"\nResult of _check_inbox_for_new_email(): {result!r}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
