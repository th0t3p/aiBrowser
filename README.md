# ai_browser

Automated web browsing for bug bounty reconnaissance. Drives a real browser
(Playwright/Chromium) through **Burp Suite's proxy**, so every request the
browser makes — links followed, forms submitted, accounts registered — lands
in Burp's proxy history automatically.

> **This tool produces no traffic logs of its own.** Burp Suite is the
> source of truth for captured traffic. Point `aiScraper` at Burp's MCP
> server separately to poll, normalize, and store what gets captured here.

---

## What it does

Three layers, each usable independently:

1. **Crawler** — deterministic, no LLM. Follows links, reads `robots.txt`
   and `sitemap.xml`, regex-extracts likely API endpoints from inline JS.
2. **Agent Explorer** — LLM-driven. For JS-heavy pages where the crawler
   finds no new links, an LLM reads the page's accessibility tree and
   decides what to click or fill next. Supports **Anthropic, OpenAI, and
   DeepSeek**.
3. **Registration + Login handlers** — fills signup/login forms, polls an
   IMAP inbox for confirmation emails, detects (but never solves) CAPTCHAs,
   and persists the resulting session so later runs skip straight to an
   authenticated crawl.

```
                    ┌──────────────┐
                    │  ai_browser  │
                    │ (Playwright) │
                    └──────┬───────┘
                           │ all traffic proxied
                           ▼
                   ┌───────────────┐
                   │  Burp Suite   │◄── aiScraper polls this separately
                   │ 127.0.0.1:8080│
                   └───────────────┘
                           │
                           ▼
                     Target Host(s)
```

---

## Prerequisites

| Component | Needed for | Notes |
|---|---|---|
| **Python 3.11+** | Everything | Check with `python3 --version` |
| **Burp Suite** (Community or Pro) | Everything | Proxy listener running, target in scope |
| **Playwright** (Chromium) | Everything | Installed separately from the pip package — see below |
| **An LLM API key** (Anthropic, OpenAI, or DeepSeek) | `--agent` only | Skip this and pass `--no-agent` to run crawler-only |
| **An IMAP-accessible inbox** | `--register` / `--login` with email confirmation | Needs an app-specific password on most providers — see below |

### Installing

```bash
git clone https://github.com/th0t3p/aiBrowser.git
cd aiBrowser
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip     # older pip can't do editable installs from pyproject.toml alone
pip install -e .
playwright install chromium              # separate step — pip install alone does NOT fetch the browser binary
```

### Confirming Burp is actually listening

```bash
curl -x http://127.0.0.1:8080 http://burpsuite -v
```

If Burp's proxy is up, this returns Burp's own CA-certificate download page.
If you get "connection refused," check Burp's **Proxy → Proxy Listeners**
tab for the actual bound port before continuing.

### HTTPS / Burp's CA certificate

By default, `ignore_https_errors` is enabled, so Playwright accepts Burp's
self-signed MITM cert with no extra setup — nothing to configure for a
first run. If you need stricter TLS validation for realism (catching
cert-related bugs on the target), export Burp's CA cert (**Proxy → Options
→ Import/Export CA certificate**) and pass it via `--ca-cert path/to/cert`.

### IMAP app passwords

Gmail, Outlook, and most major providers **block IMAP login with your
regular account password**. You'll need a provider-generated app-specific
password instead (e.g. Gmail: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
requires 2-Step Verification enabled first). Custom-domain mail servers
often don't have this restriction. Pass it via `--imap-password` or, safer,
the `IMAP_PASSWORD` environment variable (avoids it sitting in shell
history).

---

## Quick start

```bash
# Smoke test: crawler only, no LLM, no registration — fastest way to
# confirm Playwright + Burp are wired up correctly
ai-browser crawl example.com --authorized --no-agent --headless
```

Every run requires `--authorized` — there's no default; the CLI refuses to
start without it, as an explicit acknowledgment that you have testing
permission for the target.

---

## Usage

### Wildcard scope

`--scope` accepts a glob pattern for which discovered hostnames are
in-scope to follow, separate from the hostname you actually start at:

```bash
ai-browser crawl developers.example.com --authorized --scope "*.example.com"
```

The positional `hostname` argument stays a concrete, resolvable host (used
to build the seed URL, `robots.txt`, and `sitemap.xml` requests) — it can't
itself be a wildcard. `--scope` governs which *discovered links* the
crawler and agent are allowed to follow once exploring. If `--scope` is
omitted, scope defaults to an exact match on `hostname`.

### Agent Explorer — choosing an LLM provider

```bash
# Anthropic (default)
export LLM_API_KEY="sk-ant-..."
ai-browser crawl example.com --authorized --agent \
    --llm-provider anthropic --llm-model claude-sonnet-4-20250514

# OpenAI
ai-browser crawl example.com --authorized --agent \
    --llm-provider openai --llm-model gpt-4o --llm-api-key "sk-..."

# DeepSeek
ai-browser crawl example.com --authorized --agent \
    --llm-provider deepseek --llm-model deepseek-chat --llm-api-key "..."

# Self-hosted / custom endpoint (OpenAI-compatible)
ai-browser crawl example.com --authorized --agent \
    --llm-provider openai --llm-base-url "http://localhost:8000/v1" --llm-api-key "unused"
```

`--anthropic-api-key` still works as a deprecated alias for backward
compatibility, but new setups should use `--llm-provider` / `--llm-api-key`.

### Registration — letting the agent create an account autonomously

By default, if the agent encounters a signup form mid-crawl, it treats it
as a borderline action requiring confirmation — it will **not** register an
account on its own unless you explicitly opt in with `--register`:

```bash
ai-browser crawl example.com --authorized --agent --register \
    --register-email "test+example@yourdomain.com" \
    --register-password "Str0ngP@ss!" \
    --imap-host imap.yourdomain.com \
    --imap-username "test@yourdomain.com" \
    --imap-password "$IMAP_PASSWORD"
```

With `--register` set, the agent recognizes signup-intent elements (sign
up, create account, register, get started, join now) during exploration
and hands off to the registration handler for the actual fill + submit +
email-confirmation flow — rather than the agent improvising form values
itself.

If a CAPTCHA is hit during registration, the flow **pauses** and raises
`CaptchaDetected` with a saved screenshot — nothing is solved
automatically. Solve it manually in a visible browser window
(`--visible`), then call `.resume()` to continue.

### Login — reusing a session on later runs

```bash
ai-browser crawl example.com --authorized --login \
    --login-email "test+example@yourdomain.com" \
    --login-password "Str0ngP@ss!"
```

If you've already registered on a target in a previous run, you often
don't need `--login-email`/`--login-password` at all — cookies and
localStorage are persisted per hostname in `storage/browser_states/`, so a
later run picks up the still-authenticated session automatically. The
explicit login flags are there for logging in with credentials that
weren't created by this tool.

### Full pipeline example

```bash
ai-browser crawl example.com \
    --authorized \
    --scope "*.example.com" \
    --agent --llm-provider anthropic --llm-api-key "$LLM_API_KEY" \
    --register \
    --register-email "test+example@yourdomain.com" \
    --imap-host imap.yourdomain.com \
    --imap-username "test@yourdomain.com" \
    --imap-password "$IMAP_PASSWORD" \
    --max-depth 5 --max-pages 100 \
    --output results.json
```

---

## Module layout

```
ai_browser/
├── _scope.py                 # Shared glob-pattern hostname matching (used by
│                              #   browser_session, agent_explorer, crawler)
├── _form_helpers.py           # Shared form-filling + CAPTCHA detection,
│                              #   used by both registration_handler and login_handler
├── browser_session/
│   ├── session.py             # BrowserSession — Playwright wrapper, Burp proxy,
│   │                          #   scope guard, storage_state persistence
│   └── models.py               # BrowserSessionConfig, ProxyConfig, ScopeGuardError
├── crawler/
│   ├── crawler.py             # Deterministic BFS crawler (no LLM)
│   └── models.py               # CrawlConfig (seed_hostname vs scope_pattern), CrawlResult
├── agent_explorer/
│   ├── explorer.py             # Accessibility tree → LLM → action, with denylist,
│   │                          #   registration hand-off, multi-provider LLM calls
│   └── models.py               # ExplorerConfig, AgentAction, AuditLogEntry
├── registration_handler/
│   ├── handler.py             # Signup form fill, IMAP polling, CAPTCHA pause
│   └── models.py               # RegistrationConfig, IMAPConfig, CaptchaDetected
├── login_handler/
│   ├── handler.py             # Login form fill, session persistence
│   └── models.py               # LoginConfig
└── cli.py                      # Click CLI entrypoint (crawl command)
```

---

## Key design decisions

### Scope guard is enforced twice, independently

`BrowserSession` intercepts every request at the Playwright route level —
anything outside the configured scope pattern gets aborted before it
leaves the browser. `AgentExplorer` performs its **own** independent
hostname check before executing any action, rather than trusting
`BrowserSession` alone — the same defense-in-depth principle used across
the rest of this toolchain (`aiSSRF`'s candidate fetcher does the same
against `aiScraper`'s output). Scope violations are recorded on
`session.violations`; call `session.check_violations()` to raise if any
occurred.

### No traffic logging, by design

Burp captures everything at the proxy level; `aiScraper` polls Burp
separately to normalize and store it. This module stays stateless on the
traffic side — the only things it persists are cookies/localStorage
(for session reuse) and audit logs of its own actions (not the HTTP
traffic itself).

### The action denylist checks the real element, not just the LLM's claim

The LLM's self-reported action (target/value/reasoning) is checked against
the denylist first, but before actually clicking or submitting anything,
the resolved DOM element's real visible text is checked again. This
matters because page content — the very thing being security-tested —
can't be trusted to accurately describe itself back to the model.

### Registration is opt-in, not incidental

Signup-related actions are recognized as their own category, separate from
the destructive-action denylist. Without `--register`, they require human
confirmation and are never auto-approved. With `--register`, the agent
recognizes them and delegates the actual form-filling to
`RegistrationHandler` rather than improvising values itself.

### CAPTCHA handling

Detected, screenshotted, and paused — never solved automatically. The
caller receives a `CaptchaDetected` exception with the screenshot path and
resumes manually after solving it.

---

## Storage layout

```
storage/
├── browser_states/         # Per-hostname cookies/localStorage (session reuse)
│   └── example.com.json
├── audit_logs/              # Newline-delimited JSON audit log per crawl session
│   └── example.com_20260718_120000.jsonl
└── captcha_screenshots/     # Saved on every CAPTCHA pause
    └── captcha_https_example.com_signup_submit_20260718_120000.png
```

---

## Programmatic usage

```python
from ai_browser.browser_session import BrowserSession, BrowserSessionConfig
from ai_browser.crawler import Crawler, CrawlConfig

config = BrowserSessionConfig(authorized_hostname="*.example.com")
async with BrowserSession(config) as session:
    crawl_config = CrawlConfig(
        start_url="https://example.com",
        seed_hostname="example.com",
        scope_pattern="*.example.com",
        max_depth=3,
        max_pages=50,
    )
    result = await Crawler(crawl_config).run(session)
    print(f"Found {len(result.endpoints)} endpoints")
    session.check_violations()  # raises if any scope guard blocked a navigation
```
