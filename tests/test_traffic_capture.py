"""Tests for TrafficCapture module."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_browser.traffic_capture import TrafficCapture, _read_request_body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(
    *,
    url: str = "https://example.com/page",
    method: str = "GET",
    status: int = 200,
    body: bytes | None = b"<html>test</html>",
    req_headers: dict[str, str] | None = None,
    resp_headers: dict[str, str] | None = None,
    post_data: str | None = None,
    post_data_buffer: bytes | None = None,
    body_raises: Exception | None = None,
) -> MagicMock:
    """Build a mock Playwright Response with a nested mock Request."""
    mock_request = MagicMock()
    mock_request.method = method
    mock_request.url = url
    mock_request.headers = req_headers or {"accept": "*/*"}
    mock_request.post_data = post_data
    mock_request.post_data_buffer = post_data_buffer

    mock_response = MagicMock()
    mock_response.request = mock_request
    mock_response.status = status
    mock_response.headers = resp_headers or {"content-type": "text/html; charset=utf-8"}

    if body_raises is not None:
        async def _body_raises() -> bytes:
            raise body_raises
        mock_response.body = _body_raises
    else:
        async def _body() -> bytes:
            return body or b""
        mock_response.body = _body

    return mock_response


def _make_mock_page() -> MagicMock:
    """Build a mock Playwright Page whose ``on('response', ...)`` stores
    the handler so tests can invoke it directly."""
    mock_page = MagicMock()
    _handlers: dict[str, list] = {}

    def _on(event: str, handler) -> None:
        _handlers.setdefault(event, []).append(handler)

    mock_page.on = _on
    mock_page._handlers = _handlers  # expose for test access
    return mock_page


async def _fire_response(mock_page: MagicMock, response: MagicMock) -> None:
    """Invoke all registered 'response' handlers with *response*."""
    for handler in mock_page._handlers.get("response", []):
        await handler(response)


# ---------------------------------------------------------------------------
# Tests: body extraction helper
# ---------------------------------------------------------------------------


class TestReadRequestBody:
    def test_post_data_buffer_bytes(self):
        req = MagicMock()
        req.post_data_buffer = b"hello"
        req.post_data = None
        assert _read_request_body(req) == b"hello"

    def test_post_data_str(self):
        req = MagicMock()
        req.post_data_buffer = None
        req.post_data = "world"
        assert _read_request_body(req) == b"world"

    def test_post_data_buffer_priority(self):
        req = MagicMock()
        req.post_data_buffer = b"buf"
        req.post_data = "str"
        assert _read_request_body(req) == b"buf"

    def test_no_body(self):
        req = MagicMock()
        req.post_data_buffer = None
        req.post_data = None
        assert _read_request_body(req) is None

    def test_post_data_buffer_raises(self):
        req = MagicMock()
        type(req).post_data_buffer = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        req.post_data = None
        assert _read_request_body(req) is None

    def test_post_data_decodes_to_bytes(self):
        req = MagicMock()
        req.post_data_buffer = None
        req.post_data = b"raw-bytes"
        assert _read_request_body(req) == b"raw-bytes"


# ---------------------------------------------------------------------------
# Tests: TrafficCapture core
# ---------------------------------------------------------------------------


class TestTrafficCaptureDedup:
    @pytest.mark.asyncio
    async def test_identical_bodies_produce_one_file(self, tmp_path: Path):
        """Two responses with identical body content produce only ONE file in bodies/."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        body = b"same content"
        r1 = _make_mock_response(url="https://example.com/a", body=body)
        r2 = _make_mock_response(url="https://example.com/b", body=body)

        await capture._capture(r1)
        await capture._capture(r2)

        bodies = list((tmp_path / "bodies").iterdir())
        assert len(bodies) == 1, f"Expected 1 body file, got {len(bodies)}: {bodies}"

    @pytest.mark.asyncio
    async def test_dedup_counted_in_summary(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        body = b"dedup me"
        r1 = _make_mock_response(url="https://example.com/a", body=body)
        r2 = _make_mock_response(url="https://example.com/b", body=body)

        await capture._capture(r1)
        await capture._capture(r2)

        assert capture._record_count == 2
        assert capture._deduped_count == 1
        assert "1 unique body files, 1 deduped" in capture.summary

    @pytest.mark.asyncio
    async def test_different_bodies_produce_two_files(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r1 = _make_mock_response(url="https://example.com/a", body=b"alpha")
        r2 = _make_mock_response(url="https://example.com/b", body=b"beta")

        await capture._capture(r1)
        await capture._capture(r2)

        bodies = list((tmp_path / "bodies").iterdir())
        assert len(bodies) == 2

    @pytest.mark.asyncio
    async def test_request_and_response_bodies_share_dedup(self, tmp_path: Path):
        """Identical bytes in request body and response body share a single file."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        shared = b"same-bytes-everywhere"
        r1 = _make_mock_response(
            url="https://example.com/a",
            method="POST",
            body=shared,
            post_data_buffer=shared,
        )

        await capture._capture(r1)

        bodies = list((tmp_path / "bodies").iterdir())
        assert len(bodies) == 1


class TestTrafficCaptureScope:
    @pytest.mark.asyncio
    async def test_in_scope_request_captured(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(url="https://example.com/page")
        await capture._capture(r)

        assert capture._record_count == 1
        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1

    @pytest.mark.asyncio
    async def test_out_of_scope_request_not_captured(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(url="https://evil.com/tracker")
        await capture._capture(r)

        assert capture._record_count == 0
        assert not (tmp_path / "index.jsonl").exists()

    @pytest.mark.asyncio
    async def test_subdomain_glob_matching(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "*.example.com"

        in_scope = _make_mock_response(url="https://api.example.com/v1")
        out_of_scope = _make_mock_response(url="https://example.com/")

        await capture._capture(in_scope)
        await capture._capture(out_of_scope)

        assert capture._record_count == 1  # only the subdomain was captured

    @pytest.mark.asyncio
    async def test_no_scope_pattern_rejects_all(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        # scope_pattern never set -> all urls rejected

        r = _make_mock_response(url="https://example.com/page")
        await capture._capture(r)

        assert capture._record_count == 0


class TestTrafficCaptureBodyFailure:
    @pytest.mark.asyncio
    async def test_body_read_failure_sets_ref_null(self, tmp_path: Path):
        """A response whose body() raises sets response_body_ref to null without crashing."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(
            url="https://example.com/broken",
            body_raises=RuntimeError("body already consumed"),
        )

        await capture._capture(r)

        assert capture._record_count == 1
        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["response_body_ref"] is None
        assert record["response_body_sha256"] is None

    @pytest.mark.asyncio
    async def test_request_body_read_failure_when_post_data_raises(self, tmp_path: Path):
        """Post data attr that raises on access results in null ref without crash."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        req = MagicMock()
        req.method = "POST"
        req.url = "https://example.com/api"
        req.headers = {"content-type": "application/json"}
        # Make post_data_buffer raise
        type(req).post_data_buffer = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        type(req).post_data = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

        mock_response = MagicMock()
        mock_response.request = req
        mock_response.status = 200
        mock_response.headers = {}
        async def _body() -> bytes:
            return b"ok"
        mock_response.body = _body

        await capture._capture(mock_response)

        assert capture._record_count == 1
        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["request_body_ref"] is None
        assert record["request_body_sha256"] is None


class TestTrafficCaptureIndexJsonl:
    @pytest.mark.asyncio
    async def test_one_line_per_captured_request(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        for i in range(5):
            r = _make_mock_response(url=f"https://example.com/page{i}")
            await capture._capture(r)

        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            record = json.loads(line)
            assert record["schema_version"] == "1.0"

    @pytest.mark.asyncio
    async def test_out_of_scope_zero_lines(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        for i in range(3):
            r = _make_mock_response(url=f"https://other-{i}.com/page")
            await capture._capture(r)

        assert not (tmp_path / "index.jsonl").exists()

    @pytest.mark.asyncio
    async def test_query_params_parsed_correctly(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(url="https://example.com/search?q=hello&q=world&page=1")
        await capture._capture(r)

        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["query_params"] == {"q": ["hello", "world"], "page": ["1"]}

    @pytest.mark.asyncio
    async def test_request_id_is_deterministic(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(url="https://example.com/a")
        await capture._capture(r)

        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        rid = record["request_id"]
        assert len(rid) == 64
        assert all(c in "0123456789abcdef" for c in rid)


# ---------------------------------------------------------------------------
# Tests: Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    @pytest.fixture(autouse=True)
    def _load_schema(self):
        from jsonschema import Draft202012Validator

        schema_path = Path(__file__).resolve().parent.parent / "ai_browser" / "traffic_capture" / "schema.json"
        self.schema = json.loads(schema_path.read_text())
        self.validator = Draft202012Validator(self.schema)

    def test_minimal_valid_record(self):
        record = {
            "schema_version": "1.0",
            "request_id": "a" * 64,
            "captured_at": "2024-01-01T00:00:00+00:00",
            "method": "GET",
            "url": "https://example.com/",
            "query_params": {},
            "request_headers": {"accept": "*/*"},
            "request_body_ref": None,
            "request_body_sha256": None,
            "response_status": 200,
            "response_headers": {"content-type": "text/html"},
            "response_body_ref": "bodies/abc123.bin",
            "response_body_sha256": "e" * 64,
        }
        errors = list(self.validator.iter_errors(record))
        assert not errors, f"Unexpected validation errors: {errors}"

    def test_extra_properties_rejected(self):
        record = {
            "schema_version": "1.0",
            "request_id": "a" * 64,
            "captured_at": "2024-01-01T00:00:00+00:00",
            "method": "GET",
            "url": "https://example.com/",
            "query_params": {},
            "request_headers": {},
            "request_body_ref": None,
            "request_body_sha256": None,
            "response_status": None,
            "response_headers": {},
            "response_body_ref": None,
            "response_body_sha256": None,
            "extra_field": "should not be here",
        }
        errors = list(self.validator.iter_errors(record))
        assert len(errors) >= 1

    @pytest.mark.asyncio
    async def test_written_index_jsonl_validates(self, tmp_path: Path):
        """Records actually written by TrafficCapture validate against schema.json."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(
            url="https://example.com/page?x=1&y=2",
            method="POST",
            status=201,
            body=b'{"ok":true}',
            post_data_buffer=b"payload",
        )
        await capture._capture(r)

        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        errors = list(self.validator.iter_errors(record))
        assert not errors, f"Record failed schema validation: {errors}"

    def test_query_params_type_enforced(self):
        """query_params must be dict[str, list[str]]."""
        record = {
            "schema_version": "1.0",
            "request_id": "a" * 64,
            "captured_at": "2024-01-01T00:00:00+00:00",
            "method": "GET",
            "url": "https://example.com/",
            "query_params": {"q": "not-a-list"},  # should be a list
            "request_headers": {},
            "request_body_ref": None,
            "request_body_sha256": None,
            "response_status": 200,
            "response_headers": {},
            "response_body_ref": None,
            "response_body_sha256": None,
        }
        errors = list(self.validator.iter_errors(record))
        assert len(errors) >= 1

    def test_response_status_null_allowed(self):
        record = {
            "schema_version": "1.0",
            "request_id": "a" * 64,
            "captured_at": "2024-01-01T00:00:00+00:00",
            "method": "GET",
            "url": "https://example.com/",
            "query_params": {},
            "request_headers": {},
            "request_body_ref": None,
            "request_body_sha256": None,
            "response_status": None,
            "response_headers": {},
            "response_body_ref": None,
            "response_body_sha256": None,
        }
        errors = list(self.validator.iter_errors(record))
        assert not errors


