"""TrafficCapture — hooks Playwright page response events to record HTTP traffic.

Each captured request/response pair is written as one JSON line to an
append-only ``index.jsonl`` file.  Request and response bodies are stored
using a content-addressed scheme under ``bodies/<sha256>.bin`` with
automatic deduplication — identical bodies share a single file on disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Response

from ai_browser._scope import hostname_matches_scope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TrafficCapture:
    """Captures in-scope HTTP request/response pairs to a file-based store.

    Usage::

        capture = TrafficCapture(Path("output/traffic/example.com"))
        capture.ensure_dirs()
        await capture.attach_to_page(page, "*.example.com")
        # … crawler runs, every "response" event on *page* is captured …
        logger.info(capture.summary)
    """

    def __init__(self, traffic_dir: Path) -> None:
        self.traffic_dir = Path(traffic_dir)
        self.bodies_dir = self.traffic_dir / "bodies"
        self.index_path = self.traffic_dir / "index.jsonl"

        self._scope_pattern: Optional[str] = None
        self._record_count: int = 0
        self._body_hashes_seen: set[str] = set()
        self._deduped_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_dirs(self) -> None:
        """Create the traffic directory and bodies/ subdirectory if needed."""
        self.traffic_dir.mkdir(parents=True, exist_ok=True)
        self.bodies_dir.mkdir(parents=True, exist_ok=True)

    async def attach_to_page(self, page: Page, scope_pattern: str) -> None:
        """Register a ``page.on('response')`` handler that captures every
        in-scope response to the traffic store.

        Safe to call multiple times — each call registers an additional
        independent listener, which is harmless but wasteful.  Prefer
        calling once per page lifetime.
        """
        self._scope_pattern = scope_pattern

        async def _on_response(response: Response) -> None:
            await self._capture(response)

        page.on("response", _on_response)

    async def attach_to_session(
        self, session: object, scope_pattern: str
    ) -> None:
        """Attach to all current and future pages in a BrowserSession.

        *session* must be a ``BrowserSession`` whose ``start()`` has
        already been called (i.e. ``session._context`` exists).
        """
        # Avoid circular import — BrowserSession is only needed for the
        # type hint on `_context`.
        ctx = getattr(session, "_context", None)
        if ctx is None:
            raise RuntimeError(
                "BrowserSession has no _context — call session.start() first."
            )

        # Attach to already-open pages.
        for page in getattr(session, "pages", []):
            await self.attach_to_page(page, scope_pattern)

        # Attach to every page created from now on.
        def _on_new_page(page: Page) -> None:
            asyncio.ensure_future(self.attach_to_page(page, scope_pattern))

        ctx.on("page", _on_new_page)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    @property
    def summary(self) -> str:
        """Human-readable run summary matching the spec."""
        unique = len(self._body_hashes_seen)
        return (
            f"Captured {self._record_count} requests -> {self.traffic_dir}/ "
            f"({unique} unique body files, {self._deduped_count} deduped)"
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_in_scope(self, url: str) -> bool:
        if not self._scope_pattern:
            return False
        hostname = urlparse(url).hostname or ""
        return hostname_matches_scope(hostname, self._scope_pattern)

    async def _capture(self, response: Response) -> None:
        request = response.request
        url = request.url

        if not self._is_in_scope(url):
            return

        captured_at = datetime.now(timezone.utc).isoformat()
        method = request.method

        # -- request_id --------------------------------------------------
        request_id_raw = f"{method}:{url}:{captured_at}"
        request_id = hashlib.sha256(request_id_raw.encode()).hexdigest()

        # -- query_params ------------------------------------------------
        parsed = urlparse(url)
        query_params: dict[str, list[str]] = {}
        if parsed.query:
            for k, v in parse_qs(parsed.query, keep_blank_values=True).items():
                query_params[k] = v

        # -- request_headers ---------------------------------------------
        request_headers: dict[str, str] = dict(request.headers)

        # -- request body ------------------------------------------------
        request_body_ref, request_body_sha256 = await self._capture_body(
            _read_request_body(request), url, "request"
        )

        # -- response headers & status -----------------------------------
        response_headers: dict[str, str] = {}
        response_status: Optional[int] = None
        try:
            response_headers = dict(response.headers)
        except Exception:
            pass
        try:
            response_status = response.status
        except Exception:
            pass

        # -- response body -----------------------------------------------
        response_body_bytes: Optional[bytes] = None
        try:
            response_body_bytes = await response.body()
        except Exception as exc:
            logger.debug(
                "Failed to read response body for %s: %s", url, exc
            )

        response_body_ref, response_body_sha256 = await self._capture_body(
            response_body_bytes, url, "response"
        )

        # -- write record ------------------------------------------------
        record: dict = {
            "schema_version": "1.0",
            "request_id": request_id,
            "captured_at": captured_at,
            "method": method,
            "url": url,
            "query_params": query_params,
            "request_headers": request_headers,
            "request_body_ref": request_body_ref,
            "request_body_sha256": request_body_sha256,
            "response_status": response_status,
            "response_headers": response_headers,
            "response_body_ref": response_body_ref,
            "response_body_sha256": response_body_sha256,
        }

        with open(self.index_path, "a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

        self._record_count += 1

    async def _capture_body(
        self,
        body: Optional[bytes],
        url: str,
        label: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Content-address *body*, returning (ref, sha256).

        Returns ``(None, None)`` when *body* is ``None``.
        """
        if body is None:
            return None, None

        sha = hashlib.sha256(body).hexdigest()
        ref = f"bodies/{sha}.bin"
        body_path = self.bodies_dir / f"{sha}.bin"

        if sha not in self._body_hashes_seen:
            try:
                body_path.write_bytes(body)
            except OSError as exc:
                logger.debug(
                    "Failed to write %s body for %s to %s: %s",
                    label, url, body_path, exc,
                )
                return None, None
            self._body_hashes_seen.add(sha)
        else:
            self._deduped_count += 1

        return ref, sha


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_request_body(request) -> Optional[bytes]:
    """Best-effort extraction of the raw request body from a Playwright Request."""
    try:
        buf = request.post_data_buffer
        if buf is not None:
            return bytes(buf)
    except Exception:
        pass

    try:
        data = request.post_data
        if data is not None:
            return data.encode("utf-8") if isinstance(data, str) else bytes(data)
    except Exception:
        pass

    return None
