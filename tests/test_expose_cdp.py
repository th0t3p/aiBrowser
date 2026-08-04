"""Tests for CDP port exposure and PID-file lifecycle in BrowserSession."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_browser.browser_session import BrowserSession, BrowserSessionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(*, expose_cdp: bool = False, **kwargs) -> BrowserSessionConfig:
    return BrowserSessionConfig(
        authorized_hostname="example.com",
        expose_cdp=expose_cdp,
        headless=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# SIGBUS regression guard: launch() vs launch_persistent_context()
# ---------------------------------------------------------------------------


class TestCDPLaunchPath:
    @pytest.mark.asyncio
    async def test_cdp_uses_launch_not_persistent_context(self):
        """CDP path calls chromium.launch(), NOT launch_persistent_context()."""
        config = _make_config(expose_cdp=True)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock(return_value=mock_browser)
        mock_chromium.launch_persistent_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_browser.contexts = [mock_context]
        mock_context.pages = []
        mock_context.on = MagicMock()

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        mock_chromium.launch.assert_called_once()
        mock_chromium.launch_persistent_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_cdp_launch_args_have_port_not_pipe(self):
        """CDP launch args contain --remote-debugging-port but NOT --remote-debugging-pipe."""
        config = _make_config(expose_cdp=True)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock(return_value=mock_browser)
        mock_chromium.launch_persistent_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_browser.contexts = [mock_context]
        mock_context.pages = []
        mock_context.on = MagicMock()

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        call_args = mock_chromium.launch.call_args
        args_list = call_args[1].get("args", [])
        args_str = " ".join(args_list)
        assert any("--remote-debugging-port=" in a for a in args_list), \
            f"--remote-debugging-port missing from {args_list}"
        assert "--remote-debugging-pipe" not in args_str, \
            f"--remote-debugging-pipe present (causes SIGBUS): {args_str}"

    @pytest.mark.asyncio
    async def test_cdp_launch_args_include_ignore_certificate_errors(self):
        """CDP launch args include --ignore-certificate-errors."""
        config = _make_config(expose_cdp=True)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock(return_value=mock_browser)
        mock_chromium.launch_persistent_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_browser.contexts = [mock_context]
        mock_context.pages = []
        mock_context.on = MagicMock()

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        call_args = mock_chromium.launch.call_args
        args_list = call_args[1].get("args", [])
        assert "--ignore-certificate-errors" in args_list, \
            f"--ignore-certificate-errors missing from {args_list}"


class TestNonCDPLaunchPath:
    @pytest.mark.asyncio
    async def test_non_cdp_still_uses_persistent_context(self):
        """Non-CDP path (custom backend) still uses launch_persistent_context()."""
        config = _make_config(expose_cdp=False)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock()
        mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_context.pages = []
        mock_context.on = MagicMock()

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        mock_chromium.launch.assert_not_called()
        mock_chromium.launch_persistent_context.assert_called_once()


# ---------------------------------------------------------------------------
# PID file lifecycle
# ---------------------------------------------------------------------------


class TestPIDFileLifecycle:
    @pytest.mark.asyncio
    async def test_pid_file_written_on_cdp_start(self, tmp_path: Path):
        """PID file is written after a successful CDP launch."""
        config = _make_config(expose_cdp=True, storage_dir=tmp_path)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock(return_value=mock_browser)
        mock_chromium.launch_persistent_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_browser.contexts = [mock_context]
        mock_context.pages = []
        mock_context.on = MagicMock()

        # Mock CDP session for PID file write
        mock_cdp = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={
            "processInfo": [
                {"type": "browser", "id": 99999},
            ],
        })
        mock_browser.new_browser_cdp_session = AsyncMock(return_value=mock_cdp)

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        pid_file = session._pid_file_path()
        assert pid_file.exists(), f"PID file not created at {pid_file}"
        assert pid_file.read_text().strip() == "99999"

    @pytest.mark.asyncio
    async def test_pid_file_cleared_on_clean_stop(self, tmp_path: Path):
        """PID file is removed on a clean stop() (CDP path)."""
        config = _make_config(expose_cdp=True, storage_dir=tmp_path)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock(return_value=mock_browser)
        mock_chromium.launch_persistent_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_browser.close = AsyncMock()
        mock_browser.contexts = [mock_context]
        mock_context.pages = []
        mock_context.on = MagicMock()
        mock_context.close = AsyncMock()
        mock_context.storage_state = AsyncMock(return_value={"cookies": []})
        mock_playwright.stop = AsyncMock()  # stop() is async

        mock_cdp = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={
            "processInfo": [{"type": "browser", "id": 12345}],
        })
        mock_browser.new_browser_cdp_session = AsyncMock(return_value=mock_cdp)

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        pid_file = session._pid_file_path()
        assert pid_file.exists()

        await session.stop()

        assert not pid_file.exists(), f"PID file not cleaned on stop: {pid_file}"

    @pytest.mark.asyncio
    async def test_pid_file_not_written_on_non_cdp_path(self, tmp_path: Path):
        """PID file is never created on the non-CDP path."""
        config = _make_config(expose_cdp=False, storage_dir=tmp_path)
        session = BrowserSession(config)

        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_context = MagicMock()
        mock_playwright.chromium = mock_chromium
        mock_chromium.launch = AsyncMock()
        mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_context.pages = []
        mock_context.on = MagicMock()
        mock_context.storage_state = AsyncMock(return_value={"cookies": []})

        with patch("ai_browser.browser_session.session.async_playwright",
                   return_value=AsyncMock(start=AsyncMock(return_value=mock_playwright))):
            await session.start()

        pid_file = session._pid_file_path()
        assert not pid_file.exists(), f"PID file should not exist on non-CDP path"


# ---------------------------------------------------------------------------
# Process helpers (live — no mocking)
# ---------------------------------------------------------------------------


class TestProcessHelpers:
    def test_own_pid_is_alive(self):
        """The current process's own PID should be reported as alive."""
        assert BrowserSession._is_process_alive(os.getpid()) is True

    def test_implausible_pid_is_dead(self):
        """An implausibly large PID should be reported as dead."""
        # PID upper bound on macOS is 99998; pick something far beyond
        assert BrowserSession._is_process_alive(9999999) is False

    def test_pytest_is_not_chromium(self):
        """The current (pytest) process should NOT be identified as Chromium."""
        assert BrowserSession._is_chromium_process(os.getpid()) is False


