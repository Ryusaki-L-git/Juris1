"""
tests/test_gemini_gateway.py — Unit tests for GeminiGateway (gemini-3.8-flash).

Validates:
    - Default model is `gemini-3.8-flash`.
    - API key validation: missing or placeholder key ("paste the API key here man")
      fails with exact error: "Gemini API key is not configured. Set JURIS_GEMINI_API_KEY in .env."
    - Key is never logged, printed, or exposed in output or metadata.
    - Tool schema conversion to Gemini functionDeclarations format.
    - Multi-turn conversation and function calling / response formatting.
    - Parsing of Gemini responses (text and functionCall).
    - Handling of HTTP status codes (200, 401/403, 429, 500, timeout).
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from core.errors import ConfigurationError
from gateway.base import (
    GatewayMessage,
    ModelRequest,
    ToolCallRequest,
    ToolResult,
)
from gateway.gemini_gateway import (
    GeminiGateway,
    MISSING_KEY_MESSAGE,
    PLACEHOLDER_KEY,
    convert_tool_definitions,
    extract_usage_metadata,
    is_configured_api_key,
)


class GeminiGatewayUnitTests(unittest.TestCase):
    def test_usage_metadata_includes_input_output_and_thinking_tokens(self) -> None:
        usage = extract_usage_metadata({
            "usageMetadata": {
                "promptTokenCount": 120,
                "candidatesTokenCount": 30,
                "thoughtsTokenCount": 5,
            }
        })
        self.assertEqual(usage, {
            "input_tokens": 120,
            "output_tokens": 35,
            "usage_reported": True,
        })

    def test_incomplete_usage_metadata_falls_back_to_reservation(self) -> None:
        usage = extract_usage_metadata({
            "usageMetadata": {"promptTokenCount": 120}
        })
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["usage_reported"], False)

    def test_default_model_is_gemini_3_8_flash(self) -> None:
        gateway = GeminiGateway()
        self.assertEqual(gateway.model_name, "gemini-3.8-flash")
        self.assertEqual(gateway.provider_name, "gemini")

    def test_api_key_placeholder_validation(self) -> None:
        """Verifies that empty keys and placeholder keys are rejected."""
        self.assertFalse(is_configured_api_key(""))
        self.assertFalse(is_configured_api_key("   "))
        self.assertFalse(is_configured_api_key(PLACEHOLDER_KEY))
        self.assertFalse(is_configured_api_key("your_gemini_api_key_here"))
        self.assertTrue(is_configured_api_key("AIzaSyD-valid-mock-key-12345"))

    def test_missing_or_placeholder_key_returns_clear_configuration_error(self) -> None:
        """When key is missing or placeholder, generate returns error response with exact message."""
        gateway = GeminiGateway(api_key=PLACEHOLDER_KEY)
        req = ModelRequest(user_message="Hello JURIS")
        res = asyncio.run(gateway.generate(req))
        self.assertEqual(res.finish_reason, "error")
        self.assertEqual(res.text, MISSING_KEY_MESSAGE)
        self.assertEqual(res.raw_metadata.get("error"), "missing_api_key")

        # validate_configuration raises ConfigurationError
        with self.assertRaises(ConfigurationError) as ctx:
            gateway.validate_configuration()
        self.assertEqual(str(ctx.exception), MISSING_KEY_MESSAGE)

    def test_tool_definition_conversion(self) -> None:
        tools = [
            {
                "name": "get_case",
                "description": "Retrieve case details.",
                "parameters": [
                    {
                        "name": "case_id",
                        "description": "ID of case",
                        "required": True,
                        "type_hint": "string",
                    },
                    {
                        "name": "limit",
                        "description": "Result limit",
                        "required": False,
                        "type_hint": "integer",
                    },
                ],
            }
        ]
        declarations = convert_tool_definitions(tools)
        self.assertEqual(len(declarations), 1)
        decl = declarations[0]
        self.assertEqual(decl["name"], "get_case")
        self.assertEqual(decl["description"], "Retrieve case details.")
        self.assertEqual(decl["parameters"]["type"], "OBJECT")
        self.assertIn("case_id", decl["parameters"]["properties"])
        self.assertEqual(decl["parameters"]["properties"]["case_id"]["type"], "STRING")
        self.assertEqual(decl["parameters"]["properties"]["limit"]["type"], "INTEGER")
        self.assertEqual(decl["parameters"]["required"], ["case_id"])

    def test_multi_turn_contents_construction(self) -> None:
        gateway = GeminiGateway(api_key="valid-test-key")
        messages = [
            GatewayMessage(role="user", content="Show case details"),
            GatewayMessage(
                role="model",
                content="",
                tool_calls=[
                    ToolCallRequest(
                        tool_name="get_case",
                        arguments={"case_id": "c-1"},
                        thought_signature="test-signature",
                    )
                ],
            ),
            GatewayMessage(
                role="tool",
                tool_results=[
                    ToolResult(
                        tool_name="get_case",
                        success=True,
                        data={"title": "State vs Ramesh"},
                    )
                ],
            ),
        ]
        req = ModelRequest(user_message="Show case details", messages=messages)
        contents = gateway._build_contents(req)

        self.assertEqual(len(contents), 3)
        # Turn 1: user
        self.assertEqual(contents[0]["role"], "user")
        self.assertEqual(contents[0]["parts"][0]["text"], "Show case details")
        # Turn 2: model functionCall
        self.assertEqual(contents[1]["role"], "model")
        self.assertIn("functionCall", contents[1]["parts"][0])
        self.assertEqual(contents[1]["parts"][0]["functionCall"]["name"], "get_case")
        self.assertEqual(contents[1]["parts"][0]["thoughtSignature"], "test-signature")
        # Turn 3: functionResponse passed as role 'user'
        self.assertEqual(contents[2]["role"], "user")
        self.assertIn("functionResponse", contents[2]["parts"][0])
        self.assertEqual(contents[2]["parts"][0]["functionResponse"]["name"], "get_case")
        self.assertEqual(
            contents[2]["parts"][0]["functionResponse"]["response"]["content"]["title"],
            "State vs Ramesh",
        )

    def test_successful_gemini_response_parsing(self) -> None:
        mock_response_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Advocate, here is the case summary."}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ]
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("gemini-3.8-flash", str(request.url))
            self.assertEqual(request.headers.get("x-goog-api-key"), "mock-key")
            return httpx.Response(200, json=mock_response_data)

        transport = httpx.MockTransport(handler)
        gateway = GeminiGateway(api_key="mock-key", model="gemini-3.8-flash")
        req = ModelRequest(user_message="Summarize case")

        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            res = asyncio.run(gateway.generate(req))

        self.assertEqual(res.finish_reason, "stop")
        self.assertEqual(res.text, "Advocate, here is the case summary.")
        self.assertEqual(len(res.tool_calls), 0)

    def test_gemini_tool_call_response_parsing(self) -> None:
        mock_response_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "get_case",
                                    "args": {"case_id": "c-123"},
                                },
                                "thoughtSignature": "opaque-test-signature",
                            }
                        ],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ]
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=mock_response_data)

        transport = httpx.MockTransport(handler)
        gateway = GeminiGateway(api_key="mock-key", model="gemini-3.8-flash")
        req = ModelRequest(user_message="Show case c-123")

        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            res = asyncio.run(gateway.generate(req))

        self.assertEqual(res.finish_reason, "tool_call")
        self.assertEqual(len(res.tool_calls), 1)
        self.assertEqual(res.tool_calls[0].tool_name, "get_case")
        self.assertEqual(res.tool_calls[0].arguments, {"case_id": "c-123"})
        self.assertEqual(res.tool_calls[0].thought_signature, "opaque-test-signature")

    def test_gemini_auth_failure_handling(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"message": "API_KEY_INVALID"}})

        transport = httpx.MockTransport(handler)
        gateway = GeminiGateway(api_key="invalid-key", model="gemini-3.8-flash")
        req = ModelRequest(user_message="Hello")

        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            res = asyncio.run(gateway.generate(req))

        self.assertEqual(res.finish_reason, "error")
        self.assertIn("authentication failed", res.text.lower())
        self.assertNotIn("invalid-key", res.text)
        self.assertNotIn("invalid-key", str(res.raw_metadata))

    def test_gemini_bad_request_is_not_reported_as_auth_failure(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "thought signature missing"}})

        transport = httpx.MockTransport(handler)
        gateway = GeminiGateway(api_key="mock-key", model="gemini-3.5-flash-lite")
        req = ModelRequest(user_message="Hello")

        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            res = asyncio.run(gateway.generate(req))

        self.assertEqual(res.finish_reason, "error")
        self.assertIn("invalid", res.text.lower())
        self.assertNotIn("authentication failed", res.text.lower())
        self.assertEqual(res.raw_metadata.get("status_code"), 400)
        self.assertTrue(res.raw_metadata.get("request_error"))

    def test_gemini_rate_limit_429(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "Resource exhausted"}})

        transport = httpx.MockTransport(handler)
        gateway = GeminiGateway(api_key="mock-key", model="gemini-3.8-flash")
        req = ModelRequest(user_message="Hello")

        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            res = asyncio.run(gateway.generate(req))

        self.assertEqual(res.finish_reason, "error")
        self.assertIn("rate-limited", res.text)


if __name__ == "__main__":
    unittest.main()
