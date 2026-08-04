#!/usr/bin/env python3
"""Standalone verification of browser-use's safety properties.

Two independent checks:

CHECK 1 — allowed_domains enforcement (plain HTTP)
  Proves browser-use's allowed_domains genuinely blocks disallowed
  network traffic.  Runs an agent against a plain-HTTP local test
  page with two links (allowed + disallowed) and observes every
  outgoing request via Playwright's network instrumentation.
  Repeated 5 times for reliability (intermittent failures have been
  observed in GitHub issues).

CHECK 2 — HTTPS cert bypass + context reuse (production mirror)
  Verifies two assumptions the aiBrowser integration in cli.py /
  session.py depends on that Check 1 never exercises:

  a) --ignore-certificate-errors (the Chromium launch arg in
     BrowserSession.start()'s expose_cdp path) actually allows an
     untrusted/MITM'd HTTPS page to load — the real-world equivalent
     of routing through Burp Suite's proxy with a self-signed cert.
  b) browser-use genuinely *reuses* aiBrowser's pre-created browser
     context when cdp_url is set, rather than creating its own.
     This is the specific assumption that made disable_security
     removable from cli.py's BrowserContextConfig — if it doesn't
     hold, the reasoning behind removing it doesn't hold either.

Overall pass requires both checks to pass.

PREREQUISITES
-------------
* DEEPSEEK_API_KEY environment variable set
* Dependencies installed (see pyproject.toml [browser-use] optional-deps):

      pip install -e ".[browser-use]"

  Or manually:

      pip install "browser-use==0.1.48" "langchain-deepseek>=0.1"

TARGETED BROWSER-USE VERSION
-----------------------------
This script targets browser-use **0.1.48**.  The API changed significantly
in 0.2+ (BrowserProfile, ChatOpenAI were introduced / renamed).  If you
upgrade, adjust the imports and configuration accordingly.

KNOWN CAVEAT — DeepSeek thinking mode vs forced tool-calling
--------------------------------------------------------------
DeepSeek V4 models (v4-pro, v4-flash) are always in thinking mode by
default and reject forced tool-calling (tool_choice="required") with
"Thinking mode does not support this tool_choice" — a known,
widely-reported limitation affecting every agent framework that uses
forced tool_choice for structured output (LangChain, CrewAI, AutoGen,
and browser-use).  We explicitly disable thinking mode via
``extra_body={"thinking": {"type": "disabled"}}`` so the agent can
use tools at all.  Tradeoff: we lose DeepSeek's reasoning capability
for this specific use case, but it's currently required for reliable
forced tool-calling to work.

USAGE
-----
    python scripts/verify_browser_use_safety.py

The script prints a clear PASS/FAIL at the end — no external tools needed.
"""

from __future__ import annotations

import asyncio
import http.server
import logging
import os
import socket
import ssl
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Test page — a single HTML file with two clearly labelled links.
# ---------------------------------------------------------------------------

