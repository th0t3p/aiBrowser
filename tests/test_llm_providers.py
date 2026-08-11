"""Tests for multi-provider LLM support (Fix #2 HTTP errors + Fix #3 coverage)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ai_browser.agent_explorer import AgentExplorer, ExplorerConfig


class TestLLMProviderRequests:
    """Test that _ask_llm makes correct HTTP requests per provider."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
            **kwargs,
        )

    @staticmethod
    def _mock_response(status=200, json_body=None):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.json.return_value = json_body or {}
        resp.raise_for_status = MagicMock()
        if status >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "error", request=MagicMock(), response=resp
            )
        resp.text = json.dumps(json_body) if json_body else ""
        return resp

    def test_anthropic_uses_correct_url_and_auth(self):
        """Anthropic provider uses x-api-key header and correct URL."""
        explorer = AgentExplorer(self._make_config(llm_provider="anthropic"))
        assert explorer.config.llm_provider == "anthropic"

    def test_openai_uses_bearer_auth(self):
        """OpenAI provider uses Authorization: Bearer header."""
        explorer = AgentExplorer(self._make_config(llm_provider="openai"))
        assert explorer.config.llm_provider == "openai"

    def test_deepseek_uses_bearer_auth(self):
        """DeepSeek uses OpenAI-compatible format with Bearer auth."""
        explorer = AgentExplorer(self._make_config(llm_provider="deepseek"))
        assert explorer.config.llm_provider == "deepseek"

    def test_custom_base_url_override(self):
        """Custom llm_base_url overrides provider default."""
        explorer = AgentExplorer(
            self._make_config(
                llm_provider="openai",
                llm_base_url="https://custom-proxy.example.com/v1",
            )
        )
        assert explorer.config.llm_base_url == "https://custom-proxy.example.com/v1"


