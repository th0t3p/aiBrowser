# ai_browser

Automated web browsing for bug bounty reconnaissance. Drives a real browser
(Playwright/Chromium) through **Burp Suite's proxy**, so every request the
browser makes — links followed, forms submitted, accounts registered — lands
in Burp's proxy history automatically.

> **This tool captures traffic directly via Playwright.** Every in-scope
> request/response pair is saved to `output/traffic/<hostname>/index.jsonl`
> with content-addressed body files under `bodies/`. Burp Suite remains the
> proxy (all browser traffic still routes through it), but aiBrowser now
> records its own traffic for direct consumption by other tools (e.g. aiSSRF)
> without requiring aiScraper's Burp-ingestion endpoint.

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

4. **Traffic Capture** — hooks Playwright's `page.on("response")` to record
   every in-scope request/response pair as JSON Lines with content-addressed
   body files. Always on by default (opt-out with `--no-traffic-capture`),
   writes to `output/traffic/<hostname>/` for direct consumption by other
   tools (e.g. aiSSRF) with no Burp-ingestion dependency.

```
                    ┌──────────────┐
                    │  ai_browser  │
                    │ (Playwright) │
                    └──────┬───────┘
                           │ all traffic proxied
                           ▼
                   ┌───────────────┐
                   │  Burp Suite   │
                   │ 127.0.0.1:8080│
                   └───────────────┘
                           │
                           ▼
                     Target Host(s)

        Traffic also captured directly to
        output/traffic/<hostname>/index.jsonl
        (self-contained, no Burp dependency)
```

---

## Prerequisites

| Component | Needed for | Notes |
|---|---|---|
| **Python 3.11+** | Everything | Check with `python3 --version` |
| **Burp Suite** (Community or Pro) | Everything | Proxy listener running, target in scope |
| **Playwright** (Chromium) | Everything | Installed separately from the pip package — see below |
| **An LLM API key** (Anthropic, OpenAI, or DeepSeek) | `--agent` only | Skip this and pass `--no-agent` to run crawler-only |
| **`browser-use` + `langchain-deepseek`** | `--agent-backend browser-use` only | `pip install -e ".[browser-use]"` — optional extra, not installed by default |
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
often don't have this restriction. Pass it via `--imap-password` or,
safer, the `AIBROWSER_IMAP_PASSWORD` environment variable (avoids it
sitting in shell history).

### Using a .env file (recommended)

Instead of passing credentials on every command, copy the example file
and fill in your settings once:

```bash
cp .env.example .env
# Edit .env — add your API keys, IMAP credentials, etc.
```

Then invoke the CLI without repeated flags:

```bash
# Before (every flag on every command)
ai-browser crawl example.com --authorized --agent \
    --llm-provider deepseek --llm-model deepseek-chat \
    --llm-api-key "sk-..." --imap-password "$AIBROWSER_IMAP_PASSWORD"

# After (credentials from .env, only target-specific flags remain)
ai-browser crawl example.com --authorized --agent \
    --llm-provider deepseek --llm-model deepseek-chat
```

Explicit CLI flags always override `.env` values, so you can set
defaults in `.env` and override per-invocation when needed (e.g. a
different model for one target).

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

### Agent Explorer — choosing an LLM provider and backend

```bash
# With a .env file, no API key flag needed — it's read from AIBROWSER_LLM_API_KEY
ai-browser crawl example.com --authorized --agent \
    --llm-provider anthropic --llm-model claude-sonnet-4-20250514

# Without .env, set it per-command:
ai-browser crawl example.com --authorized --agent \
    --llm-provider anthropic --llm-model claude-sonnet-4-20250514 \
    --llm-api-key "sk-ant-..."

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

By default, `--agent-backend custom` uses aiBrowser's own AgentExplorer.
You can switch to **browser-use** (an external agent library with richer
action capabilities) by passing `--agent-backend browser-use`. This
requires the optional `[browser-use]` extra:

```bash
pip install -e ".[browser-use]"