# ---------------------------------------------------------------------------
# Tests: page.on("response") integration
# ---------------------------------------------------------------------------


class TestPageIntegration:
    @pytest.mark.asyncio
    async def test_attach_to_page_fires_on_response(self, tmp_path: Path):
        """Hook via attach_to_page -> response event triggers capture."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        page = _make_mock_page()

        await capture.attach_to_page(page, "example.com")

        r = _make_mock_response(url="https://example.com/page")
        await _fire_response(page, r)

        assert capture._record_count == 1

    @pytest.mark.asyncio
    async def test_out_of_scope_via_page_hook(self, tmp_path: Path):
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        page = _make_mock_page()

        await capture.attach_to_page(page, "example.com")

        r = _make_mock_response(url="https://evil.com/page")
        await _fire_response(page, r)

        assert capture._record_count == 0


# ---------------------------------------------------------------------------
# Tests: CLI wiring
# ---------------------------------------------------------------------------


class TestCLITrafficOptions:
    def test_traffic_dir_option_parses(self):
        from click.testing import CliRunner
        from ai_browser.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["crawl", "example.com", "--authorized", "--traffic-dir", "/tmp/traffic"],
        )
        assert "no such option" not in result.output.lower()

    def test_no_traffic_capture_option_parses(self):
        from click.testing import CliRunner
        from ai_browser.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["crawl", "example.com", "--authorized", "--no-traffic-capture"],
        )
        assert "no such option" not in result.output.lower()

    def test_default_omits_traffic_dir(self):
        from click.testing import CliRunner
        from ai_browser.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["crawl", "example.com", "--authorized"],
        )
        assert "no such option" not in result.output.lower()


# ---------------------------------------------------------------------------
# Tests for guarded request.headers + capture observability
# ---------------------------------------------------------------------------


class TestRequestHeadersGuard:
    """Verify that a crashing request.headers doesn't silently drop the record."""

    @pytest.mark.asyncio
    async def test_request_headers_raises_still_writes_record(self, tmp_path: Path):
        """When request.headers raises, the record is still written with
        empty request_headers — this is the direct regression test for the
        unguarded call that was silently dropping entire traffic records."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        # Build a response whose request.headers raises on access
        req = MagicMock()
        req.method = "GET"
        req.url = "https://example.com/page"
        type(req).headers = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("stale request"))
        )
        req.post_data_buffer = None
        req.post_data = None

        mock_response = MagicMock()
        mock_response.request = req
        mock_response.status = 200
        mock_response.headers = {"content-type": "text/html"}
        async def _body() -> bytes:
            return b"<html>ok</html>"
        mock_response.body = _body

        await capture._capture(mock_response)

        assert capture._record_count == 1, (
            "Record MUST be written even when request.headers raises"
        )
        lines = (tmp_path / "index.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["request_headers"] == {}, (
            "request_headers should fall back to {} on error, "
            f"got {record['request_headers']}"
        )

    @pytest.mark.asyncio
    async def test_unexpected_failure_logs_warning(self, tmp_path: Path, caplog):
        """When some other unexpected step raises inside _capture, a WARNING
        log fires with the URL and exception — record is NOT written."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(url="https://example.com/page")

        # Make open() raise OSError on write (simulates disk full / perm error)
        with patch("builtins.open", side_effect=OSError("disk full")):
            with caplog.at_level(logging.WARNING):
                await capture._capture(r)

        assert capture._record_count == 0, "Record should NOT be written on failure"

        warnings = [
            r for r in caplog.records
            if "TrafficCapture: failed to capture" in r.message
        ]
        assert len(warnings) >= 1, (
            "WARNING must fire when _capture fails, got no matching records"
        )
        assert "example.com/page" in warnings[0].message
        assert "disk full" in warnings[0].message