class TestLLMResponseParsing:
    """Test that _parse_llm_response normalizes across providers —
    including the new structured-output (tool-use / tool-calls) paths."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
            **kwargs,
        )

    # -- Anthropic tool-use (native structured output) --------------------

    def test_parse_anthropic_tool_use(self):
        """Anthropic tool_use block is extracted directly — no text parsing."""
        explorer = AgentExplorer(self._make_config(llm_provider="anthropic"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "content": [{
                "type": "tool_use",
                "id": "toolu_01",
                "name": "take_action",
                "input": {"action": "click", "target": "Login", "reasoning": "test"},
            }],
        }
        result = explorer._parse_llm_response("anthropic", resp)
        assert result == {"action": "click", "target": "Login", "reasoning": "test"}

    def test_parse_anthropic_text_fallback(self):
        """If tool_use is absent (unusual), Anthropic falls back to text extraction."""
        explorer = AgentExplorer(self._make_config(llm_provider="anthropic"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "content": [{
                "type": "text",
                "text": '{"action": "click", "target": "Fallback"}',
            }],
        }
        result = explorer._parse_llm_response("anthropic", resp)
        assert result == {"action": "click", "target": "Fallback"}

    # -- OpenAI tool-calls (native structured output) ---------------------

    def test_parse_openai_tool_calls(self):
        """OpenAI tool_calls are parsed directly — no text extraction."""
        explorer = AgentExplorer(self._make_config(llm_provider="openai"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "call_01",
                        "type": "function",
                        "function": {
                            "name": "take_action",
                            "arguments": '{"action": "scroll", "target": "", "reasoning": "see more"}',
                        },
                    }],
                },
            }],
        }
        result = explorer._parse_llm_response("openai", resp)
        assert result == {"action": "scroll", "target": "", "reasoning": "see more"}

    def test_parse_openai_tool_calls_invalid_json(self):
        """If tool_calls arguments are not valid JSON, parsing returns None."""
        explorer = AgentExplorer(self._make_config(llm_provider="openai"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "call_01",
                        "type": "function",
                        "function": {
                            "name": "take_action",
                            "arguments": "not valid json {{{",
                        },
                    }],
                },
            }],
        }
        result = explorer._parse_llm_response("openai", resp)
        assert result is None

    # -- DeepSeek: text-extraction fallback -------------------------------

    def test_parse_deepseek_text_extraction(self):
        """DeepSeek stays on text-extraction path (tool-calling not confirmed)."""
        explorer = AgentExplorer(self._make_config(llm_provider="deepseek"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"action": "fill", "target": "email", "value": "x@y.com", "reasoning": "test"}',
                },
            }],
        }
        result = explorer._parse_llm_response("deepseek", resp)
        assert result == {
            "action": "fill", "target": "email", "value": "x@y.com", "reasoning": "test",
        }

    def test_deepseek_invalid_json_returns_none(self):
        """Malformed DeepSeek text content returns None."""
        explorer = AgentExplorer(self._make_config(llm_provider="deepseek"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [{"message": {"content": "not valid json at all"}}],
        }
        result = explorer._parse_llm_response("deepseek", resp)
        assert result is None

    def test_all_providers_produce_same_normalized_dict(self):
        """Regardless of provider and response shape, the output dict has
        the same structure."""
        explorer = AgentExplorer(self._make_config())
        action = {"action": "click", "target": "About Us", "reasoning": "explore"}

        # Anthropic — tool_use
        resp_a = MagicMock(spec=httpx.Response)
        resp_a.json.return_value = {
            "content": [{"type": "tool_use", "name": "take_action", "input": action}],
        }
        result_a = explorer._parse_llm_response("anthropic", resp_a)

        # OpenAI — tool_calls
        resp_o = MagicMock(spec=httpx.Response)
        resp_o.json.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {"name": "take_action", "arguments": json.dumps(action)},
                    }],
                },
            }],
        }
        result_o = explorer._parse_llm_response("openai", resp_o)

        # DeepSeek — text extraction
        resp_d = MagicMock(spec=httpx.Response)
        resp_d.json.return_value = {
            "choices": [{"message": {"content": json.dumps(action)}}],
        }
        result_d = explorer._parse_llm_response("deepseek", resp_d)

        assert result_a == result_o == result_d
        assert result_a["action"] == "click"
        assert result_a["target"] == "About Us"


class TestHTTPErrorHandling:
    """Test that HTTP errors produce clear log messages (Fix #2)."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_401_is_http_error_not_parse_error(self):
        """A 401 response logs as HTTP error, not a parse failure."""
        explorer = AgentExplorer(self._make_config(llm_provider="openai"))

        # Simulate _ask_llm receiving a 401 response
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.text = '{"error": "invalid api key"}'
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized", request=MagicMock(), response=resp
        )

        # Mock the internal call to skip actual HTTP
        with patch.object(explorer, "_call_openai_compatible", AsyncMock(return_value=resp)):
            result = await explorer._ask_llm([{"role": "user", "content": "test"}])
            # Should return None (error handled, not raised)
            assert result is None

    @pytest.mark.asyncio
    async def test_429_rate_limit_handled(self):
        """A 429 rate limit returns None, not an unhandled exception."""
        explorer = AgentExplorer(self._make_config(llm_provider="anthropic"))

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        resp.text = '{"error": "rate limited"}'
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Too Many Requests", request=MagicMock(), response=resp
        )

        with patch.object(explorer, "_call_anthropic", AsyncMock(return_value=resp)):
            result = await explorer._ask_llm([{"role": "user", "content": "test"}])
            assert result is None