# browser-use as the Phase 2 engine — connects to the SAME Chromium
# process via CDP (no second browser launch)
ai-browser crawl example.com --authorized --agent \
    --agent-backend browser-use \
    --max-actions 15 \
    --llm-provider deepseek --llm-model deepseek-v4-flash
```

`--agent-backend custom` (the default) is stable and tested. `browser-use`
is new and requires `browser-use==0.1.48` pinned. Control agent verbosity
with `--max-actions` (default 20) — lower is cheaper, higher explores
more of the site.

#### Verify browser-use safety before pointing it at a real target

`--agent-backend browser-use` connects browser-use to the **same**
Chromium process aiBrowser already launched, via CDP, rather than letting
it spawn its own browser. Before trusting that against a real,
adversarial-by-design target, run the standalone safety verification:

```bash
pip install -e ".[browser-use]"
export DEEPSEEK_API_KEY="sk-..."
export NO_PROXY=127.0.0.1,localhost   # avoid Burp intercepting the CDP handshake
export no_proxy=127.0.0.1,localhost   # some libs only check the lowercase form

python scripts/verify_browser_use_safety.py
```

This runs two independent checks and prints a PASS/FAIL verdict for each
(overall pass requires both):

1. **`allowed_domains` enforcement** — 5 repeated runs confirming a
   disallowed domain never gets contacted, verified via Playwright's own
   network observation (not browser-use's self-reported logs).
2. **HTTPS through an untrusted cert + context reuse** — confirms the
   browser accepts an untrusted/self-signed certificate (standing in for
   Burp's MITM cert) without blocking navigation, and that browser-use is
   genuinely reusing aiBrowser's pre-created browser context rather than
   spinning up its own (the assumption the CDP-sharing design depends on).

If either check fails, don't run `--agent-backend browser-use` against a
real target until you understand why — see "Key design decisions" below
for what each check is actually protecting against.

`--anthropic-api-key` still works as a deprecated alias for backward
compatibility, but new setups should use `--llm-provider` / `--llm-api-key`.

### Registration — letting the agent create an account autonomously

By default, if the agent encounters a signup form mid-crawl, it treats it
as a borderline action requiring confirmation — it will **not** register an
account on its own unless you explicitly opt in with `--register`:

```bash
# With a .env file (see "Using a .env file" above), this shrinks to:
ai-browser crawl example.com --authorized --agent --register \
    --register-password "Str0ngP@ss!"

# Without .env, pass every flag explicitly:
ai-browser crawl example.com --authorized --agent --register \
    --register-email "test+example@yourdomain.com" \
    --register-password "Str0ngP@ss!" \
    --imap-host imap.yourdomain.com \
    --imap-username "test@yourdomain.com" \
    --imap-password "$AIBROWSER_IMAP_PASSWORD"
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
# With a .env file (credentials come from .env):
ai-browser crawl example.com \
    --authorized \
    --scope "*.example.com" \
    --agent --register \
    --register-password "Str0ngP@ss!" \
    --max-depth 5 --max-pages 100 \
    --output results.json

# Without .env, pass credentials explicitly:
ai-browser crawl example.com \
    --authorized \
    --scope "*.example.com" \
    --agent --llm-provider anthropic --llm-api-key "$AIBROWSER_LLM_API_KEY" \
    --register \
    --register-email "test+example@yourdomain.com" \
    --register-password "Str0ngP@ss!" \
    --imap-host imap.yourdomain.com \
    --imap-username "test@yourdomain.com" \
    --imap-password "$AIBROWSER_IMAP_PASSWORD" \
    --max-depth 5 --max-pages 100 \
    --output results.json
```

### Resume a crawl — skipping already-discovered URLs

When you've already run a crawl against a target and want a second pass to
find **new** endpoints without re-crawling what you already found, pass the
previous run's output JSON with `--skip-existing`:

```bash
ai-browser crawl example.com --authorized \
    --skip-existing results.json \
    --output run2.json
