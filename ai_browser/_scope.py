"""Shared scope-matching utilities for hostname authorization checks.

Used by both BrowserSession (route-level guard) and AgentExplorer (defense-in-depth).
Supports glob patterns so that ``*.example.com`` matches ``app.example.com``, etc.
"""

import fnmatch
import re
from urllib.parse import urlparse


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


class ScopeError(Exception):
    """Raised when a hostname or page URL falls outside the authorized scope."""
    pass
