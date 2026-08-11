# aiBrowser

Automated web browsing for bug bounty reconnaissance: crawls a target,
optionally drives an LLM-backed agent through JS-heavy pages, optionally
logs in or registers an account, and captures traffic — all through
Burp Suite by default, or standalone.

```bash
ai-browser crawl developers.tiktok.com --authorized
```

That's the minimum viable invocation. Everything else in this document
is about what you can layer on top of it.

---

## Options, up front

Every flag `ai-browser crawl` accepts, grouped by what they control. Full
authoritative list is always `ai-browser crawl --help` — this groups it
for readability and adds the context the raw help text doesn't have
room for.

### Authorization & scope (required + how far to follow links)

| Flag | Default | Notes |
|---|---|---|
| `--authorized` | — | **Required.** Confirms you have permission to test this hostname. The tool refuses to run without it. |
| `--scope TEXT` | seed hostname (exact match) | A glob pattern, e.g. `*.tiktok.com`. Controls which discovered links get followed. |
| `--scope-file FILE` | — | One glob pattern per line, `#` comments allowed. Combines with `--scope` (union, not override) if both given. |

### Network / traffic

| Flag | Default | Notes |
|---|---|---|
| `--proxy-server TEXT` | `http://127.0.0.1:8080` | Where to route traffic if not using `--no-proxy`. |
| `--no-proxy` | off | Route directly to the target instead of through Burp. |
| `--ca-cert PATH` | — | Burp's CA cert (DER/PEM), for trusting Burp's MITM'd HTTPS. Meaningless with `--no-proxy` — see below. |
| `--traffic-dir PATH` | `output/traffic/<hostname>/` | Where captured traffic (independent of Burp) gets written. |
| `--no-traffic-capture` | off | Disables aiBrowser's own traffic logging entirely. |

### Crawl behavior (Phase 1)

| Flag | Default | Notes |
|---|---|---|
| `--max-depth INT` | 3 | BFS depth limit. |
| `--max-pages INT` | 50 | Page count limit. |
| `--skip-existing FILE` | — | A previous run's output JSON. Already-seen URLs aren't re-crawled; their entries are merged into this run's output. |
| `--no-crawl` | off | **Skip Phase 1 entirely.** Use with `--register --signup-url` to go straight to registration, or `--login` to authenticate without discovering new pages first. Phase 2 (agent exploration) is auto-skipped when this is set. |
| `--output TEXT` | stdout | Where to write the final JSON. |
| `--storage-dir PATH` | `storage/browser_states` | Where session state, credentials, and PID files persist. |
| `--headless` / `--visible` | headless | Whether you can see the browser window. |

### Agent exploration (Phase 2)