```

The crawler seeds its visited set with every URL present in the prior
output, so those pages are never re-queued. The final output JSON merges
the prior entries (kept as-is) with any genuinely new endpoints discovered
in the resumed run, and includes `skipped_existing_count` /
`newly_discovered_count` fields to make the result self-documenting.

Prior entries win on URL collision — the already-verified data from the
earlier run is preserved unchanged.

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
├── traffic_capture/
│   ├── __init__.py              # TrafficCapture — page.on("response") hooks,
│   │                             #   content-addressed body storage, index.jsonl
│   └── schema.json               # JSON Schema v1.0 — source of truth for record format
└── cli.py                       # Click CLI entrypoint (crawl command)
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

### Traffic capture is self-contained, not Burp-dependent

aiBrowser records every in-scope request/response pair to a file-based
format under `output/traffic/<hostname>/`: one `index.jsonl` per run
with content-addressed body files under `bodies/<sha256>.bin`. This is
always on by default — opt out with `--no-traffic-capture` — and meant
for direct consumption by other tools (e.g. aiSSRF) without requiring
Burp Suite or aiScraper's ingestion endpoint. Burp remains the proxy;
the capture is an independent second record.

The JSON Schema at `ai_browser/traffic_capture/schema.json` is the
source of truth for the record format (schema version `"1.0"`). Other
tools should validate against it directly.

### The action denylist checks the real element, not just the LLM's claim

The LLM's self-reported action (target/value/reasoning) is checked against
the denylist first, but before actually clicking or submitting anything,
the resolved DOM element's real visible text is checked again. This
matters because page content — the very thing being security-tested —
can't be trusted to accurately describe itself back to the model.

### browser-use connects via CDP to the same browser, not a second one

`launch_persistent_context()` — used for the default `custom` backend —
internally adds Chromium's `--remote-debugging-pipe` flag, which
conflicts with the explicit `--remote-debugging-port` needed to expose a
CDP endpoint (a reproducible SIGBUS). The `browser-use` backend path
instead uses `launch()` + `new_context()`, and pre-creates that context
**before** handing off the CDP URL — so when browser-use connects, it
finds an existing context and reuses it rather than creating its own
(verified directly, not assumed — see `scripts/verify_browser_use_safety.py`).

HTTPS through Burp's MITM cert is handled by a Chromium-wide
`--ignore-certificate-errors` launch argument, not by browser-use's own
`disable_security` option. `disable_security=True` bypasses CSP and cert
validation entirely — browser-use's own source flags this as a
cookie-theft/malicious-iframe risk — and it's a no-op in this
architecture anyway, since it only takes effect in the branch of
browser-use's context setup that runs when a context is *not* already
being reused. Given the reuse branch is what's actually exercised here,
it's deliberately left unset rather than added "just in case."

### Orphaned Chromium processes are reaped, not left to accumulate

A crashed or killed run using the `browser-use` backend (the SIGBUS above
being the original example) can leave a Chromium process running with no
owner. Each such session records its browser's OS PID to
`storage/pids/<hostname>.pid` (fetched via CDP's
`SystemInfo.getProcessInfo`, since Playwright doesn't expose it
directly). The next run for that hostname checks for a stale PID file: if
the recorded process is still alive **and** independently confirmed to
still look like Chromium (never trusted blindly — a PID getting recycled
by the OS for something unrelated is a real if rare scenario), it's
terminated before a fresh browser launches. The file is cleared on every
clean shutdown.

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
├── pids/                   # Per-hostname CDP browser PID files (orphan reaping)
│   └── example.com.pid
├── audit_logs/              # Newline-delimited JSON audit log per crawl session
│   └── example.com_20260718_120000.jsonl
└── captcha_screenshots/     # Saved on every CAPTCHA pause
    └── captcha_https_example.com_signup_submit_20260718_120000.png

output/
└── traffic/
    └── <hostname>/          # Traffic capture (on by default)
        ├── index.jsonl       # One JSON line per captured request/response
        └── bodies/           # Content-addressed body files (<sha256>.bin)
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
