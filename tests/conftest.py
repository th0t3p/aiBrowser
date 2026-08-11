"""Pytest fixtures for ai_browser tests."""
import pytest
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
def _mock_llm_for_tests(monkeypatch):
    """Default: call_llm returns NONE — AI fails closed everywhere."""
    monkeypatch.setattr(
        "ai_browser.registration_handler.handler.call_llm",
        AsyncMock(return_value="NONE"),
    )


@pytest.fixture(autouse=True)
def _mock_extraction_for_imap_tests(monkeypatch, request):
    """Tests that exercise IMAP → _extract_link_from_email need
    _ai_extract_confirmation_action mocked since the old regex
    link extraction no longer exists."""
    test_classes_needing_mock = (
        "TestIMAPChecksAllUnseenMessages",
        "TestSenderSubdomainNotSkipped",
        "TestCheckInboxTiering",
        "TestTikTokEndToEndTier2",
        "TestDiagnosticLogging",
        "TestBytesMessageIDFix",
    )
    if request.node.parent and request.node.parent.name in test_classes_needing_mock:
        monkeypatch.setattr(
            "ai_browser.registration_handler.handler._ai_extract_confirmation_action",
            AsyncMock(return_value=("link", "https://developers.tiktok.com/confirm?token=abc")),
        )
