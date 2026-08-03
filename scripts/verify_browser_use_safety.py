#!/usr/bin/env python3
"""Standalone verification: does browser-use's allowed_domains actually prevent
disallowed traffic from reaching the network?

This script:
  1. Launches Chromium via Playwright directly (aiBrowser's proven config
     with --remote-debugging-port), NOT browser-use's own launch flags
     (which cause BUS_ADRALN crashes with chromium-1228).
  2. Starts a local HTTP server serving a test page with two links:
     - One to an "allowed"  domain (https://example.com)
     - One to a "disallowed" domain (https://example.org)
  3. Hooks Playwright's network observation directly on the browser
     context that browser-use will reuse, recording every outgoing
     request independently of browser-use's self-reported logs.
  4. Runs a browser-use Agent via cdp_url, configured with
     allowed_domains=["example.com"].
  5. Repeats 5 times (intermittent failures observed in GitHub issues).
  6. Prints an automated PASS/FAIL verdict based on observed network
     requests — no manual Burp inspection required.

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
import sys
import threading
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
    """Context manager that runs a local HTTP server in a daemon thread."""

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
# Chromium launcher — reuses aiBrowser's proven launch config.
# ---------------------------------------------------------------------------

_CDP_PORT = 9222


async def _launch_chromium_cdp():
    """Launch Chromium with remote debugging enabled.

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


# ---------------------------------------------------------------------------
# Network observation — hooks the Playwright context browser-use reuses.
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
# browser-use Agent runner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Diagnostics: monkey-patch browser-use's JSON parse site
# ---------------------------------------------------------------------------

_JSON_PARSE_DIAG_INSTALLED = False


