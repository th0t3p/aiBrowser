"""CLI entry point for ai_browser."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

import click

from ai_browser.browser_session import BrowserSession, BrowserSessionConfig, ProxyConfig
from ai_browser.crawler import Crawler, CrawlConfig, CrawlResult, DiscoveryMethod
from ai_browser.agent_explorer import AgentExplorer, ExplorerConfig
from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig, IMAPConfig
from ai_browser.login_handler import LoginHandler, LoginConfig
from ai_browser.traffic_capture import TrafficCapture
from ai_browser._scope import as_scope_list

from dotenv import load_dotenv

load_dotenv()  # loads .env from CWD into os.environ before Click parses options

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("ai_browser")

# ASCII banner reminder
AUTHORIZATION_REMINDER = """
╔══════════════════════════════════════════════════════════════╗
║  AI BROWSER — Automated Web Browsing for Bug Bounty         ║
║                                                              ║
║  ⚠ WARNING: This tool performs live browsing against a      ║
║  target hostname. Only use against hosts you have explicit   ║
║  written authorization to test. Unauthorized testing is      ║
║  illegal and could result in criminal/civil penalties.       ║
║                                                              ║
║  By default, traffic is proxied through Burp Suite            ║
║  (127.0.0.1:8080) — use --no-proxy to route directly.         ║
║  Traffic is captured to output/traffic/<hostname>/ by default  ║
║  independently for other tools (e.g. aiSSRF).                 ║
║                                                              ║
║  --scope accepts glob patterns (e.g. '*.tiktok.com') to      ║
║  follow links across subdomains. Defaults to exact-match     ║
║  on the seed hostname.                                       ║
╚══════════════════════════════════════════════════════════════╝
"""


@click.group()
@click.pass_context
def main(ctx: click.Context):
    """ai_browser: Automated web browsing for bug bounty reconnaissance.

    By default, traffic is routed through Burp Suite proxy (127.0.0.1:8080)
    via --proxy-server. Use --no-proxy to route directly to targets.
    aiScraper polls Burp's proxy history to capture and normalize traffic.
    """
    ctx.ensure_object(dict)


@main.command()
@click.argument("hostname", type=str)
@click.option(
    "--authorized",
    is_flag=True,
    default=False,
    help="Confirm you have authorization to test this hostname. REQUIRED.",
)
@click.option(
    "--scope",
    default=None,
    help="Glob pattern for in-scope hostnames (e.g. '*.tiktok.com'). "
    "Defaults to the seed hostname (exact match only) if not provided.",
)
@click.option(
    "--scope-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to a file with one glob scope pattern per line "
    "(e.g. '*.tiktok.com', 'careers.tiktok.com'). Lines starting with "
    "'#' and blank lines are ignored. Combines with --scope if both are "
    "given (union of patterns, not override). A hostname is in-scope if "
    "it matches ANY pattern from the combined set.",
)
@click.option(
    "--proxy-server",
    default="http://127.0.0.1:8080",
    show_default=True,
    envvar="AIBROWSER_PROXY_SERVER",
    help="Burp Suite proxy address.",
)
@click.option(
    "--no-proxy",
    is_flag=True,
    default=False,
    envvar="AIBROWSER_NO_PROXY",
    help="Route traffic directly to the target instead of through Burp. "
    "Use this when Burp isn't running or you don't need live Burp "
    "inspection for this run. aiBrowser's own traffic capture is "
    "unaffected either way.",
)
@click.option(
    "--max-depth",
    default=3,
    show_default=True,
    help="Maximum BFS crawl depth.",
)
@click.option(
    "--max-pages",
    default=50,
    show_default=True,
    help="Maximum number of pages to crawl.",
)
@click.option(
    "--agent/--no-agent",
    default=True,
    show_default=True,
    help="Run the agent explorer on JS-heavy pages with no links.",
)
@click.option(
    "--agent-backend",
    type=click.Choice(["custom", "browser-use"]),
    default="custom",
    show_default=True,
    envvar="AIBROWSER_AGENT_BACKEND",
    help="Which engine drives Phase 2 autonomous exploration. "
    "'custom' is aiBrowser's own explorer (default, stable). "
    "'browser-use' uses the browser-use library, connected "
    "to the same browser via CDP — requires the "
    "[browser-use] extra to be installed.",
)
@click.option(
    "--max-actions",
    default=20,
    show_default=True,
    help="Maximum autonomous actions (clicks, scrolls, etc.) for "
    "the Phase 2 agent explorer.",
)
@click.option(
    "--agent-task",
    default=None,
    envvar="AIBROWSER_AGENT_TASK",
    help="Override the Phase 2 agent's task/instructions. Only used by "
    "--agent-backend browser-use (the 'custom' backend is driven by a "
    "per-step decision loop, not a single task string, so this has no "
    "effect there). If unset, falls back to a generic "
    "'explore thoroughly' task. Useful for pointing the agent at "
    "something specific, e.g. "
    "'Click through the left sidebar navigation on every documentation "
    "page and report all linked pages' — a directive task tends to "
    "waste less of --max-actions on re-exploring already-crawled pages "
    "than the generic default.",
)
@click.option(
    "--llm-provider",
    default="anthropic",
    type=click.Choice(["anthropic", "openai", "deepseek"]),
    show_default=True,
    envvar="AIBROWSER_LLM_PROVIDER",
    help="LLM provider for agent_explorer.",
)
@click.option(
    "--llm-model",
    default=None,
    envvar="AIBROWSER_LLM_MODEL",
    help="Model name (provider-specific). Defaults: claude-sonnet-4-20250514 / gpt-4o / deepseek-chat.",
)
@click.option(
    "--llm-api-key",
    default=None,
    envvar="AIBROWSER_LLM_API_KEY",
    help="API key for the LLM provider (or set AIBROWSER_LLM_API_KEY env var).",
)
@click.option(
    "--llm-base-url",
    default=None,
    envvar="AIBROWSER_LLM_BASE_URL",
    help="Custom base URL for the LLM API (falls back to provider default).",
)
@click.option(
    "--llm-max-tokens",
    type=int,
    default=None,
    envvar="AIBROWSER_LLM_MAX_TOKENS",
    help="Max tokens for LLM API calls. If unset, Anthropic still "
    "requires a value (falls back to 4096 internally, since "
    "its API mandates this field); OpenAI/DeepSeek omit the "
    "field entirely and use the provider's own default.",
)
@click.option(
    "--anthropic-api-key",
    default=None,
    envvar="AIBROWSER_ANTHROPIC_API_KEY",
    help="[Deprecated] Use --llm-provider anthropic --llm-api-key instead.",
)
@click.option(
    "--register",
    is_flag=True,
    default=False,
    help="After crawling, attempt registration via registration_handler.",
)
@click.option(
    "--register-email",
    default=None,
    envvar="AIBROWSER_REGISTER_EMAIL",
    help="Email to use for registration (e.g. test+target@mydomain.com).",
)
@click.option(
    "--register-password",
    default=None,
    help="Password for registration. If not provided, a random "
    "password is generated automatically and saved to the "
    "credentials file after successful registration.",
)
@click.option(
    "--register-name",
    default="Test User",
    help="Full name for registration.",
)
@click.option(
    "--signup-url",
    default=None,
    help="Directly specify the signup/registration page URL, "
    "bypassing automatic discovery from crawled endpoints. "
    "Useful when you already know the URL, or when running with --no-crawl.",
)
@click.option(
    "--login-verify-url",
    default=None,
    envvar="AIBROWSER_LOGIN_VERIFY_URL",
    help="Override the login URL used for post-confirmation account "
    "verification. Defaults to https://<hostname>/login when unset "
    "(--register attempts a real login after confirmation to prove "
    "the account is active). Use this when the target's login page "
    "lives somewhere other than /login.",
)
@click.option(
    "--login",
    is_flag=True,
    default=False,
    help="Log in before crawling using persisted or provided credentials.",
)
@click.option(
    "--login-email",
    default=None,
    help="Email/username for login (falls back to --register-email).",
)
@click.option(
    "--login-password",
    default=None,
    help="Password for login (falls back to --register-password).",
)
@click.option(
    "--imap-host",
    default=None,
    envvar="AIBROWSER_IMAP_HOST",
    help="IMAP server hostname for email confirmation polling.",
)
@click.option(
    "--imap-port",
    default=993,
    show_default=True,
    envvar="AIBROWSER_IMAP_PORT",
    help="IMAP server port.",
)
@click.option(
    "--imap-username",
    default=None,
    envvar="AIBROWSER_IMAP_USERNAME",
    help="IMAP login username (full email address).",
)
@click.option(
    "--imap-password",
    default=None,
    envvar="AIBROWSER_IMAP_PASSWORD",
    help="IMAP login password (or set AIBROWSER_IMAP_PASSWORD env var).",
)
@click.option(
    "--email-timeout",
    default=120,
    show_default=True,
    help="How long (seconds) to poll inbox for confirmation email.",
)
@click.option(
    "--output",
    default=None,
    help="Path to write JSON crawl results. Prints to stdout if not set.",
)
@click.option(
    "--no-crawl",
    is_flag=True,
    default=False,
    help="Skip Phase 1 (deterministic crawl) entirely. Registration "
    "(--register) will use --signup-url if provided, otherwise fall "
    "back to guessing the bare hostname root. Phase 2 (agent "
    "exploration) is skipped automatically when this is set, since "
    "it has no crawl seed to build on.",
)
@click.option(
    "--skip-existing",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to a previous run's output JSON. URLs already "
    "present there will not be re-crawled; their entries "
    "are merged into this run's output.",
)
@click.option(
    "--headless/--visible",
    default=True,
    help="Run browser headless (default) or visible.",
)
@click.option(
    "--ca-cert",
    default=None,
    type=click.Path(exists=True),
    envvar="AIBROWSER_CA_CERT",
    help="Path to exported Burp CA certificate (DER/PEM) for HTTPS trust.",
)
@click.option(
    "--storage-dir",
    default="storage/browser_states",
    show_default=True,
    type=click.Path(),
    envvar="AIBROWSER_STORAGE_DIR",
    help="Directory for browser state persistence.",
)
@click.option(
    "--traffic-dir",
    default=None,
    type=click.Path(),
    envvar="AIBROWSER_TRAFFIC_DIR",
    help="Directory for traffic capture output. "
    "Defaults to output/traffic/<hostname>/.",
)
@click.option(
    "--no-traffic-capture",
    is_flag=True,
    default=False,
    help="Disable traffic capture entirely.",
)
@click.option(
    "--cookies-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to an exported cookie/session file (Playwright storage_state "
    "JSON or bare cookie array). When set, skips automatic session restore "
    "and login/registration phases.",
)
@click.option(
    "--email-backend",
    type=click.Choice(["imap", "disposable"]),
    default="imap",
    show_default=True,
    help="Email confirmation backend. 'imap' uses a pre-configured IMAP "
    "inbox (default). 'disposable' provisions a fresh AgentMail inbox "
    "per run.",
)
@click.option(
    "--disposable-inbox-api-key",
    default=None,
    envvar="AIBROWSER_DISPOSABLE_INBOX_API_KEY",
    help="API key for the disposable inbox provider (AgentMail).",
)
@click.option(
    "--disposable-inbox-domain",
    default=None,
    help="Custom domain for disposable inbox, if supported by the provider.",
)
@click.pass_context
def crawl(
    ctx: click.Context,
    hostname: str,
    authorized: bool,
    scope: Optional[str],
    scope_file: Optional[str],
    proxy_server: str,
    no_proxy: bool,
    max_depth: int,
    max_pages: int,
    agent: bool,
    agent_backend: str,
    max_actions: int,
    agent_task: Optional[str],
    llm_provider: str,
    llm_model: Optional[str],
    llm_api_key: Optional[str],
    llm_base_url: Optional[str],
    llm_max_tokens: Optional[int],
    anthropic_api_key: Optional[str],
    register: bool,
    register_email: Optional[str],
    register_password: Optional[str],
    register_name: str,
    signup_url: Optional[str],
    login_verify_url: Optional[str],
    login: bool,
    login_email: Optional[str],
    login_password: Optional[str],
    imap_host: Optional[str],
    imap_port: int,
    imap_username: Optional[str],
    imap_password: Optional[str],
    email_timeout: int,
    output: Optional[str],
    no_crawl: bool,
    skip_existing: Optional[str],
    headless: bool,
    ca_cert: Optional[str],
    storage_dir: str,
    traffic_dir: Optional[str],
    no_traffic_capture: bool,
    email_backend: str,
    disposable_inbox_api_key: Optional[str],
    disposable_inbox_domain: Optional[str],
    cookies_file: Optional[str],
):
    """Crawl HOSTNAME, discovering URLs and endpoints.

    Traffic is routed through Burp proxy by default — use --no-proxy
    to route directly. HOSTNAME is the target hostname to crawl
    (e.g. example.com).  The --authorized flag MUST be provided.
    """
    if not authorized:
        click.echo(AUTHORIZATION_REMINDER, err=True)
        click.echo(
            "ERROR: --authorized flag is required. This confirms you have "
            "permission to test this hostname.",
            err=True,
        )
        sys.exit(1)

    click.echo(AUTHORIZATION_REMINDER)

    # Deprecation: --anthropic-api-key maps to new fields
    _llm_api_key = llm_api_key or anthropic_api_key
    if anthropic_api_key and not llm_api_key:
        click.echo(
            "⚠ Warning: --anthropic-api-key is deprecated. Use --llm-provider anthropic --llm-api-key instead.",
            err=True,
        )

    # Auto-generate a fresh registration password when none is provided.
    # A static default would silently reuse the same weak password for
    # every registered account — fresh random is safer and the password
    # is always saved to the credentials file after registration.
    if register_password is None:
        register_password = secrets.token_urlsafe(16)

    # --signup-url without --register is a harmless no-op — log it so the
    # user isn't confused about why it had no visible effect.
    if signup_url and not register:
        logger.debug("--signup-url was set but --register is not — the "
                      "signup URL will not be used this run.")

    # --no-crawl without --register and without --signup-url: nothing to
    # crawl, nothing to register, Phase 2 has no seed. Print a clarifying
    # message rather than silently doing nothing.
    if no_crawl and not register and not signup_url and not login:
        click.echo(
            "Phase 1 skipped (--no-crawl). Nothing else was requested — "
            "no crawler, no agent (auto-skipped), no registration, no login. "
            "Pass --register or --login for the other phases to run.",
        )

    start_url = f"https://{hostname}"
    scope_pattern: Union[str, List[str]]

    # Build the combined scope pattern list
    if scope_file:
        _raw = Path(scope_file).read_text()
        _file_patterns = [
            line.strip() for line in _raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not _file_patterns:
            click.echo(
                f"ERROR: --scope-file {scope_file} contains no usable patterns "
                f"(all lines are blank or comments).",
                err=True,
            )
            sys.exit(1)
        if scope:
            _file_patterns.append(scope)
        scope_pattern = _file_patterns
    elif scope:
        scope_pattern = scope
    else:
        scope_pattern = hostname

    # If --scope is provided, warn if the seed hostname doesn't match
    if scope and hostname != scope:
        from ai_browser._scope import hostname_matches_any_scope
        if not hostname_matches_any_scope(hostname, scope_pattern):
            click.echo(
                f"⚠ Warning: seed hostname '{hostname}' does not match "
                f"any scope pattern. The crawl will start outside its "
                f"own declared scope.",
                err=True,
            )

    # Print proxy routing status — always visible so anyone reading
    # a run's output can tell at a glance which mode was used.
    if no_proxy:
        click.echo(
            "Running WITHOUT a proxy — traffic goes direct to the target, "
            "not through Burp. aiBrowser's own traffic capture is unaffected."
        )
    else:
        click.echo(f"Routing through proxy: {proxy_server}")

    # Build browser session config
    session_config = BrowserSessionConfig(
        authorized_hostname=scope_pattern,
        storage_key=hostname,
        proxy=None if no_proxy else ProxyConfig(server=proxy_server),
        headless=headless,
        storage_dir=Path(storage_dir),
        ca_cert_path=Path(ca_cert) if ca_cert else None,
        expose_cdp=(agent_backend == "browser-use"),
        cookies_file=Path(cookies_file) if cookies_file else None,
    )

    # Build crawl config
    crawl_config = CrawlConfig(
        start_url=start_url,
        seed_hostname=hostname,
        scope_pattern=scope_pattern,
        max_depth=max_depth,
        max_pages=max_pages,
    )

    # Prior-run support: seed visited set so previously-seen URLs are skipped
    prior_endpoints: list = []
    seed_visited: Optional[set[str]] = None
    if skip_existing:
        try:
            prior_data = json.loads(Path(skip_existing).read_text())
        except json.JSONDecodeError as exc:
            click.echo(
                f"ERROR: --skip-existing file {skip_existing} is not valid JSON: {exc}",
                err=True,
            )
            sys.exit(1)
        prior_endpoints = prior_data.get("endpoints", [])
        seed_visited = {
            Crawler._normalize(ep["url"]) for ep in prior_endpoints
        }
        click.echo(
            f"Loaded {len(prior_endpoints)} previously-discovered "
            f"endpoints from {skip_existing} — these will be skipped."
        )

    # Run
    asyncio.run(
        _run_crawl(
            session_config=session_config,
            crawl_config=crawl_config,
            seed_visited=seed_visited,
            prior_endpoints=prior_endpoints,
            run_agent=agent,
            agent_backend=agent_backend,
            max_actions=max_actions,
            agent_task=agent_task,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_api_key=_llm_api_key,
            llm_base_url=llm_base_url,
            llm_max_tokens=llm_max_tokens,
            do_register=register,
            register_email=register_email,
            register_password=register_password,
            register_name=register_name,
            signup_url=signup_url,
            login_verify_url=login_verify_url,
            do_login=login,
            login_email=login_email,
            login_password=login_password,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_username=imap_username,
            imap_password=imap_password,
            email_timeout=email_timeout,
            output_file=output,
            no_crawl=no_crawl,
            hostname=hostname,
            scope_pattern=scope_pattern,
            traffic_dir=traffic_dir,
            no_traffic_capture=no_traffic_capture,
            email_backend=email_backend,
            disposable_inbox_api_key=disposable_inbox_api_key,
            disposable_inbox_domain=disposable_inbox_domain,
            cookies_file=cookies_file,
        )
    )


def _save_credentials(
    storage_dir: Path,
    hostname: str,
    email: str,
    password: str,
    confirmed: bool,
    login_verified: Optional[bool] = None,
) -> None:
    """Persist registration credentials to a local file with restricted permissions.

    The password is saved in plaintext — ``chmod 0600`` restricts the file
    to the current user only.  Credentials are always saved even when
    ``confirmed`` is False, because the password is still needed to
    complete confirmation manually later.
    """
    credentials_dir = storage_dir / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    cred_file = credentials_dir / f"{hostname}.json"
    cred_data = {
        "hostname": hostname,
        "email": email,
        "password": password,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "confirmed": confirmed,
        "login_verified": login_verified,
    }
    cred_file.write_text(json.dumps(cred_data, indent=2) + "\n")
    cred_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — owner read/write only
    click.echo(f"  Credentials saved to {cred_file}")
    if not confirmed:
        click.echo(
            "  WARNING: Registration completed but email confirmation was NOT "
            "received. The password has been saved so you can complete "
            "confirmation manually — the account may not be fully active.",
            err=True,
        )


# ---------------------------------------------------------------------------
# Phase 2: Agent explorer backends
# ---------------------------------------------------------------------------


async def _run_phase2_custom(
    *,
    session,
    result,
    prior_endpoints: list,
    llm_provider: str,
    llm_model: str,
    llm_api_key: str,
    llm_base_url: Optional[str],
    llm_max_tokens: Optional[int],
    hostname: str,
    scope_pattern: Union[str, List[str]],
) -> None:
    """Run aiBrowser's own AgentExplorer (default, stable)."""
    click.echo(f"\n[Phase 2] Running agent explorer on {hostname}...")
    explorer_config = ExplorerConfig(
        authorized_hostname=scope_pattern,
        storage_key=hostname,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url or "",
        llm_max_tokens=llm_max_tokens,
    )
    explorer = AgentExplorer(explorer_config)

    explorer_page = await session.new_page()
    try:
        await explorer_page.goto(f"https://{hostname}", timeout=30000)
    except Exception as exc:
        logger.warning(
            "Failed to navigate to %s for agent exploration: %s",
            hostname, exc,
        )
        click.echo(f"  Agent exploration skipped: could not load {hostname}.")
    else:
        audit_entries = await explorer.explore(session, explorer_page)
        click.echo(f"  Agent took {len(audit_entries)} autonomous actions.")

        phase1_urls: set[str] = {
            Crawler._normalize(ep.url) for ep in result.endpoints
        }
        if prior_endpoints:
            phase1_urls |= {
                Crawler._normalize(ep["url"]) for ep in prior_endpoints
            }

        agent_urls: set[str] = set()
        for entry in audit_entries:
            if entry.action.current_url:
                normalized = Crawler._normalize(entry.action.current_url)
                agent_urls.add(normalized)
                result.add_endpoint(
                    entry.action.current_url,
                    DiscoveryMethod.AGENT_EXPLORATION,
                )

        new_count = len(agent_urls - phase1_urls)
        already_known_count = len(agent_urls & phase1_urls)
        logger.info(
            "Agent exploration summary: %d actions taken, %d new "
            "endpoints discovered, %d already known",
            len(audit_entries), new_count, already_known_count,
        )
        click.echo(f"  Agent discovered {new_count} new endpoint(s).")
    finally:
        await explorer_page.close()


