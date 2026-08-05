"""Disposable inbox backend — currently AgentMail, designed to be extensible.

Uses direct httpx calls (not the agentmail SDK) to avoid adding a dependency.
API reference: https://docs.agentmail.to
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from ai_browser.registration_handler.models import DisposableInboxConfig

logger = logging.getLogger(__name__)

_AGENTMAIL_DEFAULT_BASE = "https://api.agentmail.to"


async def provision_inbox(config: DisposableInboxConfig) -> str:
    """Provision a fresh disposable inbox and return its email address.

    Raises on failure (bad API key, network error, provider outage) — this
    means registration in disposable mode cannot possibly work, so the
    caller should fail fast before attempting any navigation.
    """
    if config.provider != "agentmail":
        raise ValueError(f"Unsupported disposable inbox provider: {config.provider}")

    base = config.base_url or _AGENTMAIL_DEFAULT_BASE
    url = f"{base.rstrip('/')}/v1/inboxes"

    body: dict = {}
    if config.domain:
        body["domain"] = config.domain

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    email: str = data.get("email_address", "")
    if not email:
        raise RuntimeError(
            f"AgentMail inbox created but response missing email_address: {data}"
        )

    logger.info("Provisioned disposable inbox: %s", email)
    return email


async def wait_for_confirmation_link(
    config: DisposableInboxConfig,
    inbox_address: str,
    timeout_seconds: int,
    target_domain: str = "",
) -> Optional[str]:
    """Block until a message arrives or *timeout_seconds* expires, then
    extract a confirmation link from its body.

    Uses the inbox's message list endpoint in a polling loop (the
    AgentMail REST API does not have a dedicated long-poll wait endpoint,
    but the SDK supports WebSockets for real-time delivery; polling is
    simpler and sufficient for this use case).

    Returns ``None`` on timeout — never raises for a normal
    "nothing arrived" outcome.
    """
    if config.provider != "agentmail":
        raise ValueError(f"Unsupported disposable inbox provider: {config.provider}")

    base = config.base_url or _AGENTMAIL_DEFAULT_BASE

    from ai_browser.registration_handler.handler import _extract_link_from_body

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    poll_interval = 5  # seconds between polls

    logger.info(
        "Waiting for confirmation email in disposable inbox %s (timeout=%ds)",
        inbox_address, timeout_seconds,
    )

    # We need the inbox ID. The inbox_address is the email; for AgentMail,
    # we get the inbox_id from listing inboxes and matching by email.
    async with httpx.AsyncClient(timeout=30.0) as client:
        inbox_id: Optional[str] = None

        while asyncio.get_event_loop().time() < deadline:
            # Resolve inbox_id on first iteration
            if inbox_id is None:
                inbox_id = await _resolve_inbox_id(client, base, config.api_key, inbox_address)
                if inbox_id is None:
                    logger.warning(
                        "Could not find inbox for %s — will retry", inbox_address
                    )
                    await asyncio.sleep(poll_interval)
                    continue

            # List messages for this inbox
            try:
                resp = await client.get(
                    f"{base.rstrip('/')}/v1/inboxes/{inbox_id}/messages",
                    headers={"Authorization": f"Bearer {config.api_key}"},
                    params={"limit": 5},
                )
                resp.raise_for_status()
                data = resp.json()
                messages = data.get("data", [])
            except Exception as exc:
                logger.debug("Error listing messages: %s", exc)
                await asyncio.sleep(poll_interval)
                continue

            for msg in messages:
                # Extract body text from the message (API returns body_text or body_html)
                body_text = msg.get("body_text", "") or msg.get("body_html", "") or ""
                if not body_text:
                    # Try fetching full message
                    msg_id = msg.get("id", "")
                    if msg_id:
                        try:
                            full_resp = await client.get(
                                f"{base.rstrip('/')}/v1/inboxes/{inbox_id}/messages/{msg_id}",
                                headers={"Authorization": f"Bearer {config.api_key}"},
                            )
                            full_resp.raise_for_status()
                            full_data = full_resp.json()
                            body_text = (
                                full_data.get("body_text", "")
                                or full_data.get("body_html", "")
                                or ""
                            )
                        except Exception:
                            pass

                if body_text:
                    link = _extract_link_from_body(body_text, target_domain)
                    if link:
                        logger.info("Confirmation link found via disposable inbox: %s", link)
                        return link

            await asyncio.sleep(poll_interval)

    logger.warning("Timed out waiting for confirmation email in disposable inbox")
    return None


async def _resolve_inbox_id(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    email_address: str,
) -> Optional[str]:
    """Find the inbox ID for a given email address by listing inboxes."""
    try:
        resp = await client.get(
            f"{base.rstrip('/')}/v1/inboxes",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        inboxes = data.get("data", [])
        for inbox in inboxes:
            if inbox.get("email_address") == email_address:
                return inbox.get("id")
    except Exception as exc:
        logger.debug("Failed to resolve inbox ID: %s", exc)
    return None