class TestCaptureObservability:
    """Verify that every capture decision produces a log line."""

    @pytest.mark.asyncio
    async def test_out_of_scope_logs_debug(self, tmp_path: Path, caplog):
        """Out-of-scope response → DEBUG 'out of scope, skipping' line."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(url="https://other.com/page")
        with caplog.at_level(logging.DEBUG):
            await capture._capture(r)

        assert capture._record_count == 0
        skip_logs = [
            r for r in caplog.records
            if "out of scope, skipping" in r.message
        ]
        assert len(skip_logs) >= 1, "Should log 'out of scope, skipping'"

    @pytest.mark.asyncio
    async def test_successful_capture_logs_debug(self, tmp_path: Path, caplog):
        """Successful capture → DEBUG 'recorded ...' line with method/url/status."""
        capture = TrafficCapture(tmp_path)
        capture.ensure_dirs()
        capture._scope_pattern = "example.com"

        r = _make_mock_response(
            url="https://example.com/api",
            method="POST",
            status=201,
        )
        with caplog.at_level(logging.DEBUG):
            await capture._capture(r)

        assert capture._record_count == 1
        recorded_logs = [
            r for r in caplog.records
            if "TrafficCapture: recorded" in r.message
        ]
        assert len(recorded_logs) >= 1
        msg = recorded_logs[0].message
        assert "POST" in msg
        assert "example.com/api" in msg
        assert "status=201" in msg
