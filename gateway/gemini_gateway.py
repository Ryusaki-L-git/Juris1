"""
gateway/gemini_gateway.py — Gemini Model Gateway for JURIS.

Communicates with Google Gemini models using the Gemini REST API.
Configured for `gemini-3.8-flash`.

SECURITY & RESILIENCE:
    - API keys are retrieved from environment/settings only. Never logged, printed, or exposed.
    - Validates against missing or placeholder API keys.
    - Uses official `x-goog-api-key` header to prevent leaking key into URL query logs.
    - Translates JURIS ToolDefinitions into Gemini functionDeclarations format.
    - Normalizes multi-turn conversations and function calling.
    - Handles timeouts, rate limits (429), auth failures (401/403), and server errors cleanly.
    - Model never accesses Firestore or database directly; only requests registered tools.
"""

from __future__ import annotations

import logging
from typing import Any
import hashlib
import json

import httpx

from core.config import settings
from core.errors import ConfigurationError
from gateway.base import (
    GatewayMessage,
    JurisModelGateway,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)

logger = logging.getLogger("juris.gateway.gemini")

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
PLACEHOLDER_KEY = "paste the API key here man"
MISSING_KEY_MESSAGE = "Gemini API key is not configured. Set JURIS_GEMINI_API_KEY in .env."


def is_configured_api_key(key: str | None) -> bool:
    """Return True only if key is non-empty, non-whitespace, and not a placeholder."""
    if not key or not key.strip():
        return False
    clean = key.strip()
    if clean == PLACEHOLDER_KEY:
        return False
    if clean.lower() in ("your_gemini_api_key_here", "none", "null", "placeholder"):
        return False
    return True


