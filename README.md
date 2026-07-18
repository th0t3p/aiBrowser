# ai_browser

Automated web browsing module for bug bounty reconnaissance — all traffic proxied
through **Burp Suite** so that your existing `aiScraper` service can poll and
normalize captured traffic from Burp's proxy history.

> **This tool produces NO traffic logs itself.** Burp Suite is the source of truth.
> Run Burp with the target hostname in scope, and aiScraper will handle the rest.

## Architecture

```
                    ┌──────────────┐
                    │  ai_browser  │
                    │ (Playwright) │
                    └──────┬───────┘
                           │  proxy all traffic
                           ▼
                   ┌───────────────┐
                   │  Burp Suite   │◄── aiScraper polls proxy history
                   │ 127.0.0.1:8080│
                   └───────────────┘
                           │
                           ▼
                     Target Host
```

## Prerequisites

| Component | Role |
|-----------|------|
| **Burp Suite** (Community or Pro) | Running with proxy listener on `127.0.0.1:8080`, target hostname in scope |
| **Playwright** | Browser automation (Chromium) |
| **Claude API key** (optional) | Required only for agent_explorer (JS-heavy SPA pages) |
| **IMAP inbox** (optional) | Required only for registration_handler email confirmation |
| **Python 3.11+** | Runtime |

### Burp CA Certificate (for HTTPS targets)

1. In Burp: **Proxy → Options → Import/Export CA certificate**
2. Export as **DER format** (or PEM)
3. Pass it via `--ca-cert path/to/cacert.der` to `ai-browser crawl`

Alternatively, import the Burp CA into your OS trust store once and omit the flag.

## Installation

```bash
git clone <this-repo>
cd aiBrowser
pip install -e .
playwright install chromium
```

## Module Overview

```
ai_browser/
├── browser_session/        # Playwright wrapper, Burp proxy, scope guard
│   ├── session.py          #   BrowserSession (async context manager)
│   └── models.py           #   ProxyConfig, BrowserSessionConfig, ScopeGuardError
├── crawler/                # Deterministic BFS crawler (no LLM)
│   ├── crawler.py          #   robots.txt, sitemap.xml, JS endpoint regex
│   └── models.py           #   CrawlConfig, CrawlResult, DiscoveredEndpoint
├── agent_explorer/         # Claude-driven SPA exploration
│   ├── explorer.py         #   Accessibility tree → Claude → action, with denylist
│   └── models.py           #   ExplorerConfig, AgentAction, AuditLogEntry
├── registration_handler/   # Signup automation + IMAP + CAPTCHA detection
│   ├── handler.py          #   Form fill, email confirmation, captcha pause
│   └── models.py           #   RegistrationConfig, IMAPConfig, CaptchaDetected
└── cli.py                  # Click CLI entrypoint (crawl command)
```

## Usage

### Basic Crawl

```bash
# Requires the --authorized flag — refuses to run without it
ai-browser crawl example.com --authorized

# Fully visible browser (for debugging)
ai-browser crawl example.com --authorized --visible
```

### With Agent Explorer (SPA pages)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
ai-browser crawl example.com --authorized --agent
```

### With Registration Handler

```bash
ai-browser crawl example.com --authorized --register \
    --register-email "test+example@mydomain.com" \
    --register-password "Str0ngP@ss!" \
    --imap-host imap.mydomain.com \
    --imap-username "test@mydomain.com" \
    --imap-password "$IMAP_PASSWORD"
```

### Custom Burp Proxy

```bash
ai-browser crawl example.com --authorized --proxy-server http://127.0.0.1:9090
```

### Full Crawl Pipeline

```bash
ai-browser crawl example.com \
    --authorized \
    --agent \
    --anthropic-api-key "sk-ant-..." \
    --max-depth 5 \
    --max-pages 100 \
    --register \
    --register-email "test+example@mydomain.com" \
    --imap-host imap.mydomain.com \
    --imap-username "test@mydomain.com" \
    --ca-cert ~/burp-cacert.der \
    --output results.json
```

## Programmatic Usage

```python
from ai_browser.browser_session import BrowserSession, BrowserSessionConfig, ProxyConfig
from ai_browser.crawler import Crawler, CrawlConfig

config = BrowserSessionConfig(authorized_hostname="example.com")
async with BrowserSession(config) as session:
    crawl_config = CrawlConfig(
        start_url="https://example.com",
        authorized_hostname="example.com",
        max_depth=3,
        max_pages=50,
    )
    crawler = Crawler(crawl_config)
    result = await crawler.run(session)
    print(f"Found {len(result.endpoints)} endpoints")
```

## Key Design Decisions

### Scope Guard

Every navigation is intercepted at the Playwright route level. Any attempt
to reach a hostname other than `authorized_hostname` raises `ScopeGuardError`
and aborts the request. This prevents accidental navigation to third-party
services (CDNs, analytics, OAuth providers) from leaking into Burp history.

### No Traffic Logging

This module intentionally does **not** write traffic logs, HAR files, or
request/response bodies to disk. Burp Suite captures everything at the proxy
level. The accompanying `aiScraper` service polls Burp's REST API to normalize
and store traffic data. This avoids duplication and keeps the browser side
stateless.

### Storage State Persistence

Cookie and localStorage state is saved/restored per hostname to
`storage/browser_states/<hostname>.json`. This allows session reuse across
invocations — useful for authenticated crawling.

### CAPTCHA Handling

CAPTCHAs are detected, screenshotted, and the flow is paused. The caller
receives a `CaptchaDetected` exception with the screenshot path and should
solve it manually in a visible browser window, then call `handler.resume()`.
No automated CAPTCHA solving is attempted.

### Action Denylist

The agent_explorer uses a configurable denylist to prevent autonomous
clicks/submits on destructive actions. Patterns include "delete account",
"cancel subscription", "confirm purchase", "pay now", and others.
Borderline actions can trigger a human confirmation callback.

## Storage

```
storage/
├── browser_states/          # Per-hostname storage_state JSON files
│   └── example.com.json
├── audit_logs/              # Newline-delimited JSON audit logs per session
│   └── example.com_20260718_120000.jsonl
└── captcha_screenshots/     # Saved CAPTCHA screenshots
    └── captcha_https_example.com_signup_submit_20260718_120000.png
```