class TestBackwardCompat:
    """Test deprecated anthropic_* fields still work."""

    def test_anthropic_api_key_migrates(self):
        """anthropic_api_key populates llm_api_key via model_validator."""
        config = ExplorerConfig(
            authorized_hostname="example.com",
            anthropic_api_key="sk-ant-old",
        )
        assert config.llm_api_key == "sk-ant-old"

    def test_anthropic_model_migrates(self):
        """anthropic_model populates llm_model via model_validator."""
        config = ExplorerConfig(
            authorized_hostname="example.com",
            anthropic_api_key="sk-ant-old",
            anthropic_model="claude-3-opus-20240229",
        )
        assert config.llm_model == "claude-3-opus-20240229"

    def test_ask_claude_deprecated_exists(self):
        """_ask_claude is a deprecated shim that still exists and is callable."""
        config = ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
        )
        explorer = AgentExplorer(config)
        assert callable(explorer._ask_claude)
        # Verify it delegates to _ask_llm (same object, not a copy)
        import inspect
        source = inspect.getsource(explorer._ask_claude)
        assert "_ask_llm" in source


# ---------------------------------------------------------------------------
# Issue 1: nested JSON parsing (Fix #3 — regex → raw_decode)
# ---------------------------------------------------------------------------


class TestNestedJSONParsing:
    r"""Test that _extract_json correctly parses nested JSON objects
    where the old regex ``\{[^}]+\}`` would fail."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
            **kwargs,
        )

    def test_flat_json_still_works(self):
        """Simple flat JSON (no nesting) is still parsed correctly."""
        from ai_browser.agent_explorer.explorer import _extract_json

        text = '{"action": "click", "target": "Login"}'
        result = _extract_json(text)
        assert result == {"action": "click", "target": "Login"}

    def test_nested_json_object_parses_correctly(self):
        """A JSON with a nested object parses fully — the old regex
        would have truncated at the first inner ``}``."""
        from ai_browser.agent_explorer.explorer import _extract_json

        text = (
            '{"action": "fill", "target": "form", '
            '"value": {"email": "x@y.com", "password": "secret"}}'
        )
        result = _extract_json(text)
        assert result == {
            "action": "fill",
            "target": "form",
            "value": {"email": "x@y.com", "password": "secret"},
        }

    def test_nested_with_markdown_wrapper(self):
        """JSON embedded in markdown code fences with nested objects."""
        from ai_browser.agent_explorer.explorer import _extract_json

        text = '```json\n{"outer": {"inner": {"key": "val"}}}\n```'
        result = _extract_json(text)
        assert result == {"outer": {"inner": {"key": "val"}}}

    def test_reasoning_field_with_braces(self):
        """The ``reasoning`` field itself may contain curly-brace-like
        text in natural language — must not confuse the parser."""
        from ai_browser.agent_explorer.explorer import _extract_json

        text = (
            '{"action": "click", "target": "Login", '
            '"reasoning": "Found the {Login} button on the page"}'
        )
        result = _extract_json(text)
        assert result == {
            "action": "click",
            "target": "Login",
            "reasoning": "Found the {Login} button on the page",
        }

    def test_no_json_returns_none(self):
        """Text with no ``{`` returns None."""
        from ai_browser.agent_explorer.explorer import _extract_json

        assert _extract_json("plain text, no json at all") is None

    def test_multiple_objects_extracts_first(self):
        """When multiple JSON objects appear, only the first is returned."""
        from ai_browser.agent_explorer.explorer import _extract_json

        text = '{"first": 1} garbage {"second": 2}'
        result = _extract_json(text)
        assert result == {"first": 1}

    def test_end_to_end_parse_nested_via_explorer(self):
        """Full _parse_llm_response path handles nested JSON via DeepSeek
        text-extraction fallback (the only provider still on that path)."""
        from unittest.mock import MagicMock

        import httpx

        explorer = AgentExplorer(self._make_config(llm_provider="deepseek"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action": "fill", "target": "signup", '
                            '"value": {"name": "Test", "email": "a@b.com"}}'
                        )
                    }
                }
            ]
        }
        result = explorer._parse_llm_response("deepseek", resp)
        assert result == {
            "action": "fill",
            "target": "signup",
            "value": {"name": "Test", "email": "a@b.com"},
        }


# ---------------------------------------------------------------------------
# Issue 2: diagnostic logging on parse failure
# ---------------------------------------------------------------------------


class TestParseFailureLogging:
    """Test that parse failures log full diagnostic information
    (content length, content preview, and full raw response body),
    not just a possibly-empty content slice."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
            **kwargs,
        )

    def test_empty_content_logs_full_raw_response(self, caplog):
        """When the extracted content is empty (thinking-mode exhausted
        max_tokens), the warning log includes the full raw response body
        so the empty-content case is distinguishable from
        content-present-but-unparseable."""
        import logging
        from unittest.mock import MagicMock

        import httpx

        explorer = AgentExplorer(self._make_config())
        resp = MagicMock(spec=httpx.Response)
        raw_body = {
            "choices": [
                {
                    "message": {"content": ""},
                    "finish_reason": "length",
                }
            ]
        }
        resp.json.return_value = raw_body

        with caplog.at_level(logging.WARNING):
            result = explorer._parse_llm_response("openai", resp)

        assert result is None
        # The warning should contain the full raw body dict, not just content=""
        warning_text = caplog.text
        assert "Could not parse JSON" in warning_text
        assert "Extracted content length=0" in warning_text
        assert "Full raw response:" in warning_text
        # Verify the raw body dict appears in the log (distinguishes empty
        # content from unparseable content)
        assert "'finish_reason': 'length'" in warning_text

    def test_unparseable_content_shows_content_and_raw_body(self, caplog):
        """When content is present but unparseable JSON, the log shows
        both the content preview AND the full raw body."""
        import logging
        from unittest.mock import MagicMock

        import httpx

        explorer = AgentExplorer(self._make_config())
        resp = MagicMock(spec=httpx.Response)
        raw_body = {
            "choices": [
                {"message": {"content": "not valid {{{ json"}}
            ]
        }
        resp.json.return_value = raw_body

        with caplog.at_level(logging.WARNING):
            result = explorer._parse_llm_response("openai", resp)

        assert result is None
        warning_text = caplog.text
        assert "Could not parse JSON" in warning_text
        assert "Extracted content length=" in warning_text
        assert "Full raw response:" in warning_text
        assert "not valid {{{ json" in warning_text