def convert_tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert JURIS tool definitions into Gemini function declarations."""
    declarations = []
    type_map = {
        "string": "STRING",
        "integer": "INTEGER",
        "int": "INTEGER",
        "boolean": "BOOLEAN",
        "bool": "BOOLEAN",
        "object": "OBJECT",
        "array": "ARRAY",
        "list": "ARRAY",
    }

    for tool in tools:
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in tool.get("parameters", []):
            name = param.get("name")
            if not name:
                continue
            desc = param.get("description", "")
            type_hint = str(param.get("type_hint", "string")).lower()
            schema_type = type_map.get(type_hint, "STRING")

            properties[name] = {
                "type": schema_type,
                "description": desc,
            }
            if param.get("required", True):
                required.append(name)

        parameters_schema: dict[str, Any] = {
            "type": "OBJECT",
        }
        if properties:
            parameters_schema["properties"] = properties
        if required:
            parameters_schema["required"] = required

        declarations.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": parameters_schema,
        })

    return declarations


class GeminiGateway(JurisModelGateway):
    """
    Production model gateway communicating with Google's Gemini API (gemini-3.8-flash).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def api_key(self) -> str:
        key = self._api_key if self._api_key is not None else settings.effective_gemini_api_key
        return key.strip()


    @property
    def is_key_configured(self) -> bool:
        return is_configured_api_key(self.api_key)

    @property
    def model_name(self) -> str:
        return (self._model or settings.effective_gemini_model or "gemini-3.8-flash").strip()

    @property
    def timeout_seconds(self) -> float:
        return self._timeout if self._timeout is not None else settings.model_timeout

    def validate_configuration(self) -> None:
        """
        Validate that the Gemini gateway is configured correctly.
        Raises ConfigurationError if the API key is missing or is the placeholder.
        """
        if not self.is_key_configured:
            raise ConfigurationError(MISSING_KEY_MESSAGE)

    def _build_contents(self, request: ModelRequest) -> list[dict[str, Any]]:
        """Construct Gemini contents array from turn history or single message."""
        contents: list[dict[str, Any]] = []

        if request.messages:
            for msg in request.messages:
                if msg.role == "user":
                    contents.append({
                        "role": "user",
                        "parts": [{"text": msg.content or " "}],
                    })
                elif msg.role == "model":
                    parts: list[dict[str, Any]] = []
                    for tc in msg.tool_calls:
                        function_call_part: dict[str, Any] = {
                            "functionCall": {
                                "name": tc.tool_name,
                                "args": tc.arguments,
                            }
                        }
                        if tc.thought_signature:
                            function_call_part["thoughtSignature"] = tc.thought_signature
                        parts.append(function_call_part)
                    if msg.content:
                        parts.append({"text": msg.content})
                    if parts:
                        contents.append({"role": "model", "parts": parts})
                elif msg.role == "tool":
                    parts = []
                    for tr in msg.tool_results:
                        response_data = tr.data if tr.success else {"error": tr.error or "Tool execution failed"}
                        parts.append({
                            "functionResponse": {
                                "name": tr.tool_name,
                                "response": {
                                    "name": tr.tool_name,
                                    "content": response_data,
                                },
                            }
                        })
                    if parts:
                        contents.append({"role": "user", "parts": parts})
        else:
            parts = [{"text": request.user_message}]
            if request.tool_results:
                for tr in request.tool_results:
                    response_data = tr.data if tr.success else {"error": tr.error or "Tool execution failed"}
                    parts.append({
                        "functionResponse": {
                            "name": tr.tool_name,
                            "response": {
                                "name": tr.tool_name,
                                "content": response_data,
                            },
                        }
                    })
            contents.append({"role": "user", "parts": parts})

        return contents

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not self.is_key_configured:
            logger.warning("Gemini API key is not configured or is placeholder.")
            return ModelResponse(
                text=MISSING_KEY_MESSAGE,
                tool_calls=[],
                finish_reason="error",
                provider=self.provider_name,
                raw_metadata={"error": "missing_api_key"},
            )

        url = f"{GEMINI_API_BASE_URL}/{self.model_name}:generateContent"

        contents = self._build_contents(request)
        payload: dict[str, Any] = {"contents": contents}

        if request.system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": request.system_prompt}]
            }

        if request.available_tools:
            function_declarations = convert_tool_definitions(request.available_tools)
            if function_declarations:
                payload["tools"] = [{"functionDeclarations": function_declarations}]

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self.api_key,
                    },
                )

            if response.status_code in (401, 403):
                logger.warning("Gemini authentication/authorization failure (HTTP %d).", response.status_code)
                return ModelResponse(
                    text=(
                        "Gemini API authentication failed or access was denied. "
                        "Verify that JURIS_GEMINI_API_KEY is valid and permitted to use the configured model."
                    ),
                    finish_reason="error",
                    provider=self.provider_name,
                    raw_metadata={"status_code": response.status_code, "auth_error": True},
                )

            if response.status_code == 400:
                # Keep provider details in server logs for diagnosis, but do not expose
                # raw provider responses to the client.
                logger.warning(
                    "Gemini request rejected: HTTP 400; provider_error=%s",
                    response.text[:1000],
                )
                return ModelResponse(
                    text="Gemini rejected the request as invalid. Check the server logs for diagnostic details.",
                    finish_reason="error",
                    provider=self.provider_name,
                    raw_metadata={"status_code": 400, "request_error": True},
                )

            if response.status_code == 429:
                logger.warning("Gemini API rate limit encountered.")
                return ModelResponse(
                    text="JURIS AI is temporarily rate-limited by Gemini API. Please retry shortly.",
                    finish_reason="error",
                    provider=self.provider_name,
                    raw_metadata={"status_code": 429},
                )

            if response.status_code >= 500:
                logger.warning("Gemini API server error %d.", response.status_code)
                return ModelResponse(
                    text="The external Gemini AI service is currently unavailable. Please try again later.",
                    finish_reason="error",
                    provider=self.provider_name,
                    raw_metadata={"status_code": response.status_code},
                )

            if response.status_code != 200:
                logger.warning("Gemini API error %d.", response.status_code)
                return ModelResponse(
                    text="The AI provider returned an unexpected error.",
                    finish_reason="error",
                    provider=self.provider_name,
                    raw_metadata={"status_code": response.status_code},
                )

            data = response.json()
            candidates = data.get("candidates", [])
            if not candidates:
                logger.warning("Gemini response contained no candidates.")
                return ModelResponse(
                    text="The model did not generate a response. Please refine your query.",
                    finish_reason="error",
                    provider=self.provider_name,
                )

            candidate = candidates[0]
            content = candidate.get("content", {})
            parts = content.get("parts", [])

            text_chunks: list[str] = []
            tool_calls: list[ToolCallRequest] = []

            for part in parts:
                if "text" in part:
                    text_chunks.append(part["text"])
                if "functionCall" in part:
                    fc = part["functionCall"]
                    name = fc.get("name", "")
                    args = fc.get("args") or {}
                    if name:
                        canonical = json.dumps({"tool": name, "arguments": args}, sort_keys=True, separators=(",", ":"))
                        call_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
                        signature = part.get("thoughtSignature") or fc.get("thoughtSignature")
                        tool_calls.append(
                            ToolCallRequest(
                                tool_name=name,
                                arguments=args,
                                call_id=call_id,
                                thought_signature=signature if isinstance(signature, str) else None,
                            )
                        )

            finish_reason = "tool_call" if tool_calls else "stop"
            return ModelResponse(
                text="".join(text_chunks),
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                provider=self.provider_name,
                raw_metadata={"session_id": request.session_id},
            )

        except httpx.TimeoutException:
            logger.warning("Gemini API timed out after %s seconds.", self.timeout_seconds)
            return ModelResponse(
                text="The AI request timed out. Please try again.",
                finish_reason="error",
                provider=self.provider_name,
                raw_metadata={"error": "timeout"},
            )
        except Exception as e:
            logger.error("Unexpected error calling Gemini API: %s", type(e).__name__)
            return ModelResponse(
                text="An unexpected error occurred while communicating with the AI model.",
                finish_reason="error",
                provider=self.provider_name,
                raw_metadata={"error": type(e).__name__},
            )