async def _run_phase2_browser_use(
    *,
    session,
    result,
    prior_endpoints: list,
    llm_provider: str,
    llm_model: str,
    llm_api_key: str,
    llm_base_url: Optional[str],
    max_actions: int,
    agent_task: Optional[str],
    hostname: str,
    scope_pattern: Union[str, List[str]],
) -> None:
    """Run browser-use as the Phase 2 agent engine, connected to the same
    browser process via CDP (--remote-debugging-port)."""
    try:
        from browser_use import Agent as BrowserUseAgent
        from browser_use import Browser as BrowserUseBrowser
        from browser_use import BrowserConfig as BUBrowserConfig
        from browser_use import BrowserContextConfig
    except ImportError:
        raise click.ClickException(
            "browser-use is not installed.  Install it with:\n"
            "    pip install -e \".[browser-use]\"\n"
            "or:\n"
            "    pip install \"browser-use==0.1.48\" \"langchain-deepseek>=0.1\""
        )

    if not session.cdp_url:
        raise click.ClickException(
            "BrowserSession did not expose a CDP endpoint — this is a bug. "
            "Make sure expose_cdp=True was passed to BrowserSessionConfig."
        )

    click.echo(f"\n[Phase 2] Running browser-use agent on {hostname}...")

    # ---- LLM ----------------------------------------------------------
    if llm_provider == "deepseek":
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError:
            raise click.ClickException(
                "langchain-deepseek is not installed.  Install it with:\n"
                "    pip install -e \".[browser-use]\""
            )
        llm = ChatDeepSeek(
            model=llm_model,
            api_key=llm_api_key,
            # DeepSeek V4 models are always in thinking mode by default
            # and reject forced tool-calling — required for browser-use's
            # structured actions.  Confirmed via testing (DeepSeek-V3
            # GitHub issue #1376, multiple LangChain issues).
            extra_body={"thinking": {"type": "disabled"}},
        )
        if llm_base_url:
            llm.model_kwargs = llm.model_kwargs or {}
            llm.model_kwargs["base_url"] = llm_base_url
    elif llm_provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise click.ClickException(
                "langchain-anthropic is not installed.  Install it with:\n"
                "    pip install -e \".[browser-use]\""
            )
        llm = ChatAnthropic(model=llm_model, api_key=llm_api_key)
    elif llm_provider == "openai":
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=llm_model,
            api_key=llm_api_key,
            base_url=llm_base_url or None,
        )
    else:
        raise click.ClickException(
            f"--agent-backend browser-use does not yet support "
            f"--llm-provider {llm_provider}"
        )

    # ---- Confirm hostname loads before handing off --------------------
    explorer_page = await session.new_page()
    try:
        await explorer_page.goto(f"https://{hostname}", timeout=30000)
    except Exception as exc:
        logger.warning(
            "Failed to navigate to %s for browser-use exploration: %s",
            hostname, exc,
        )
        click.echo(f"  Agent exploration skipped: could not load {hostname}.")
        return
    finally:
        await explorer_page.close()

    # ---- Browser-use setup (CDP attach) -------------------------------
    bu_browser_config = BUBrowserConfig(
        cdp_url=session.cdp_url,
        keep_alive=True,
        headless=True,
    )
    bu_context_config = BrowserContextConfig(
        allowed_domains=as_scope_list(scope_pattern),
        # disable_security is deliberately NOT set here.  When cdp_url
        # is in use and the connected browser already has a context
        # (which it does — aiBrowser creates it before hand-off),
        # browser-use's _create_context() reuses browser.contexts[0]
        # and skips the branch where disable_security would apply.
        # The actual HTTPS-through-Burp fix is the
        # --ignore-certificate-errors Chromium launch arg in
        # BrowserSession.start().
        wait_for_network_idle_page_load_time=1.0,
        minimum_wait_page_load_time=0.5,
        maximum_wait_page_load_time=5.0,
    )

    bu_browser = BrowserUseBrowser(config=bu_browser_config)
    bu_ctx = await bu_browser.new_context(config=bu_context_config)
    await bu_ctx._initialize_session()

    # Clean any leftover pages from Phase 1 in the CDP context
    pw_context = bu_ctx.session.context
    for page in list(pw_context.pages):
        try:
            await page.close()
        except Exception:
            pass

    # ---- Task ---------------------------------------------------------
    # The no-forms/no-registration instruction is appended unconditionally,
    # even to a custom --agent-task. Unlike the 'custom' backend
    # (AgentExplorer), which has a separate denylist check independent of
    # its prompt, this task string is currently the ONLY thing preventing
    # the browser-use agent from submitting forms or registering accounts.
    # A custom task forgetting to mention this would otherwise silently
    # drop that constraint.
    _no_forms_instruction = (
        "Do not submit any forms or attempt to register/sign up for "
        "anything, regardless of any other instructions in this task."
    )
    if agent_task:
        task = f"{agent_task.rstrip()} {_no_forms_instruction}"
    else:
        task = (
            f"Explore {hostname} thoroughly. Look for API documentation, "
            f"developer resources, webhook/callback configuration pages, "
            f"and other technical endpoints. Click through navigation menus, "
            f"expand collapsed sections, and follow links that seem likely "
            f"to reveal more of the site's structure. {_no_forms_instruction}"
        )

    bu_agent = BrowserUseAgent(
        task=task,
        llm=llm,
        browser=bu_browser,
        browser_context=bu_ctx,
        use_vision=False,
    )

    click.echo(f"  browser-use agent starting (CDP: {session.cdp_url})...")
    visited_urls: list[str] = []
    try:
        bu_result = await bu_agent.run(max_steps=max_actions)
        visited_urls = _extract_urls_from_browser_use_result(bu_result)
        click.echo(f"  browser-use agent completed. {len(visited_urls)} URLs visited.")
    except Exception as exc:
        logger.warning("browser-use agent failed: %s", exc)
        click.echo(f"  browser-use agent error: {exc}")
    finally:
        # Clean up pages so the CDP context is empty for any future
        # browser-use runs in the same browser process.
        try:
            for page in list(pw_context.pages):
                await page.close()
        except Exception:
            pass
        try:
            await bu_agent.close()
        except Exception:
            pass
        try:
            await bu_browser.close()
        except Exception:
            pass

    if not visited_urls:
        return

    # ---- Merge discovered URLs ---------------------------------------
    phase1_urls: set[str] = {
        Crawler._normalize(ep.url) for ep in result.endpoints
    }
    if prior_endpoints:
        phase1_urls |= {
            Crawler._normalize(ep["url"]) for ep in prior_endpoints
        }

    agent_urls: set[str] = set()
    for url in visited_urls:
        # Empty/blank filtering is handled by _extract_urls_from_browser_use_result
        normalized = Crawler._normalize(url)
        agent_urls.add(normalized)
        result.add_endpoint(url, DiscoveryMethod.AGENT_EXPLORATION)

    new_count = len(agent_urls - phase1_urls)
    already_known_count = len(agent_urls & phase1_urls)
    logger.info(
        "browser-use exploration summary: %d new endpoints discovered, "
        "%d already known",
        new_count, already_known_count,
    )
    click.echo(f"  Agent discovered {new_count} new endpoint(s) "
               f"(via browser-use).")


