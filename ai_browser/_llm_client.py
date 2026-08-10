"""Shared LLM HTTP client — extracted from AgentExplorer so RegistrationHandler
and other modules can make bounded LLM classification calls without duplicating
the per-provider HTTP wiring."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_ANTHROPIC_DEFAULT_MAX_TOKENS = 4096


async def _call_anthropic(
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[list[dict]] = None,
    tool_choice: Optional[dict] = None,
    base_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> httpx.Response:
    """Call Anthropic's Messages API and return the raw response."""
    url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body: dict = {
        "model": model,
        "max_tokens": max_tokens or _ANTHROPIC_DEFAULT_MAX_TOKENS,
        "messages": messages,
    }
    if system_prompt:
        body["system"] = system_prompt
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice

    _client = client or httpx.AsyncClient(timeout=30.0)
    if client is None:
        try:
            return await _client.post(url, json=body, headers=headers)
        finally:
            await _client.aclose()
    else:
        return await _client.post(url, json=body, headers=headers)


async def _call_openai_compatible(
    provider: str,
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[list[dict]] = None,
    tool_choice: Optional[dict] = None,
    base_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> httpx.Response:
    """Call an OpenAI-compatible Chat Completions endpoint and return the raw response."""
    if base_url:
        url = base_url.rstrip("/") + "/chat/completions"
    elif provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
    else:
        url = "https://api.deepseek.com/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    full_messages: list[dict] = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    body: dict = {
        "model": model,
        "messages": full_messages,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice

    _client = client or httpx.AsyncClient(timeout=30.0)
    if client is None:
        try:
            return await _client.post(url, json=body, headers=headers)
        finally:
            await _client.aclose()
    else:
        return await _client.post(url, json=body, headers=headers)


async def call_llm(
    provider: str,
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
    base_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Send *messages* to the given *provider* and return the text response.

    This is a simplified convenience wrapper for single-question classification
    calls (e.g. from RegistrationHandler) that don't need tool-use or the full
    response object.  Returns ``None`` on any error — callers should always
    fail open.
    """
    try:
        if provider == "anthropic":
            resp = await _call_anthropic(
                api_key=api_key,
                model=model,
                messages=messages,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                base_url=base_url,
                client=client,
            )
            resp.raise_for_status()
            data = resp.json()
            # Extract the first text content block
            for block in data.get("content", []):
                if block.get("type") == "text":
                    result = block.get("text", "").strip()
                    return result if result else None
            return None

        elif provider in ("openai", "deepseek"):
            resp = await _call_openai_compatible(
                provider=provider,
                api_key=api_key,
                model=model,
                messages=messages,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                base_url=base_url,
                client=client,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                result = (choices[0].get("message", {}).get("content", "") or "").strip()
                return result if result else None
            return None

        else:
            logger.error("Unknown LLM provider: %s", provider)
            return None

    except Exception as exc:
        logger.debug("LLM call failed (%s): %s", provider, exc)
        return None