# ---------------------------------------------------------------------------
# Issue 3: configurable max_tokens
# ---------------------------------------------------------------------------


class TestMaxTokensConfig:
    """Test that the configurable llm_max_tokens value is actually
    used in the API request body, not a hardcoded 512."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
            **kwargs,
        )

    def test_default_max_tokens_is_none(self):
        """The default llm_max_tokens is None — the field is genuinely optional."""
        config = ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
        )
        assert config.llm_max_tokens is None

    def test_custom_max_tokens_in_config(self):
        """Explicit llm_max_tokens value is stored in config."""
        config = self._make_config(llm_max_tokens=8192)
        assert config.llm_max_tokens == 8192

    @pytest.mark.asyncio
    async def test_anthropic_uses_configured_max_tokens(self):
        """_call_anthropic includes llm_max_tokens in the request body."""
        explorer = AgentExplorer(self._make_config(llm_max_tokens=3000))

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        # Capture the JSON body sent in the POST
        with patch.object(
            explorer._client, "post", AsyncMock(return_value=mock_resp)
        ) as mock_post:
            await explorer._call_anthropic(
                "key", "claude-model", None, [{"role": "user", "content": "test message"}]
            )
            call_body = mock_post.call_args[1]["json"]
            assert call_body["max_tokens"] == 3000

    @pytest.mark.asyncio
    async def test_openai_compatible_uses_configured_max_tokens(self):
        """_call_openai_compatible includes llm_max_tokens in the body."""
        explorer = AgentExplorer(self._make_config(llm_max_tokens=4096))

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(
            explorer._client, "post", AsyncMock(return_value=mock_resp)
        ) as mock_post:
            await explorer._call_openai_compatible(
                "openai", "key", "gpt-4o", None, [{"role": "user", "content": "test"}]
            )
            call_body = mock_post.call_args[1]["json"]
            assert call_body["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_deepseek_uses_configured_max_tokens(self):
        """DeepSeek (via _call_openai_compatible) uses the configured max_tokens."""
        explorer = AgentExplorer(
            self._make_config(llm_provider="deepseek", llm_max_tokens=2048)
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(
            explorer._client, "post", AsyncMock(return_value=mock_resp)
        ) as mock_post:
            await explorer._call_openai_compatible(
                "deepseek", "key", "deepseek-chat", None, [{"role": "user", "content": "test"}]
            )
            call_body = mock_post.call_args[1]["json"]
            assert call_body["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_anthropic_falls_back_when_none(self):
        """When llm_max_tokens is None, _call_anthropic includes the
        internal fallback of 4096 in the request body."""
        explorer = AgentExplorer(
            self._make_config(llm_max_tokens=None)
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(
            explorer._client, "post", AsyncMock(return_value=mock_resp)
        ) as mock_post:
            await explorer._call_anthropic(
                "key", "claude-model", None, [{"role": "user", "content": "test message"}]
            )
            call_body = mock_post.call_args[1]["json"]
            assert call_body["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_openai_falls_back_to_default_when_none(self):
        """When llm_max_tokens is None, _call_openai_compatible uses the
        shared DEFAULT_MAX_TOKENS (4096) instead of omitting."""
        explorer = AgentExplorer(
            self._make_config(llm_max_tokens=None)
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(
            explorer._client, "post", AsyncMock(return_value=mock_resp)
        ) as mock_post:
            await explorer._call_openai_compatible(
                "openai", "key", "gpt-4o", None, [{"role": "user", "content": "test"}]
            )
            call_body = mock_post.call_args[1]["json"]
            from ai_browser._llm_client import DEFAULT_MAX_TOKENS
            assert call_body["max_tokens"] == DEFAULT_MAX_TOKENS

    @pytest.mark.asyncio
    async def test_explicit_value_still_works_for_openai(self):
        """With an explicit llm_max_tokens, _call_openai_compatible
        includes exactly that value."""
        explorer = AgentExplorer(
            self._make_config(llm_max_tokens=100)
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(
            explorer._client, "post", AsyncMock(return_value=mock_resp)
        ) as mock_post:
            await explorer._call_openai_compatible(
                "openai", "key", "gpt-4o", None, [{"role": "user", "content": "test"}]
            )
            call_body = mock_post.call_args[1]["json"]
            assert call_body["max_tokens"] == 100


class TestEmptyLLMResponseReturnsNone:
    @pytest.mark.asyncio
    async def test_anthropic_empty_text_returns_none(self):
        from ai_browser._llm_client import call_llm
        from unittest.mock import AsyncMock, MagicMock, patch
        import httpx

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": ""}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("ai_browser._llm_client._call_anthropic", AsyncMock(return_value=mock_resp)):
            result = await call_llm(
                "anthropic", "key", "model", [{"role": "user", "content": "test"}]
            )
            assert result is None, f"Expected None, got {result!r}"

    @pytest.mark.asyncio
    async def test_openai_empty_content_returns_none(self):
        from ai_browser._llm_client import call_llm
        from unittest.mock import AsyncMock, MagicMock, patch
        import httpx

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": ""}}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("ai_browser._llm_client._call_openai_compatible", AsyncMock(return_value=mock_resp)):
            result = await call_llm("openai", "key", "model", [{"role": "user", "content": "test"}])
            assert result is None, f"Expected None, got {result!r}"

    @pytest.mark.asyncio
    async def test_openai_missing_content_returns_none(self):
        from ai_browser._llm_client import call_llm
        from unittest.mock import AsyncMock, MagicMock, patch
        import httpx

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {}}],  # no content key at all
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("ai_browser._llm_client._call_openai_compatible", AsyncMock(return_value=mock_resp)):
            result = await call_llm("openai", "key", "model", [{"role": "user", "content": "test"}])
            assert result is None, f"Expected None, got {result!r}"
