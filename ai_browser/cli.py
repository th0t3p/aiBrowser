"""CLI entry point for ai_browser."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from ai_browser.browser_session import BrowserSession, BrowserSessionConfig, ProxyConfig
from ai_browser.crawler import Crawler, CrawlConfig, DiscoveryMethod
from ai_browser.agent_explorer import AgentExplorer, ExplorerConfig
from ai_browser.registration_handler import RegistrationHandler, RegistrationConfig, IMAPConfig
from ai_browser.login_handler import LoginHandler, LoginConfig

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
║  All traffic is proxied through Burp Suite (127.0.0.1:8080) ║
║  for capture via aiScraper. This tool does NOT log traffic   ║
║  itself — Burp Suite is the source of truth.                ║
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

    All traffic is routed through Burp Suite proxy (default 127.0.0.1:8080).
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
    "--proxy-server",
    default="http://127.0.0.1:8080",
    show_default=True,
    envvar="AIBROWSER_PROXY_SERVER",
    help="Burp Suite proxy address.",
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
    default=4096,
    show_default=True,
    envvar="AIBROWSER_LLM_MAX_TOKENS",
    help="Max tokens for LLM API calls during agent exploration. "
    "Reasoning-enabled models (e.g. DeepSeek v4) consume tokens "
    "on internal reasoning before the final answer — too low a "
    "value can result in empty responses.",
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
    default="Test1234!@#$",
    help="Password for registration.",
)
@click.option(
    "--register-name",
    default="Test User",
    help="Full name for registration.",
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
@click.pass_context
def crawl(
    ctx: click.Context,
    hostname: str,
    authorized: bool,
    scope: Optional[str],
    proxy_server: str,
    max_depth: int,
    max_pages: int,
    agent: bool,
    llm_provider: str,
    llm_model: Optional[str],
    llm_api_key: Optional[str],
    llm_base_url: Optional[str],
    llm_max_tokens: int,
    anthropic_api_key: Optional[str],
    register: bool,
    register_email: Optional[str],
    register_password: str,
    register_name: str,
    login: bool,
    login_email: Optional[str],
    login_password: Optional[str],
    imap_host: Optional[str],
    imap_port: int,
    imap_username: Optional[str],
    imap_password: Optional[str],
    email_timeout: int,
    output: Optional[str],
    skip_existing: Optional[str],
    headless: bool,
    ca_cert: Optional[str],
    storage_dir: str,
):
    """Crawl HOSTNAME through Burp proxy, discovering URLs and endpoints.

    HOSTNAME is the target hostname to crawl (e.g. example.com).
    The --authorized flag MUST be provided.
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

    start_url = f"https://{hostname}"
    scope_pattern = scope or hostname

    # If --scope is provided, warn if the seed hostname doesn't match
    if scope and hostname != scope:
        from ai_browser._scope import hostname_matches_scope
        if not hostname_matches_scope(hostname, scope):
            click.echo(
                f"⚠ Warning: seed hostname '{hostname}' does not match "
                f"scope pattern '{scope}'. The crawl will start outside its "
                f"own declared scope.",
                err=True,
            )

    # Build browser session config
    session_config = BrowserSessionConfig(
        authorized_hostname=scope_pattern,
        proxy=ProxyConfig(server=proxy_server),
        headless=headless,
        storage_dir=Path(storage_dir),
        ca_cert_path=Path(ca_cert) if ca_cert else None,
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
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_api_key=_llm_api_key,
            llm_base_url=llm_base_url,
            llm_max_tokens=llm_max_tokens,
            do_register=register,
            register_email=register_email,
            register_password=register_password,
            register_name=register_name,
            do_login=login,
            login_email=login_email,
            login_password=login_password,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_username=imap_username,
            imap_password=imap_password,
            email_timeout=email_timeout,
            output_file=output,
            hostname=hostname,
            scope_pattern=scope_pattern,
        )
    )