# ---------------------------------------------------------------------------
# Orphan safety: must never kill a non-Chromium process
# ---------------------------------------------------------------------------


class TestOrphanSafety:
    @pytest.mark.asyncio
    async def test_orphan_reap_never_kills_non_chromium(self, tmp_path: Path):
        """Writing the CURRENT process's PID as an 'orphan' must NOT result
        in any termination signal being sent to it.

        This test writes its own PID into the PID file, then runs the
        reap logic.  The safety check (is_chromium_process) must reject
        it before any SIGTERM/SIGKILL is ever attempted.

        We intercept os.kill to verify only signal-0 (liveness probe) is
        ever sent, never SIGTERM (15) or SIGKILL (9).
        """
        config = _make_config(expose_cdp=True, storage_dir=tmp_path)
        session = BrowserSession(config)

        # Write our own PID as a fake orphan
        pid_file = session._pid_file_path()
        pid_file.write_text(str(os.getpid()))

        # Intercept os.kill to verify no termination signals
        actual_signals: list[int] = []
        _original_kill = os.kill

        def _tracking_kill(pid: int, sig: int) -> None:
            actual_signals.append(sig)
            if sig == 0:
                _original_kill(pid, sig)  # allow liveness probe through

        try:
            with patch("os.kill", side_effect=_tracking_kill):
                await session._reap_orphan_if_needed()
        finally:
            # Clean up the test PID file
            pid_file.unlink(missing_ok=True)

        # Only signal 0 (liveness) is allowed; 15 (SIGTERM) and 9 (SIGKILL) are NOT
        for sig in actual_signals:
            assert sig in (0,), \
                f"Termination signal {sig} was sent to the test process!"
        assert 0 in actual_signals, \
            "Liveness probe (signal 0) should have been used"
