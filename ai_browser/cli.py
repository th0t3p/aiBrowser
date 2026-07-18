"""CLI entry point for ai_browser."""

from __future__ import annotations

import asyncio
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
    help="LLM provider for agent_explorer.",
)
@click.option(
    "--llm-model",
    default=None,
    help="Model name (provider-specific). Defaults: claude-sonnet-4-20250514 / gpt-4o / deepseek-chat.",
)
@click.option(
    "--llm-api-key",
    default=None,
    envvar="LLM_API_KEY",
    help="API key for the LLM provider (or set LLM_API_KEY env var).",
)
@click.option(
    "--llm-base-url",
    default=None,
    help="Custom base URL for the LLM API (falls back to provider default).",
)
@click.option(
    "--anthropic-api-key",
    default=None,
    envvar="ANTHROPIC_API_KEY",
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
    help="IMAP server hostname for email confirmation polling.",
)
@click.option(
    "--imap-port",
    default=993,
    show_default=True,
    help="IMAP server port.",
)
@click.option(
    "--imap-username",
    default=None,
    help="IMAP login username (full email address).",
)
@click.option(
    "--imap-password",
    default=None,
    envvar="IMAP_PASSWORD",
    help="IMAP login password (or set IMAP_PASSWORD env var).",
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
    "--headless/--visible",
    default=True,
    help="Run browser headless (default) or visible.",
)
@click.option(
    "--ca-cert",
    default=None,
    type=click.Path(exists=True),
    help="Path to exported Burp CA certificate (DER/PEM) for HTTPS trust.",
)
@click.option(
    "--storage-dir",
    default="storage/browser_states",
    show_default=True,
    type=click.Path(),
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

    # Run
    asyncio.run(
        _run_crawl(
            session_config=session_config,
            crawl_config=crawl_config,
            run_agent=agent,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_api_key=_llm_api_key,
            llm_base_url=llm_base_url,
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
    run_agent: bool,
    llm_provider: str,
    llm_model: Optional[str],
    llm_api_key: Optional[str],
    llm_base_url: Optional[str],
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
        crawler = Crawler(crawl_config)
        result = await crawler.run(session)
        click.echo(
            f"  Crawl complete: {result.total_pages_crawled} pages, "
            f"{len(result.endpoints)} unique endpoints found "
            f"({result.total_js_endpoints} from JS)."
        )

        # Phase 2: Agent explorer for JS-heavy pages
        if run_agent and llm_api_key:
            click.echo(f"\n[Phase 2] Running agent explorer on {hostname}...")
            explorer_config = ExplorerConfig(
                authorized_hostname=scope_pattern,
                llm_provider=llm_provider,
                llm_model=llm_model or "",
                llm_api_key=llm_api_key,
                llm_base_url=llm_base_url or "",
            )
            explorer = AgentExplorer(explorer_config)

            # Start from the first page
            pages = session.pages
            if pages:
                audit_entries = await explorer.explore(session, pages[0])
                click.echo(f"  Agent took {len(audit_entries)} autonomous actions.")

                # Add discovered URLs from agent exploration
                for entry in audit_entries:
                    if entry.action.current_url:
                        result.add_endpoint(
                            entry.action.current_url,
                            DiscoveryMethod.LINK,
                        )

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
                click.echo(f"  Registration complete! Current URL: {page.url}")
            except Exception as exc:
                click.echo(f"  Registration error: {exc}", err=True)

        # Output results
        import json as json_mod

        output_data = {
            "hostname": hostname,
            "total_pages_crawled": result.total_pages_crawled,
            "total_links_discovered": result.total_links_discovered,
            "total_js_endpoints": result.total_js_endpoints,
            "unique_urls": result.unique_urls,
            "endpoints": [
                {
                    "url": ep.url,
                    "method": ep.method.value,
                    "source_url": ep.source_url,
                    "discovered_at": ep.discovered_at.isoformat(),
                }
                for ep in result.endpoints
            ],
            "errors": result.errors,
        }

        if output_file:
            Path(output_file).write_text(json_mod.dumps(output_data, indent=2))
            click.echo(f"\nResults written to {output_file}")
        else:
            click.echo(f"\n{json_mod.dumps(output_data, indent=2)}")


if __name__ == "__main__":
    main()
