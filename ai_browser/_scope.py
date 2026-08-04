"""Shared scope-matching utilities for hostname authorization checks.

Used by both BrowserSession (route-level guard) and AgentExplorer (defense-in-depth).
Supports glob patterns so that ``*.example.com`` matches ``app.example.com``, etc.
"""

import fnmatch
from typing import List, Union
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Single-pattern matching (kept for backward compatibility)
# ---------------------------------------------------------------------------


def hostname_matches_scope(hostname: str, scope_pattern: str) -> bool:
    """Return True if *hostname* is within the authorized *scope_pattern*.

    *scope_pattern* can be:
        - An exact hostname: ``"example.com"``
        - A glob: ``"*.example.com"`` matches ``app.example.com``, ``api.example.com``
        - A glob: ``"example.*"`` matches ``example.com``, ``example.org``

    Comparison is case-insensitive.
    """
    if not hostname or not scope_pattern:
        return False

    hostname = hostname.lower().strip()
    scope_pattern = scope_pattern.lower().strip()

    # Fast path: exact match
    if hostname == scope_pattern:
        return True

    # Glob match (supports * and ?)
    if fnmatch.fnmatch(hostname, scope_pattern):
        return True

    return False


def page_url_matches_scope(page_url: str, scope_pattern: str) -> bool:
    """Return True if the hostname of *page_url* matches *scope_pattern*."""
    parsed = urlparse(page_url)
    hostname = parsed.hostname or ""
    return hostname_matches_scope(hostname, scope_pattern)


# ---------------------------------------------------------------------------
# Multi-pattern helpers (--scope-file support)
# ---------------------------------------------------------------------------


def as_scope_list(value: Union[str, List[str]]) -> List[str]:
    """Normalize a single pattern or a list of patterns to a list."""
    return [value] if isinstance(value, str) else list(value)


def hostname_matches_any_scope(hostname: str, patterns: Union[str, List[str]]) -> bool:
    """True if *hostname* matches ANY pattern in *patterns* (str or list)."""
    return any(
        hostname_matches_scope(hostname, p) for p in as_scope_list(patterns)
    )


def page_url_matches_any_scope(page_url: str, patterns: Union[str, List[str]]) -> bool:
    """True if the hostname of *page_url* matches ANY pattern in *patterns*."""
    parsed = urlparse(page_url)
    hostname = parsed.hostname or ""
    return hostname_matches_any_scope(hostname, patterns)


def display_scope(patterns: Union[str, List[str]]) -> str:
    """Return a human-readable representation of the scope pattern(s)."""
    plist = as_scope_list(patterns)
    if len(plist) == 1:
        return repr(plist[0])
    return "[" + ", ".join(repr(p) for p in plist) + "]"


class ScopeError(Exception):
    """Raised when a hostname or page URL falls outside the authorized scope."""
    pass