| Flag | Default | Notes |
|---|---|---|
| `--agent` / `--no-agent` | agent (on) | Whether Phase 2 runs at all. |
| `--agent-backend [custom\|browser-use]` | `custom` | Which engine drives it. See [Two backends](#two-backends-for-phase-2) below — this choice has real, non-obvious consequences for other flags. |
| `--max-actions INT` | 20 | **Only affects `--agent-backend browser-use`.** See the callout below — this is the single most common way to be confused by this tool's current behavior. |
| `--agent-task TEXT` | generic "explore thoroughly" | **Only affects `--agent-backend browser-use`.** Override the agent's instructions. |

> **`--max-actions` silently does nothing on the default backend.**
> `_run_phase2_custom` never reads it — the custom explorer's action
> budget is a separate, hardcoded value (also 20, which is why this is
> easy to miss) inside `ExplorerConfig` that no CLI flag currently
> exposes. If you pass `--max-actions 100` without also passing
> `--agent-backend browser-use`, you will get exactly the same
> exploration depth as the default. See [Suggestions](#suggestions-for-simplifying-the-cli) below.

### LLM configuration (shared across Phase 2, Phase 3, and login's AI checks)

| Flag | Default | Notes |
|---|---|---|
| `--llm-provider [anthropic\|openai\|deepseek]` | `anthropic` | |
| `--llm-model TEXT` | provider-specific | Required if you set a provider explicitly and want anything other than the built-in default. |
| `--llm-api-key TEXT` | — | Or set `AIBROWSER_LLM_API_KEY`. |
| `--llm-base-url TEXT` | provider default | |
| `--llm-max-tokens INT` | provider default | |
| `--anthropic-api-key TEXT` | — | **Deprecated.** Falls back into `--llm-api-key` with a warning. Use the real flag. |

**These same credentials drive every AI-assisted decision in the tool**,
not just Phase 2: the registration post-submit judge, the login success
judge, and the confirmation-email classification fallback (see
[Email registration](#email-registration-in-detail)) all reuse
`--llm-provider`/`--llm-api-key`/etc. There's no separate credential set
for registration or login. If you're running `--register` or `--login`
without Phase 2 (`--no-agent`), you still want these flags set if you
want the AI-assisted parts of registration/login to function — without
them, those specific checks are skipped (some fail open, treated as
inconclusive; one fails closed, treated as "no" — see below) but nothing
crashes.

### Registration (Phase 3)

| Flag | Default | Notes |
|---|---|---|
| `--register` | off | Attempt registration after crawling. |
| `--register-email TEXT` | — | Required *unless* `--email-backend disposable` (address is provisioned dynamically in that mode). |
| `--register-password TEXT` | random, auto-generated | Saved to the credentials file either way. See below for the saved JSON fields. |
| `--register-name TEXT` | — | |
| `--signup-url TEXT` | — | **Directly specify the registration page URL**, bypassing automatic discovery from crawled endpoints. Essential when running with `--no-crawl` (since there are no endpoints to discover from), but also useful anytime you already know the signup URL and don't want automatic guessing to interfere. |
| `--login-verify-url TEXT` | `https://<hostname>/login` (guessed) | Override the login URL used for post-confirmation account verification. After registration and email confirmation, the tool attempts a real login with the same credentials to prove the account is active — this flag tells it where the login page actually lives, since `/login` is a guess that won't always be right. Env: `AIBROWSER_LOGIN_VERIFY_URL`. |

Credentials are saved to `storage/<storage_dir>/credentials/<hostname>.json` after
registration. The JSON includes: `hostname`, `email`, `password` (plaintext —
chmod 0600), `registered_at`, `confirmed` (whether a confirmation action was
attempted), and `login_verified` (`true` = login succeeded, `false` = login
failed, `null` = verification didn't run or was inconclusive).

### Login (Phase 0)

| Flag | Default | Notes |
|---|---|---|
| `--login` | off | Attempt login *before* crawling. |
| `--login-email TEXT` | falls back to `--register-email` | |
| `--login-password TEXT` | falls back to `--register-password` | |

### Email confirmation backend

| Flag | Default | Notes |
|---|---|---|
| `--email-backend [imap\|disposable]` | `imap` | See [Email registration](#email-registration-in-detail). |
| `--imap-host` / `--imap-port` / `--imap-username` / `--imap-password` | port 993 | A pre-existing mailbox you control. Password can also be `AIBROWSER_IMAP_PASSWORD`. |
| `--email-timeout INT` | 120 | Seconds to wait for the confirmation email. |
| `--disposable-inbox-api-key TEXT` | — | AgentMail API key. Required if `--email-backend disposable`. |
| `--disposable-inbox-domain TEXT` | provider default | Custom sending domain, if your AgentMail plan supports it. |

### Session / cookies

| Flag | Default | Notes |
|---|---|---|
| `--cookies-file FILE` | — | Import an existing session. Accepts Playwright `storage_state()` JSON, a bare cookie array, or a plain `name=value` per line text dump (e.g. copied from browser devtools). Skips automatic per-hostname session restore. |
| `--cookies-domain DOMAIN` | — | Domain to apply to cookies that don't specify their own (required for plain `name=value` text files, which carry no domain info). Defaults to `.<hostname>` if not set. |

---

## What can be used together, what can't

**Hard errors** (the tool refuses to start):

- `--register` with neither `--register-email` nor `--email-backend disposable` set.
- `--login` without `--login-email`/`--login-password` **and** without `--register-email`/`--register-password` to fall back to (both need to resolve to *something*).
- `--email-backend disposable` together with any of `--imap-host` / `--imap-username` / `--imap-password` — pick one backend, not both.
- `--email-backend disposable` without `--disposable-inbox-api-key`.
- `--agent` (default) without `--llm-model` when a provider was explicitly set but no model resolved — this is really "you configured half an LLM setup," not a real conflict.

**Not an error, but one silently wins:**

- `--cookies-file` + `--register` and/or `--login`: cookies win. Phase 0 and Phase 3 are both skipped outright, regardless of whether you also passed `--register`/`--login` — you'll see `"Skipped - using provided cookies..."` in the output rather than either phase running.
- `--login` succeeding also skips `--register` (checked via the login handler's own success verification, not just "the flag was passed") — but a **failed or inconclusive** login does *not* block registration; Phase 3 still runs normally in that case.
- `--max-actions` / `--agent-task` with `--agent-backend custom` (the default): both flags are accepted, neither does anything. No warning is printed. See the callout above.
- `--ca-cert` with `--no-proxy`: accepted, has no effect (there's no MITM cert to trust when there's no proxy in the loop).

**Precedence, summarized:** `--cookies-file` > successful `--login` > default (crawl normally, then optionally register).

---

## Two modes: routed through Burp, or standalone

By default, all traffic goes through Burp Suite (`http://127.0.0.1:8080`)
*and* is independently logged by aiBrowser itself — these are two
separate, parallel things that both happen unless you turn one off.

**Why both exist:** Burp gives you the interactive side — Repeater,
Intruder, the passive/active Scanner, extensions, just watching traffic
live while a crawl runs. aiBrowser's own capture (`output/traffic/<hostname>/`)
exists independently of Burp: it's Playwright's own `page.on("response")`
hook, writing an append-only `index.jsonl` (one record per request, with
headers, status, and content-addressed SHA-256 references into a
`bodies/` directory) that other tools in this pipeline (e.g. `aiSSRF`)
consume directly — it doesn't depend on Burp being up, and it dedupes
identical bodies automatically.

**`--no-proxy`** turns off *only* the Burp-routing half. Traffic still
goes to the target directly instead of through 127.0.0.1:8080, but
`output/traffic/` capture keeps working exactly the same either way. Use
this when Burp isn't running, or you specifically don't need live Burp
inspection for a given run — a downed Burp instance used to take out
every phase of a run identically before this flag existed.

**`--no-traffic-capture`** turns off the other half — no `index.jsonl`,
no `bodies/`. You'd want this if you're only routing through Burp for
its own capture/history and don't need aiBrowser's redundant copy.

Both can be off at once (`--no-proxy --no-traffic-capture`) for a
fast, log-free smoke test with no persistent traffic record at all.

---

## Running from different phases

Phase 1 (crawl) runs by default but can be skipped with `--no-crawl`.
Everything else is opt-in or conditionally skipped:

- **Skip Phase 1 entirely**: `--no-crawl`. Phase 2 is auto-skipped when this is set (no crawl seed to explore from). Phase 3 still runs if `--register` is passed — provide `--signup-url` to avoid falling back to the bare hostname root.
- **Skip crawling already-known URLs**: `--skip-existing <prior-run.json>`. Their entries get merged into this run's output rather than re-fetched.
- **Skip Phase 0 and Phase 3 entirely, start already-authenticated**: `--cookies-file <path>`. Three accepted shapes — Playwright's `storage_state()` JSON, a bare browser-extension-style cookie array, or plain `name=value` lines (auto-detected by whether the file parses as valid JSON first). For the plain-text format, use `--cookies-domain` to supply the domain (defaults to `.<hostname>`).
- **Skip Phase 3 only, log in fresh instead of registering**: `--login` with credentials. If login succeeds (verified, not just attempted — see below), registration is skipped automatically even if `--register` was also passed.
- **Skip Phase 2**: `--no-agent`. Crawl + optional login/register, no autonomous exploration.
- **Skip Phase 3 explicitly**: just don't pass `--register`. (Phase 0/`--login` is independent of this.)

### Two backends for Phase 2

`--agent-backend custom` (default) is aiBrowser's own explorer — stable,
but its action budget isn't currently exposed via any CLI flag (see the
callout above). `--agent-backend browser-use` connects the `browser-use`
library to the *same* Chromium process via CDP rather than launching a
second browser — requires the `[browser-use] extra` installed
(`pip install -e ".[browser-use]"`). This is the only backend
`--max-actions`/`--agent-task` actually affect.

**Before trusting `browser-use` against a real target**, run
`scripts/verify_browser_use_safety.py` — it independently verifies (not
via browser-use's own self-reported logs) that `allowed_domains`
enforcement holds and that HTTPS through an untrusted/MITM'd cert works
correctly for this CDP-sharing setup. Needs `DEEPSEEK_API_KEY` and
`NO_PROXY=127.0.0.1,localhost` set (Burp will otherwise intercept the
CDP handshake itself and break the check).

---

## Email registration, in detail

This is the most involved subsystem in the tool, and the one most worth
understanding before relying on it.

### Two backends, chosen with `--email-backend`

**`imap` (default)** — polls a real, pre-existing mailbox you control.
Needs `--imap-host`/`--imap-username`/`--imap-password` (or the env var
for the password). This is a **shared** inbox — if it's your everyday
mailbox, unrelated mail landing during the poll window is a real
consideration, which is exactly what the tiered matching below exists to
handle.

**`disposable`** — provisions a fresh, single-purpose AgentMail inbox
per registration attempt via API. No shared-inbox contention by
construction, since nothing else ever uses that address. Needs
`--disposable-inbox-api-key`. Mutually exclusive with any `--imap-*` flag.

### Finding the actual signup page

`signup_url` isn't guessed — `discover_signup_url()` scans every URL
Phase 1 discovered for path patterns that plausibly indicate a signup
page (`/signup`, `/register`, `/join`, etc.), ranking exact path-segment
matches above weaker substring matches, and explicitly excluding
documentation-flavored false positives (a `/doc/getting-started-create-an-app`
page is *about* registering an app, it isn't a signup form). If nothing
plausible turns up, it falls back to the bare hostname root. This is
purely deterministic — there's no AI involved in picking the URL itself.

When `--skip-existing <prior-run.json>` is combined with `--register`,
the URLs from the prior run are **also** added to the candidate list
(prior URLs first, then fresh URLs), so discovery benefits from the
full history rather than only the current crawl's endpoints. This is
especially useful when `--no-crawl` is also set — the prior-run data
becomes the *only* source of candidate endpoints, making
`--signup-url`-less registration with a skipped Phase 1 actually
functional rather than falling back to a bare hostname root every time.

### Filling the form, and *not* filling the wrong one

Fields are matched by common name/id/placeholder patterns
(`_form_helpers.fill_form_fields`). Critically: **if no email field is
found, the form is never submitted at all** — this guards against the
generic submit-button fallback clicking whatever button it can find on
the page (which, on a page with no real signup form but *some* form
present — a newsletter box being the textbook case — used to produce a
false "registration submitted" report).

### After submitting: an AI check that fails open

If `use_ai_judge` is on (default) and LLM credentials are configured, an
LLM looks at the post-submit page and judges whether it looks like a
real registration happened (`_ai_judge_did_submit`) — indicators include
"check your email," "account created," or **a request to enter a
verification code/PIN**, which was specifically added after this exact
judge misclassified a legitimate PIN-flow registration as "not a
registration" the first time it was tested for real. This judge **fails
open**: if the LLM call fails or returns nothing usable, the result is
`None` (inconclusive), not a confident negative — the run doesn't get
blocked on an LLM hiccup.

### Finding the confirmation email: three tiers

This is where most of the real-world debugging effort has gone, because
"which email in the inbox is actually the confirmation" turned out to
be less obvious than it sounds — TikTok's own confirmation mail comes
from `dev.tiktok.com`, not `developers.tiktok.com`, which is completely
standard practice for transactional mail and broke a naive exact-domain
check outright.

1. **Domain-preferred, deterministic.** Among unread messages that
   arrived after signup was submitted, ones whose sender shares the
   target's *registrable* domain (`tldextract`-based comparison —
   `dev.tiktok.com` and `developers.tiktok.com` both resolve to
   `tiktok.com`, so this matches; a completely unrelated domain doesn't)
   are checked first, newest first, for an extractable confirmation
   link.
2. **Everything else, still deterministic.** If nothing domain-matching
   panned out, the same content-extraction pass runs over the remaining
   unread messages — this covers legitimate mail sent via a third-party
   ESP (SendGrid, Mailgun, etc.) whose sending domain wouldn't
   domain-match at all, registrable or otherwise.
3. **AI classification of the single latest unread message, once, as a
   last resort.** Only triggers after the *entire* poll timeout has
   elapsed with nothing found — not once per poll interval, which would
   waste an LLM call every few seconds for no reason. The LLM is shown
   the latest unread message's sender/subject/body and asked whether it
   plausibly relates to this signup. **This one fails closed** — an LLM
   failure or empty response results in "no," not "inconclusive, proceed
   anyway." This is a deliberate, intentional exception to how every
   other AI check in this tool behaves: being wrong here means acting on
   a stranger's unrelated email, so the safe default under uncertainty
   is to not act, not to guess yes.

### Verification codes (PIN/OTP), not just links

The confirmation email may contain a verification code (PIN or OTP)
instead of a clickable link — e.g. "Your code is: 8R7H3W". Flow:

1. **Extraction**: The same Tiers 1–3 that look for links also attempt
   to extract a keyword-anchored code (`pin`, `code`, `otp`) between 4–8
   alphanumeric characters.

2. **Filling**: If a code is extracted, the tool looks for a code-entry
   field on the current page (matching `code`, `otp`, `pin`,
   `verification_code`, etc.) via the existing `fill_form_fields`
   helper, fills it, and submits the form.

3. **Failure modes logged distinctly** — the `reason=` field in the
   final warning distinguishes "code found but no input field on the
   page" (`reason=code_found_no_field`) from "no email received at all"
   (`reason=no_email_received`), "email found but no extractable content"
   (`reason=email_found_no_extractable_content`), and "AI judge rejected
   the email" (`reason=ai_judge_rejected`). Each represents a different
   real-world failure and the log makes it clear which one happened.

4. **Deterministic signal**: A code-entry field detected on the page is
   treated as strong evidence the registration flow is real — it
   overrides an earlier false-negative AI judge verdict ("NOT a
   registration") if the earlier verdict was wrong.

### CAPTCHA

Detected, never solved. The tool pauses and takes a screenshot rather
than attempting any bypass — this is a deliberate posture choice for a
security-testing tool, not a missing feature.

---

## Suggestions for simplifying the CLI

A few things that came up directly while writing this document, worth
considering as cleanup:

1. **Expose `--max-actions` for the custom backend too, or make its
   current inertness loud instead of silent.** Right now it's a trap:
   the flag is accepted regardless of `--agent-backend`, does nothing on
   the default, and nothing tells you that. Either wire it into
   `ExplorerConfig` for both backends, or have the CLI print a warning
   when `--max-actions`/`--agent-task` are set without
   `--agent-backend browser-use`.

2. **Env var coverage is inconsistent.** Most credential-shaped flags
   have an `AIBROWSER_*` env var fallback (`--llm-api-key`,
   `--imap-password`, `--disposable-inbox-api-key`), but
   `--register-password`, `--login-email`, and `--login-password` don't.
   Worth either adding the missing ones or documenting why those three
   are treated differently (there may be a reason — e.g. not wanting
   registration passwords in shell env by convention — but if so it's
   not stated anywhere currently).

3. **The registration/login validation errors are scattered `click.echo`
   + `return` pairs rather than a single upfront validation pass.**
   Functionally fine, but it means a run can get partway through Phase 0
   (spend time actually attempting a crawl) before discovering in Phase
   3 that the flag combination was invalid from the start. Validating
   all cross-flag constraints once, before Phase 1 starts, would fail
   faster and be easier to extend as more phases/flags are added.

4. **`--agent` / `--no-agent` and `--register` / no-`--register` use
   different conventions** — one's a boolean pair (`--agent`/`--no-agent`),
   the other's a bare flag with no explicit off-switch (`--register`
   present or absent). Not wrong, just inconsistent; picking one
   convention for all phase-toggle flags would make the mental model
   more predictable.

5. **Consider a single `--phases` selector** (e.g.
   `--phases crawl,login,agent` or similar) as an alternative/complement
   to the current per-phase boolean flags, especially now that there are
   real precedence rules between them (`--cookies-file` overriding both
   login and registration, successful login overriding registration).
   Right now understanding "what will actually run" requires reading
   this document's precedence table; an explicit phase list would make
   it visible directly in the command.