def _extract_urls_from_browser_use_result(bu_result) -> list[str]:
    """Extract visited URLs from a browser-use AgentHistoryList result.

    Uses the ``urls()`` method confirmed correct via verify_browser_use_safety.py
    testing — it returns ``[h.state.url for h in self.history]``.

    Filters out None entries, empty strings, and ``"about:blank"``.
    ``urls()`` has been observed to include a leading empty string, and
    failed/initial navigation state is often ``"about:blank"`` rather
    than ``None``.  The caller should handle deduplication — this
    function returns URLs in the order they were visited, including
    duplicates.
    """
    if bu_result is None:
        return []
    if hasattr(bu_result, "urls"):
        return [
            u for u in bu_result.urls()
            if u is not None and u != "" and u != "about:blank"
        ]
    return []


async def _run_crawl(
    session_config: BrowserSessionConfig,
    crawl_config: CrawlConfig,
    seed_visited: Optional[set[str]],
    prior_endpoints: list,
    run_agent: bool,
    agent_backend: str,
    max_actions: int,
    agent_task: Optional[str],
    llm_provider: str,
    llm_model: Optional[str],
    llm_api_key: Optional[str],
    llm_base_url: Optional[str],
    llm_max_tokens: Optional[int],
    do_register: bool,
    register_email: Optional[str],
    register_password: str,
    register_name: str,
    signup_url: Optional[str],
    login_verify_url: Optional[str],
    do_login: bool,
    login_email: Optional[str],
    login_password: Optional[str],
    imap_host: Optional[str],
    imap_port: int,
    imap_username: Optional[str],
    imap_password: Optional[str],
    email_timeout: int,
    output_file: Optional[str],
    no_crawl: bool,
    hostname: str,
    scope_pattern: Union[str, List[str]],
    traffic_dir: Optional[str],
    no_traffic_capture: bool,
    email_backend: str,
    disposable_inbox_api_key: Optional[str],
    disposable_inbox_domain: Optional[str],
    cookies_file: Optional[str],
) -> None:
    """Run the full crawl pipeline."""

    # ---- Install a narrow asyncio exception handler to suppress -----------
    # Playwright-internal teardown-race noise: during playwright.stop(), a
    # background Connection.run() task may process an in-flight CDP message
    # for a page/frame/browser object that has already been disposed.  This
    # surfaces as a KeyError whose message is a Playwright object GUID
    # (e.g. "page@3f30ad12ce2c3e61f8200366ccde3521").  The run has already
    # completed and written its results to disk by this point — this is
    # purely cosmetic noise.  We suppress the traceback for this *specific*
    # pattern only, logging at debug level instead of alarming the user.
    #
    # Revisit if: these KeyErrors start appearing *before* a run's real work
    # completes (i.e. not during teardown).  That would suggest an actual
    # object-lifecycle bug in the Playwright usage, not just teardown noise.
    import re

    _playwright_guid_re = re.compile(r"^(page|frame|browser|context)@[0-9a-f]+$")
    loop = asyncio.get_running_loop()
    _original_handler = loop.get_exception_handler()

    def _teardown_race_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, KeyError) and exc.args:
            msg = str(exc.args[0])
            if _playwright_guid_re.match(msg):
                logger.debug(
                    "Suppressed Playwright teardown-race KeyError for "
                    "already-disposed object %r (this is cosmetic noise "
                    "during playwright.stop())",
                    msg,
                )
                return
        # Fall back to the original handler (or the default)
        if _original_handler is not None:
            _original_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_teardown_race_handler)

    async with BrowserSession(session_config) as session:
        # -- traffic capture -------------------------------------------
        capture: Optional[TrafficCapture] = None
        if not no_traffic_capture:
            _traffic_dir = traffic_dir or f"output/traffic/{hostname}"
            capture = TrafficCapture(Path(_traffic_dir))
            capture.ensure_dirs()
            await capture.attach_to_session(session, scope_pattern)
            click.echo(f"  Traffic capture enabled -> {_traffic_dir}/")

        login_authenticated: Optional[bool] = None
        # Phase 0: Login (before crawl, if requested)
        if do_login and not cookies_file:
            _email = login_email or register_email
            _password = login_password or register_password
            if not _email or not _password:
                click.echo(
                    "ERROR: --login requires --login-email/--login-password or --register-email/--register-password.",
                    err=True,
                )
                return
            click.echo(f"\n[Phase 0] Logging in as {_email}...")
            login_config = LoginConfig(
                login_url=f"https://{hostname}/login",
                email=_email,
                password=_password,
            )
            login_handler = LoginHandler(login_config)
            try:
                await login_handler.login(session)
                login_authenticated = login_handler.authenticated
                click.echo("  Login complete!")
            except Exception as exc:
                click.echo(f"  Login error: {exc}", err=True)

        # Phase 1: Deterministic crawl (skipped when --no-crawl is set)
        if no_crawl:
            click.echo("\n[Phase 1] Skipped (--no-crawl).")
            result = CrawlResult(config=crawl_config)
        else:
            click.echo(f"\n[Phase 1] Starting deterministic crawl of {hostname}...")
            crawler = Crawler(crawl_config, seed_visited=seed_visited)
            result = await crawler.run(session)
            click.echo(
                f"  Crawl complete: {result.total_pages_crawled} pages, "
                f"{len(result.endpoints)} unique endpoints found "
                f"({result.total_js_endpoints} from JS)."
            )

        # Phase 2: Agent explorer for JS-heavy pages.
        # When --no-crawl is set, auto-skip Phase 2 — there's no crawl
        # seed to build exploration on.
        if run_agent and llm_api_key and not no_crawl:
            if not llm_model:
                click.echo(
                    "ERROR: --llm-model is required when using --llm-provider.",
                    err=True,
                )
                return

            if agent_backend == "custom":
                await _run_phase2_custom(
                    session=session,
                    result=result,
                    prior_endpoints=prior_endpoints,
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                    llm_api_key=llm_api_key,
                    llm_base_url=llm_base_url,
                    llm_max_tokens=llm_max_tokens,
                    hostname=hostname,
                    scope_pattern=scope_pattern,
                )
            elif agent_backend == "browser-use":
                await _run_phase2_browser_use(
                    session=session,
                    result=result,
                    prior_endpoints=prior_endpoints,
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                    llm_api_key=llm_api_key,
                    llm_base_url=llm_base_url,
                    max_actions=max_actions,
                    agent_task=agent_task,
                    hostname=hostname,
                    scope_pattern=scope_pattern,
                )

        elif run_agent and not llm_api_key:
            click.echo(
                "\n[Phase 2] Skipped: --no-agent or LLM_API_KEY not set.",
                err=True,
            )

        # Phase 3: Registration
        skip_registration = bool(cookies_file) or (login_authenticated is True)
        if do_register and not skip_registration:
            # ---- Validate email backend configuration --------------------------
            if email_backend == "disposable":
                if imap_host or imap_username or imap_password:
                    click.echo(
                        "ERROR: --email-backend disposable is mutually exclusive "
                        "with --imap-host/--imap-username/--imap-password.",
                        err=True,
                    )
                    return
                if not disposable_inbox_api_key:
                    click.echo(
                        "ERROR: --disposable-inbox-api-key is required when "
                        "--email-backend disposable is set.",
                        err=True,
                    )
                    return
            else:
                # IMAP/static mode — email is still required
                if not register_email:
                    click.echo(
                        "ERROR: --register-email is required when --register is set "
                        "(or use --email-backend disposable for dynamic provisioning).",
                        err=True,
                    )
                    return

            email_display = register_email or "(will be provisioned)"
            click.echo(f"\n[Phase 3] Attempting registration for {email_display}...")

            imap_config = None
            if imap_host and imap_username and imap_password:
                imap_config = IMAPConfig(
                    host=imap_host,
                    port=imap_port,
                    username=imap_username,
                    password=imap_password,
                )

            disposable_config = None
            if email_backend == "disposable":
                from ai_browser.registration_handler.models import DisposableInboxConfig
                disposable_config = DisposableInboxConfig(
                    api_key=disposable_inbox_api_key,  # type: ignore[arg-type]
                    domain=disposable_inbox_domain,
                )

            reg_config = RegistrationConfig(
                signup_url=(
                    signup_url
                    or (result.endpoints[0].url if result.endpoints else f"https://{hostname}")
                ),
                email=register_email,
                password=register_password,
                name=register_name,
                imap_config=imap_config,
                email_poll_timeout_seconds=email_timeout,
                # Pass prior + fresh crawl endpoints for signup-page discovery.
                # prior_endpoints entries are dicts (from --skip-existing JSON);
                # result.endpoints entries are DiscoveredEndpoint objects.
                candidate_endpoints=(
                    [ep["url"] for ep in prior_endpoints]
                    + [ep.url for ep in result.endpoints]
                ),
                # LLM fields for AI judge (reuse the crawl's LLM config)
                llm_provider=llm_provider,
                llm_model=llm_model or "",
                llm_api_key=llm_api_key or "",
                llm_base_url=llm_base_url or "",
                login_verify_url=login_verify_url,
                disposable_inbox_config=disposable_config,
            )
            handler = RegistrationHandler(reg_config)

            try:
                page = await handler.register(session)
                if not handler.submitted:
                    # No email field found — this page does not look like a
                    # registration form at all (could be a newsletter box,
                    # login form, or a completely unrelated page).
                    click.echo(
                        f"  No registration form found — this page does not "
                        f"appear to have a signup form. "
                        f"Registration was NOT attempted.",
                        err=True,
                    )
                elif handler.registration_looked_real is False:
                    # Form was submitted but the AI judge says the result
                    # doesn't look like a real registration (e.g. newsletter
                    # signup, login, error page, no visible change).
                    click.echo(
                        f"  Form submitted, but the result does not look like "
                        f"a real registration (AI judge verdict: NOT a "
                        f"registration). This may have been a newsletter "
                        f"subscribe box, login form, or something else. "
                        f"Current URL: {page.url}",
                        err=True,
                    )
                elif handler.confirmed:
                    if handler.login_verified is True:
                        click.echo(
                            f"  Registration complete and verified via login! "
                            f"Current URL: {page.url}"
                        )
                    elif handler.login_verified is False:
                        click.echo(
                            f"  Registration form submitted and a confirmation "
                            f"step was attempted, but a follow-up login attempt "
                            f"did NOT succeed — the account may not actually be "
                            f"active. Investigate before relying on these "
                            f"credentials. Current URL: {page.url}",
                            err=True,
                        )
                    else:
                        click.echo(
                            f"  Confirmation action completed (unverified). "
                            f"Current URL: {page.url}"
                        )
                else:
                    click.echo(
                        f"  Registration form submitted, but email confirmation was "
                        f"NOT completed (no confirmation link found within timeout). "
                        f"Account may not be fully active. Current URL: {page.url}",
                        err=True,
                    )
            except Exception as exc:
                click.echo(f"  Registration error: {exc}", err=True)
            else:
                # Save credentials so the account is recoverable.
                # Always save even if confirmation failed — the password
                # is still needed to complete confirmation manually later.
                # Only save if the form was actually submitted (not when no
                # registration form was found at all).
                if handler.submitted:
                    _save_credentials(
                        storage_dir=session_config.storage_dir,
                        hostname=hostname,
                        email=handler.provisioned_email or register_email or "",
                        password=register_password,
                        confirmed=handler.confirmed,
                        login_verified=handler.login_verified,
                    )

        # -- traffic capture summary ----------------------------------
        else:
            click.echo(f"\n[Phase 3] Skipped - using provided cookies or already authenticated for {hostname}.")
        if capture is not None:
            click.echo(f"\n  {capture.summary}")

        # Output results
        # Deduplicate new endpoints against prior endpoints so skipped
        # URLs are not included twice. Prior entries win on conflict
        # (they represent already-verified data from a previous run).
        existing_urls = {Crawler._normalize(ep["url"]) for ep in prior_endpoints}
        new_endpoints = [
            {
                "url": ep.url,
                "method": ep.method.value,
                "source_url": ep.source_url,
                "discovered_at": ep.discovered_at.isoformat(),
            }
            for ep in result.endpoints
            if Crawler._normalize(ep.url) not in existing_urls
        ]

        output_data = {
            "hostname": hostname,
            "total_pages_crawled": result.total_pages_crawled,
            "total_links_discovered": result.total_links_discovered,
            "total_js_endpoints": result.total_js_endpoints,
            "unique_urls": result.unique_urls,
            "endpoints": prior_endpoints + new_endpoints,
            "errors": result.errors,
        }
        if prior_endpoints:
            output_data["skipped_existing_count"] = len(prior_endpoints)
            output_data["newly_discovered_count"] = len(new_endpoints)

        if output_file:
            Path(output_file).write_text(json.dumps(output_data, indent=2))
            click.echo(f"\nResults written to {output_file}")
        else:
            click.echo(f"\n{json.dumps(output_data, indent=2)}")

    # Restore the original asyncio exception handler now that
    # BrowserSession.stop() (and its associated playwright.stop()) has
    # completed.  Early returns inside the async-with block (all of which
    # are user-input errors before any real crawling) skip this line, but
    # those paths exit the process soon after so the handler leak is benign.
    loop.set_exception_handler(_original_handler)


if __name__ == "__main__":
    main()