async def _run_crawl(
    session_config: BrowserSessionConfig,
    crawl_config: CrawlConfig,
    seed_visited: Optional[set[str]],
    prior_endpoints: list,
    run_agent: bool,
    llm_provider: str,
    llm_model: Optional[str],
    llm_api_key: Optional[str],
    llm_base_url: Optional[str],
    llm_max_tokens: int,
    do_register: bool,
    register_email: Optional[str],
    register_password: str,
    register_name: str,
    do_login: bool,
    login_email: Optional[str],
    login_password: Optional[str],
    imap_host: Optional[str],
    imap_port: int,
    imap_username: Optional[str],
    imap_password: Optional[str],
    email_timeout: int,
    output_file: Optional[str],
    hostname: str,
    scope_pattern: str,
) -> None:
    """Run the full crawl pipeline."""

    async with BrowserSession(session_config) as session:
        # Phase 0: Login (before crawl, if requested)
        if do_login:
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
                click.echo("  Login complete!")
            except Exception as exc:
                click.echo(f"  Login error: {exc}", err=True)

        # Phase 1: Deterministic crawl
        click.echo(f"\n[Phase 1] Starting deterministic crawl of {hostname}...")
        crawler = Crawler(crawl_config, seed_visited=seed_visited)
        result = await crawler.run(session)
        click.echo(
            f"  Crawl complete: {result.total_pages_crawled} pages, "
            f"{len(result.endpoints)} unique endpoints found "
            f"({result.total_js_endpoints} from JS)."
        )

        # Phase 2: Agent explorer for JS-heavy pages
        if run_agent and llm_api_key:
            if not llm_model:
                click.echo(
                    "ERROR: --llm-model is required when using --llm-provider.",
                    err=True,
                )
                return
            click.echo(f"\n[Phase 2] Running agent explorer on {hostname}...")
            explorer_config = ExplorerConfig(
                authorized_hostname=scope_pattern,
                llm_provider=llm_provider,
                llm_model=llm_model,
                llm_api_key=llm_api_key,
                llm_base_url=llm_base_url or "",
                llm_max_tokens=llm_max_tokens,
            )
            explorer = AgentExplorer(explorer_config)

            # Open a genuinely fresh page and navigate to the target hostname.
            # The crawler closes all its pages in a finally block, so
            # session.pages after Phase 1 contains only an auto-created
            # about:blank tab — not a reflection of crawl outcome.
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

                # Collect URLs already known from Phase 1 + any --skip-existing
                # prior runs, so we can distinguish genuinely new discoveries.
                phase1_urls: set[str] = {
                    Crawler._normalize(ep.url) for ep in result.endpoints
                }
                if prior_endpoints:
                    phase1_urls |= {
                        Crawler._normalize(ep["url"]) for ep in prior_endpoints
                    }

                # Add discovered URLs from agent exploration
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

        elif run_agent and not llm_api_key:
            click.echo(
                "\n[Phase 2] Skipped: --no-agent or LLM_API_KEY not set.",
                err=True,
            )

        # Phase 3: Registration
        if do_register:
            if not register_email:
                click.echo(
                    "ERROR: --register-email is required when --register is set.",
                    err=True,
                )
                return

            click.echo(f"\n[Phase 3] Attempting registration for {register_email}...")

            imap_config = None
            if imap_host and imap_username and imap_password:
                imap_config = IMAPConfig(
                    host=imap_host,
                    port=imap_port,
                    username=imap_username,
                    password=imap_password,
                )

            reg_config = RegistrationConfig(
                signup_url=result.endpoints[0].url if result.endpoints else f"https://{hostname}",
                email=register_email,
                password=register_password,
                name=register_name,
                imap_config=imap_config,
                email_poll_timeout_seconds=email_timeout,
            )
            handler = RegistrationHandler(reg_config)

            try:
                page = await handler.register(session)
                if handler.confirmed:
                    click.echo(f"  Registration complete and confirmed! Current URL: {page.url}")
                else:
                    click.echo(
                        f"  Registration form submitted, but email confirmation was "
                        f"NOT completed (no confirmation link found within timeout). "
                        f"Account may not be fully active. Current URL: {page.url}",
                        err=True,
                    )
            except Exception as exc:
                click.echo(f"  Registration error: {exc}", err=True)

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


if __name__ == "__main__":
    main()