def _install_extract_json_diagnostics() -> None:
    """Monkey-patch browser-use's extract_json_from_model_output to log
    the raw content string before the json.loads() call that fails with
    "Expecting value: line 1 column 1 (char 0)" when content is empty.

    This is applied once and prints a distinctive ``[JSON-DIAG]`` prefix
    on every call so we can confirm whether the raw LLM response content
    is truly empty (DeepSeek thinking-mode bug) or contains text that
    just isn't valid JSON.
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
# browser-use Agent runner
# ---------------------------------------------------------------------------


async def _run_single_agent(
    test_page_url: str,
    run_index: int,
    cdp_url: str,
    request_log: RequestLog,
) -> bool:
    """Run a single browser-use agent against the test page.

    *request_log* is cleared at the start of this run and populated
    with every outgoing request observed on the Playwright context
    that browser-use reuses (because cdp_url is set).
    """
    from browser_use import Agent, Browser, BrowserConfig, BrowserContextConfig

    # ---- Diagnostics: monkey-patch the JSON parse site -----------------
    # browser-use's extract_json_from_model_output at
    # agent/message_manager/utils.py:41 is where json.loads(content)
    # fails with "Expecting value: line 1 column 1" when DeepSeek's
    # thinking mode produces an empty response.  We patch it to print
    # the raw content BEFORE the parse attempt so we can confirm
    # whether the content is truly empty or just non-JSON text.
    _install_extract_json_diagnostics()

    # ---- LLM ----------------------------------------------------------
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("[SKIP] DEEPSEEK_API_KEY not set — cannot run agent")
        return False
    from langchain_deepseek import ChatDeepSeek

    # deepseek-chat was retired July 24, 2026 — use v4-flash for this
    # quick two-link test (cheaper/faster; v4-pro also works).
    #
    # DeepSeek V4 models are always in thinking mode by default, which is
    # fundamentally incompatible with forced tool-calling (tool_choice="required"
    # or naming a specific function) that browser-use relies on internally.
    # "Thinking mode does not support this tool_choice" is a known DeepSeek V4
    # limitation — not a bug in this script, browser-use, or langchain-deepseek.
    # Disabling thinking mode costs us DeepSeek's reasoning capability for this
    # run, but it's the only way to make forced tool-calling work reliably.
    llm = ChatDeepSeek(
        model="deepseek-v4-flash",
        api_key=api_key,
        extra_body={"thinking": {"type": "disabled"}},
    )

    # ---- Browser config -----------------------------------------------
    browser_config = BrowserConfig(
        cdp_url=cdp_url,
        keep_alive=True,
        headless=True,
    )

    context_config = BrowserContextConfig(
        # 127.0.0.1 is required so the agent can reach the local test server
        # to even see the two links.  example.org is deliberately excluded —
        # correctly blocking navigation to it is the whole point of this test.
        allowed_domains=["127.0.0.1", "example.com"],
        wait_for_network_idle_page_load_time=1.0,
        minimum_wait_page_load_time=0.5,
        maximum_wait_page_load_time=5.0,
    )

    browser_use_browser = Browser(config=browser_config)
    browser_use_ctx = await browser_use_browser.new_context(config=context_config)

    # ---- Hook Playwright network observation --------------------------
    # When cdp_url is set, browser-use's _create_context reuses the
    # first context from the CDP browser.  We initialize the session
    # early so we can access the underlying Playwright context and
    # attach our request listener before the agent starts navigating.
    await browser_use_ctx._initialize_session()
    pw_context = browser_use_ctx.session.context

    # Clean slate: close every page left over from the previous run
    # so the agent doesn't find a stale page and skip fresh navigation.
    for page in list(pw_context.pages):
        try:
            await page.close()
        except Exception:
            pass

    request_log.clear()

    def _on_request(request) -> None:
        request_log.record(request)

    pw_context.on("request", _on_request)

    # ---- Task ---------------------------------------------------------
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
            # AgentHistoryList.urls() returns [h.state.url ...] for each step.
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
        # Clean up for next run: close all pages while browser-use's
        # Playwright connection is still alive, so the CDP browser
        # actually tears them down.  The next run's _initialize_session
        # will then find an empty context with no stale pages to reuse.
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
# Main
# ---------------------------------------------------------------------------

_NUM_RUNS = 5

_ALLOWED_HOST = "example.com"
_DISALLOWED_HOST = "example.org"


async def main() -> None:
    # --- enable browser-use debug logging --------------------------------
    logging.getLogger("browser_use").setLevel(logging.DEBUG)

    # --- pre-flight check --------------------------------------------------
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print(
            "\n[SKIP] DEEPSEEK_API_KEY is not set.\n"
            "       export DEEPSEEK_API_KEY=sk-... and re-run."
        )
        sys.exit(1)

    print("[check] Using LLM provider: deepseek (langchain-deepseek)")

    # --- launch Chromium with CDP ------------------------------------------
    pw, cdp_browser, cdp_url = await _launch_chromium_cdp()

    # --- shared request log across all runs --------------------------------
    all_runs_log = RequestLog()
    per_run_log = RequestLog()

    # --- start test page server -------------------------------------------
    with TestPageServer() as server:
        test_url = server.url

        # --- run agent 5 times --------------------------------------------
        agent_ok_count = 0
        try:
            for i in range(_NUM_RUNS):
                ok = await _run_single_agent(test_url, i, cdp_url, per_run_log)
                if ok:
                    agent_ok_count += 1
                # Accumulate requests across all runs
                for entry in per_run_log.all:
                    all_runs_log.record_direct(entry)
                if i < _NUM_RUNS - 1:
                    await asyncio.sleep(1)
        finally:
            # --- tear down CDP browser ------------------------------------
            try:
                await cdp_browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
            print("[browser] Chromium stopped")

    # --- automated verdict -------------------------------------------------
    total_requests = len(all_runs_log.all)
    allowed_seen = all_runs_log.any_match(_ALLOWED_HOST)
    disallowed_seen = all_runs_log.any_match(_DISALLOWED_HOST)

    # PASS: at least one allowed request AND zero disallowed requests
    passed = allowed_seen and not disallowed_seen

    print(f"\n{'='*60}")
    print(f"  VERDICT: {'PASS' if passed else 'FAIL'}")
    print(f"{'='*60}")
    print(f"  Agent runs completed  : {agent_ok_count}/{_NUM_RUNS}")
    print(f"  Total requests observed: {total_requests}")
    print(f"  Allowed  ({_ALLOWED_HOST:>20s}): {'✓ seen' if allowed_seen else '✗ NOT seen'}")
    print(f"  Disallowed ({_DISALLOWED_HOST:>20s}): {'✗ SEEN (FAIL)' if disallowed_seen else '✓ not seen'}")
    print(f"{'='*60}")

    if passed:
        print(
            "\n  browser-use allowed_domains enforcement appears to be "
            "working.\n  Proceed to integration planning."
        )
    else:
        if disallowed_seen:
            print(
                "\n  HARD STOP: Disallowed domain appeared in observed "
                "network traffic.\n  Do NOT integrate browser-use into "
                "aiBrowser's Phase 2.\n  Report: browser-use version, "
                "exact requests observed, and any error output above."
            )
        elif not allowed_seen:
            print(
                "\n  No allowed-domain traffic observed — the agent may "
                "not have\n  successfully navigated to the allowed link.  "
                "Review the agent output above."
            )

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s | %(name)s | %(message)s",
    )
    asyncio.run(main())