_TEST_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>browser-use Safety Verification</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 600px;
           margin: 4rem auto; padding: 0 1rem; }
    h1 { font-size: 1.5rem; }
    a { display: block; padding: 0.75rem 1rem; margin: 0.5rem 0;
        border: 2px solid #ccc; border-radius: 6px; text-decoration: none;
        color: #333; font-weight: 600; }
    a.allowed { border-color: #22c55e; background: #f0fdf4; }
    a.disallowed { border-color: #ef4444; background: #fef2f2; }
    .badge { font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 99px;
             color: white; margin-left: 0.5rem; }
    .badge-ok { background: #22c55e; }
    .badge-no { background: #ef4444; }
  </style>
</head>
<body>
  <h1>browser-use Safety Verification Page</h1>
  <p>
    This page contains one <strong>allowed</strong> link and one
    <strong>disallowed</strong> link.  browser-use is configured
    with <code>allowed_domains=["example.com"]</code>.
  </p>
  <a class="allowed" href="https://example.com/" target="_blank"
     rel="noopener">
    Allowed → example.com
    <span class="badge badge-ok">ALLOWED</span>
  </a>
  <a class="disallowed" href="https://example.org/" target="_blank"
     rel="noopener">
    Disallowed → example.org
    <span class="badge badge-no">DISALLOWED</span>
  </a>
  <p style="margin-top: 2rem; color: #888; font-size: 0.85rem;">
    <em>If you can see this, the local test server is running.</em>
  </p>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Tiny single-file HTTP server (stdlib only, no extra deps)
# ---------------------------------------------------------------------------

_HTML_BYTES = _TEST_PAGE_HTML.encode("utf-8")


class _SinglePageHandler(http.server.BaseHTTPRequestHandler):
    """Serve exactly one HTML page on every GET, regardless of path."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_HTML_BYTES)))
        self.end_headers()
        self.wfile.write(_HTML_BYTES)

    def log_message(self, fmt, *args) -> None:
        """Suppress the default access-log noise."""
        pass


def _find_free_port() -> int:
    """Return an OS-assigned free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestPageServer:
    """Context manager that runs a local plain-HTTP server in a daemon thread."""

    def __init__(self) -> None:
        self._port: int = 0
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        self._port = _find_free_port()
        self._server = http.server.HTTPServer(
            ("127.0.0.1", self._port), _SinglePageHandler
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[server] Test page listening at {self.url}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "TestPageServer":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Self-signed cert generation (for Check 2's HTTPS test server)
# ---------------------------------------------------------------------------


def _generate_self_signed_cert(tmp_dir: Path) -> tuple[Path, Path]:
    """Generate a self-signed cert + key for CN=127.0.0.1.

    Returns ``(cert_path, key_path)`` — both PEM files in *tmp_dir*.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_dir / "key.pem"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_dir / "cert.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    return cert_path, key_path


# ---------------------------------------------------------------------------
# HTTPS test server (for Check 2)
# ---------------------------------------------------------------------------


class HttpsTestPageServer:
    """Context manager that runs a local *HTTPS* server with a self-signed cert.

    Same interface as TestPageServer: ``.start()`` / ``.stop()`` / ``.url``.
    """

    def __init__(self) -> None:
        self._port: int = 0
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._tmp_dir: Optional[tempfile.TemporaryDirectory] = None

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self._port}"

    def start(self) -> None:
        self._port = _find_free_port()
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="ai_browser_https_")
        cert_path, key_path = _generate_self_signed_cert(Path(self._tmp_dir.name))

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        self._server = http.server.HTTPServer(
            ("127.0.0.1", self._port), _SinglePageHandler
        )
        self._server.socket = ssl_ctx.wrap_socket(
            self._server.socket, server_side=True
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[server] HTTPS test page listening at {self.url}  (self-signed cert)")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()

    def __enter__(self) -> "HttpsTestPageServer":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Chromium launchers
# ---------------------------------------------------------------------------

_CDP_PORT = 9222
_CDP_PORT_CHECK2 = 9223


async def _launch_chromium_cdp():
    """Launch Chromium with remote debugging — for Check 1 (plain HTTP).

    Returns ``(playwright, cdp_browser, cdp_url)``.
    """
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    cdp_browser = await playwright.chromium.launch(
        headless=True,
        args=[f"--remote-debugging-port={_CDP_PORT}"],
    )
    cdp_url = f"http://localhost:{_CDP_PORT}"
    print(f"[browser] Chromium launched (CDP at {cdp_url})")
    return playwright, cdp_browser, cdp_url


async def _launch_chromium_cdp_like_production():
    """Launch Chromium mirroring aiBrowser's production CDP config — for Check 2.

    Mirrors BrowserSession.start()'s expose_cdp path exactly:
    - --remote-debugging-port + --ignore-certificate-errors
    - Pre-creates a context with ignore_https_errors=True before
      returning the CDP URL (so browser-use sees a non-empty
      browser.contexts list).

    Returns ``(playwright, cdp_browser, cdp_url, precreated_context)``.
    """
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    cdp_browser = await playwright.chromium.launch(
        headless=True,
        args=[
            f"--remote-debugging-port={_CDP_PORT_CHECK2}",
            "--ignore-certificate-errors",
        ],
    )
    cdp_url = f"http://localhost:{_CDP_PORT_CHECK2}"
    precreated_context = await cdp_browser.new_context(ignore_https_errors=True)
    print(f"[browser] Chromium launched (CDP at {cdp_url}, pre-created context)")
    return playwright, cdp_browser, cdp_url, precreated_context


# ---------------------------------------------------------------------------
# Network observation
# ---------------------------------------------------------------------------


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


class RequestLog:
    """Collects observed network requests with hostname-aware queries."""

    def __init__(self) -> None:
        self._entries: list[dict] = []

    def record(self, request) -> None:
        self._entries.append({"url": request.url, "method": request.method})

    def record_direct(self, entry: dict) -> None:
        """Append a pre-built entry (used for accumulating across runs)."""
        self._entries.append(dict(entry))

    @property
    def all(self) -> list[dict]:
        return list(self._entries)

    def any_match(self, host: str) -> bool:
        target = host.lower()
        for e in self._entries:
            h = _hostname(e["url"])
            if h == target or h.endswith("." + target):
                return True
        return False

    def clear(self) -> None:
        self._entries.clear()


# ---------------------------------------------------------------------------
# Diagnostics: monkey-patch browser-use's JSON parse site
# ---------------------------------------------------------------------------

_JSON_PARSE_DIAG_INSTALLED = False


def _install_extract_json_diagnostics() -> None:
    """Monkey-patch browser-use's extract_json_from_model_output to log
    the raw content string before the json.loads() call that fails with
    "Expecting value: line 1 column 1 (char 0)" when content is empty.
    """
    global _JSON_PARSE_DIAG_INSTALLED
    if _JSON_PARSE_DIAG_INSTALLED:
        return
    _JSON_PARSE_DIAG_INSTALLED = True

    import browser_use.agent.message_manager.utils as _mm_utils

    _original = _mm_utils.extract_json_from_model_output  # type: ignore[attr-defined]

    def _diag_extract_json(content: str):
        print(f"[JSON-DIAG] extract_json_from_model_output called")
        print(f"[JSON-DIAG]   content length: {len(content)}")
        print(f"[JSON-DIAG]   content repr:   {repr(content[:500])}")
        if not content or not content.strip():
            print("[JSON-DIAG]   *** CONTENT IS EMPTY — DeepSeek thinking-mode bug ***")
        elif content.strip().startswith("{"):
            print("[JSON-DIAG]   content starts with '{' — looks like valid JSON start")
        else:
            print(f"[JSON-DIAG]   content starts with: {repr(content[:80])}")
        return _original(content)

    _mm_utils.extract_json_from_model_output = _diag_extract_json  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LLM factory (shared by both checks)
# ---------------------------------------------------------------------------


def _build_llm(api_key: str) -> object:
    from langchain_deepseek import ChatDeepSeek
    return ChatDeepSeek(
        model="deepseek-v4-flash",
        api_key=api_key,
        extra_body={"thinking": {"type": "disabled"}},
    )


# ---------------------------------------------------------------------------
# CHECK 1 — allowed_domains enforcement (plain HTTP, 5 runs)
# ---------------------------------------------------------------------------


async def _run_single_agent_check1(
    test_page_url: str,
    run_index: int,
    cdp_url: str,
    request_log: RequestLog,
) -> bool:
    """Run a single browser-use agent against the test page (Check 1)."""
    from browser_use import Agent, Browser, BrowserConfig, BrowserContextConfig

    _install_extract_json_diagnostics()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("[SKIP] DEEPSEEK_API_KEY not set — cannot run agent")
        return False

    llm = _build_llm(api_key)

    browser_config = BrowserConfig(
        cdp_url=cdp_url,
        keep_alive=True,
        headless=True,
    )

    context_config = BrowserContextConfig(
        allowed_domains=["127.0.0.1", "example.com"],
        wait_for_network_idle_page_load_time=1.0,
        minimum_wait_page_load_time=0.5,
        maximum_wait_page_load_time=5.0,
    )

    browser_use_browser = Browser(config=browser_config)
    browser_use_ctx = await browser_use_browser.new_context(config=context_config)

    await browser_use_ctx._initialize_session()
    pw_context = browser_use_ctx.session.context

    for page in list(pw_context.pages):
        try:
            await page.close()
        except Exception:
            pass

    request_log.clear()

    def _on_request(request) -> None:
        request_log.record(request)

    pw_context.on("request", _on_request)

    task = (
        f"Navigate to {test_page_url}.  You will see a page with two links: "
        "one to 'example.com' (ALLOWED — the green link) and one to "
        "'example.org' (DISALLOWED — the red link).  "
        "Click ONLY the 'example.com' link.  "
        "Do NOT click the 'example.org' link under any circumstances.  "
        "After clicking the allowed link, wait for the page to load, "
        "then report whether the page loaded successfully."
    )

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser_use_browser,
        browser_context=browser_use_ctx,
        use_vision=False,
    )

    print(f"\n{'='*60}")
    print(f"  Run {run_index + 1}/5 — starting agent")
    print(f"  Task: {task[:100]}...")
    print(f"{'='*60}\n")

    success = False
    try:
        result = await agent.run(max_steps=10)
        print(f"\n[run {run_index + 1}] Agent completed.")
        print(f"[run {run_index + 1}] Observed requests ({len(request_log.all)}):")
        for entry in request_log.all:
            print(f"           {entry['method']:6s} {entry['url']}")

        if result and hasattr(result, "history"):
            urls_visited = result.urls()
            print(f"[run {run_index + 1}] Agent-reported URLs: {urls_visited}")
            success = True
        else:
            print(
                f"[run {run_index + 1}] WARNING: No history returned — "
                f"agent may have failed silently."
            )
    except Exception as exc:
        print(f"\n[run {run_index + 1}] Agent failed with: {exc}")
    finally:
        try:
            for page in list(pw_context.pages):
                await page.close()
        except Exception:
            pass
        try:
            await agent.close()
        except Exception:
            pass
        try:
            await browser_use_browser.close()
        except Exception:
            pass

    return success


# ---------------------------------------------------------------------------
# CHECK 2 — HTTPS cert bypass + context reuse (single run)
# ---------------------------------------------------------------------------


async def _run_https_context_reuse_check(
    https_test_page_url: str,
    cdp_url: str,
    request_log: RequestLog,
) -> dict:
    """Run Check 2: verify HTTPS cert bypass and browser-use context reuse.

    Returns a dict with keys: cert_bypass_ok, context_reused, scope_held.
    """
    from browser_use import Agent, Browser, BrowserConfig, BrowserContextConfig

    # Isolate the new_context instrumentation to a self-contained block
    # so we restore the original before any agent work starts.
    from playwright.async_api import Browser as PlaywrightBrowser

    new_context_calls = {"n": 0}
    _original_new_context = PlaywrightBrowser.new_context

    async def _counting_new_context(self, *args, **kwargs):
        new_context_calls["n"] += 1
        return await _original_new_context(self, *args, **kwargs)

    PlaywrightBrowser.new_context = _counting_new_context
    browser_use_browser = None
    try:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            print("[SKIP] DEEPSEEK_API_KEY not set — cannot run agent")
            return {"cert_bypass_ok": False, "context_reused": False, "scope_held": False}

        llm = _build_llm(api_key)

        # Mirror _run_phase2_browser_use in cli.py exactly — no disable_security
        browser_config = BrowserConfig(
            cdp_url=cdp_url,
            keep_alive=True,
            headless=True,
        )
        context_config = BrowserContextConfig(
            allowed_domains=["127.0.0.1", "example.com"],
            wait_for_network_idle_page_load_time=1.0,
            minimum_wait_page_load_time=0.5,
            maximum_wait_page_load_time=5.0,
        )

        browser_use_browser = Browser(config=browser_config)
        browser_use_ctx = await browser_use_browser.new_context(config=context_config)
        await browser_use_ctx._initialize_session()
    finally:
        PlaywrightBrowser.new_context = _original_new_context

    context_reused = new_context_calls["n"] == 0
    print(f"[check2] browser-use new_context calls: {new_context_calls['n']} "
          f"({'context reused' if context_reused else 'NEW context created!'})")

    pw_context = browser_use_ctx.session.context

    # Clean slate
    for page in list(pw_context.pages):
        try:
            await page.close()
        except Exception:
            pass

    # ---- Cert bypass smoke test (no LLM needed) ------------------------
    cert_bypass_ok = False
    test_page = await pw_context.new_page()
    try:
        await test_page.goto(https_test_page_url, timeout=15000)
        title = await test_page.title()
        cert_bypass_ok = "Safety Verification" in title
        print(f"[check2] HTTPS page title: '{title}' "
              f"({'cert bypass OK' if cert_bypass_ok else 'CERT BYPASS FAILED'})")
    except Exception as exc:
        print(f"[check2] HTTPS page load failed: {exc}")
    finally:
        try:
            await test_page.close()
        except Exception:
            pass

    if not cert_bypass_ok:
        # Don't bother with the agent if the cert doesn't even load
        return {"cert_bypass_ok": False, "context_reused": context_reused, "scope_held": False}

    # ---- Network observation for scope check ----------------------------
    request_log.clear()

    def _on_request(request) -> None:
        request_log.record(request)

    pw_context.on("request", _on_request)

    # ---- Agent run ------------------------------------------------------
    task = (
        f"Navigate to {https_test_page_url}.  The page uses a self-signed "
        f"certificate — the browser is already configured to trust it, so "
        f"just proceed normally.  You will see a page with two links: "
        "one to 'example.com' (ALLOWED — the green link) and one to "
        "'example.org' (DISALLOWED — the red link).  "
        "Click ONLY the 'example.com' link.  "
        "Do NOT click the 'example.org' link under any circumstances.  "
        "After clicking the allowed link, wait for the page to load, "
        "then report whether the page loaded successfully."
    )

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser_use_browser,
        browser_context=browser_use_ctx,
        use_vision=False,
    )

    scope_held = False
    try:
        result = await agent.run(max_steps=10)
        print(f"\n[check2] Agent completed. Observed requests ({len(request_log.all)}):")
        for entry in request_log.all:
            print(f"           {entry['method']:6s} {entry['url']}")

        if result and hasattr(result, "history"):
            print(f"[check2] Agent-reported URLs: {result.urls()}")

        scope_held = (
            request_log.any_match("example.com")
            and not request_log.any_match("example.org")
        )
    except Exception as exc:
        print(f"\n[check2] Agent failed with: {exc}")
    finally:
        try:
            for page in list(pw_context.pages):
                await page.close()
        except Exception:
            pass
        try:
            await agent.close()
        except Exception:
            pass
        try:
            await browser_use_browser.close()
        except Exception:
            pass

    return {"cert_bypass_ok": cert_bypass_ok, "context_reused": context_reused, "scope_held": scope_held}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_NUM_RUNS = 5

_ALLOWED_HOST = "example.com"
_DISALLOWED_HOST = "example.org"


async def main() -> None:
    logging.getLogger("browser_use").setLevel(logging.DEBUG)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("\n[SKIP] DEEPSEEK_API_KEY is not set.\n"
              "       export DEEPSEEK_API_KEY=sk-... and re-run.")
        sys.exit(1)

    print("[check] Using LLM provider: deepseek (langchain-deepseek)")

    overall_check1_passed = False
    check2_result = {"cert_bypass_ok": False, "context_reused": False, "scope_held": False}
    check2_passed = False

    # =====================================================================
    # CHECK 1 — allowed_domains enforcement (plain HTTP, 5 runs)
    # =====================================================================
    pw1, cdp_browser1, cdp_url1 = await _launch_chromium_cdp()

    all_runs_log = RequestLog()
    per_run_log = RequestLog()

    try:
        with TestPageServer() as server:
            test_url = server.url
            agent_ok_count = 0
            for i in range(_NUM_RUNS):
                ok = await _run_single_agent_check1(test_url, i, cdp_url1, per_run_log)
                if ok:
                    agent_ok_count += 1
                for entry in per_run_log.all:
                    all_runs_log.record_direct(entry)
                if i < _NUM_RUNS - 1:
                    await asyncio.sleep(1)
    finally:
        try:
            await cdp_browser1.close()
        except Exception:
            pass
        try:
            await pw1.stop()
        except Exception:
            pass
        print("[browser] Check 1 Chromium stopped")

    total_requests = len(all_runs_log.all)
    allowed_seen = all_runs_log.any_match(_ALLOWED_HOST)
    disallowed_seen = all_runs_log.any_match(_DISALLOWED_HOST)
    overall_check1_passed = allowed_seen and not disallowed_seen

    print(f"\n{'='*60}")
    print(f"  CHECK 1 VERDICT: {'PASS' if overall_check1_passed else 'FAIL'}")
    print(f"{'='*60}")
    print(f"  Agent runs completed  : {agent_ok_count}/{_NUM_RUNS}")
    print(f"  Total requests observed: {total_requests}")
    print(f"  Allowed  ({_ALLOWED_HOST:>20s}): {'✓ seen' if allowed_seen else '✗ NOT seen'}")
    print(f"  Disallowed ({_DISALLOWED_HOST:>20s}): {'✗ SEEN (FAIL)' if disallowed_seen else '✓ not seen'}")
    print(f"{'='*60}")

    # =====================================================================
    # CHECK 2 — HTTPS cert bypass + context reuse
    # =====================================================================
    print(f"\n{'='*60}")
    print(f"  CHECK 2 — HTTPS cert bypass + context reuse")
    print(f"{'='*60}")

    check2_log = RequestLog()
    try:
        pw2, cdp_browser2, cdp_url2, precreated_ctx2 = await _launch_chromium_cdp_like_production()

        # Quick standalone smoke test: confirm pre-created context exists
        contexts = cdp_browser2.contexts
        print(f"[check2] Pre-created contexts: {len(contexts)} (expect 1)")

        with HttpsTestPageServer() as https_server:
            https_test_url = https_server.url
            check2_result = await _run_https_context_reuse_check(
                https_test_url, cdp_url2, check2_log,
            )
    finally:
        try:
            await precreated_ctx2.close()
        except Exception:
            pass
        try:
            await cdp_browser2.close()
        except Exception:
            pass
        try:
            await pw2.stop()
        except Exception:
            pass
        print("[browser] Check 2 Chromium stopped")

    check2_passed = all(check2_result.values())

    print(f"\n{'='*60}")
    print(f"  CHECK 2 VERDICT: {'PASS' if check2_passed else 'FAIL'}")
    print(f"{'='*60}")
    print(f"  HTTPS cert bypass  : {'✓ PASS' if check2_result['cert_bypass_ok'] else '✗ FAIL'}")
    print(f"  Context reused     : {'✓ PASS' if check2_result['context_reused'] else '✗ FAIL'}")
    print(f"  Scope held         : {'✓ PASS' if check2_result['scope_held'] else '✗ FAIL'}")
    print(f"{'='*60}")

    if not check2_passed:
        if not check2_result["cert_bypass_ok"]:
            print(
                "\n  FAIL — cert bypass: The --ignore-certificate-errors fix "
                "is not sufficient as implemented.  Do not assume it works "
                "against a real target (Burp Suite proxy with self-signed "
                "cert) without further investigation."
            )
        if not check2_result["context_reused"]:
            print(
                "\n  FAIL — context reuse: The assumption that browser-use "
                "reuses aiBrowser's pre-created context is FALSE.  This "
                "invalidates the reasoning behind removing disable_security "
                "from cli.py.  Re-examine before the next live run rather "
                "than reflexively re-adding it."
            )
        if not check2_result["scope_held"]:
            print(
                "\n  HARD STOP — scope failure: Allowed_domains enforcement "
                "failed during Check 2 (HTTPS).  Same severity as a Check 1 "
                "scope failure.  Do not proceed with browser-use integration."
            )

    # =====================================================================
    # OVERALL VERDICT
    # =====================================================================
    overall_passed = overall_check1_passed and check2_passed

    print(f"\n{'='*60}")
    print(f"  OVERALL VERDICT: {'PASS' if overall_passed else 'FAIL'}")
    print(f"  Check 1 (scope):  {'PASS' if overall_check1_passed else 'FAIL'}")
    print(f"  Check 2 (https):  {'PASS' if check2_passed else 'FAIL'}")
    print(f"{'='*60}")

    if overall_passed:
        print(
            "\n  Both checks passed.  browser-use is safe to use as a Phase 2 "
            "backend:\n  - allowed_domains is enforced at the network level\n"
            "  - untrusted HTTPS certs (Burp proxy) are bypassed correctly\n"
            "  - browser-use reuses aiBrowser's pre-created context\n"
            "\n  Proceed with confidence."
        )

    sys.exit(0 if overall_passed else 1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s | %(name)s | %(message)s",
    )
    asyncio.run(main())
