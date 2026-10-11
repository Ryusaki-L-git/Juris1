"""OpenAI-compatible model gateway.

Supports providers that expose the OpenAI chat-completions contract. JURIS
keeps the provider behind the gateway so the orchestrator never imports an
SDK or knows provider-specific details.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from gateway.base import (
    GatewayMessage,
    JurisModelGateway,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
    ToolResult,
)

logger = logging.getLogger("juris.gateway.openai_compatible")


class OpenAICompatibleGateway(JurisModelGateway):
    def __init__(
        self,
        *,
        provider_name: str,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout: float = 60.0,
        require_api_key: bool = True,
    ) -> None:
        self._provider_name = provider_name
        self._api_key = (api_key or "").strip()
        self._model = model.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._require_api_key = require_api_key

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _messages(self, request: ModelRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for message in request.messages:
            if message.role == "tool":
                for result in message.tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": result.tool_name,
                        "content": result.model_dump_json(),
                    })
                continue
            item: dict[str, Any] = {"role": message.role, "content": message.content or ""}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.call_id or call.tool_name,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": __import__("json").dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            messages.append(item)
        if not messages:
            messages.append({"role": "user", "content": request.user_message})
        return messages

    @staticmethod
    def _tools(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tools = []
        for definition in definitions:
            properties: dict[str, Any] = {}
            required: list[str] = []
            for parameter in definition.get("parameters", []):
                name = parameter.get("name")
                if not name:
                    continue
                type_hint = str(parameter.get("type_hint", "string")).lower()
                json_type = {
                    "int": "integer", "integer": "integer", "bool": "boolean",
                    "boolean": "boolean", "object": "object", "array": "array",
                }.get(type_hint, "string")
                properties[name] = {
                    "type": json_type,
                    "description": parameter.get("description", ""),
                }
                if parameter.get("required", True):
                    required.append(name)
            schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                schema["required"] = required
            tools.append({
                "type": "function",
                "function": {
                    "name": definition["name"],
                    "description": definition.get("description", ""),
                    "parameters": schema,
                },
            })
        return tools

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not self._base_url or not self._model:
            return ModelResponse(
                text="The selected AI provider is not configured.",
                finish_reason="error",
                provider=self.provider_name,
                raw_metadata={"error": "provider_not_configured"},
            )
        if self._require_api_key and not self._api_key:
            return ModelResponse(
                text=f"{self.provider_name} API credentials are not configured.",
                finish_reason="error",
                provider=self.provider_name,
                raw_metadata={"error": "missing_api_key"},
            )

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages(request),
        }
        if request.available_tools:
            payload["tools"] = self._tools(request.available_tools)

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            if response.status_code in (401, 403):
                return ModelResponse(
                    text=f"{self.provider_name} authentication failed.",
                    finish_reason="error", provider=self.provider_name,
                    raw_metadata={"status_code": response.status_code},
                )
            if response.status_code == 429:
                return ModelResponse(
                    text=f"{self.provider_name} is temporarily rate-limited. Please retry shortly.",
                    finish_reason="error", provider=self.provider_name,
                    raw_metadata={"status_code": 429},
                )
            if response.status_code >= 500:
                return ModelResponse(
                    text=f"{self.provider_name} is temporarily unavailable.",
                    finish_reason="error", provider=self.provider_name,
                    raw_metadata={"status_code": response.status_code},
                )
            if response.status_code != 200:
                return ModelResponse(
                    text="The selected AI provider returned an unexpected error.",
                    finish_reason="error", provider=self.provider_name,
                    raw_metadata={"status_code": response.status_code},
                )

            body = response.json()
            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls: list[ToolCallRequest] = []
            for raw_call in message.get("tool_calls") or []:
                function = raw_call.get("function") or {}
                name = str(function.get("name") or "")
                if not name:
                    continue
                raw_args = function.get("arguments") or "{}"
                try:
                    import json
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    arguments = {}
                tool_calls.append(ToolCallRequest(
                    tool_name=name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    call_id=str(raw_call.get("id") or name),
                ))

            return ModelResponse(
                text=str(message.get("content") or ""),
                tool_calls=tool_calls,
                finish_reason="tool_call" if tool_calls else str(choice.get("finish_reason") or "stop"),
                provider=self.provider_name,
                raw_metadata={"model": body.get("model"), "usage": body.get("usage")},
            )
        except httpx.TimeoutException:
            return ModelResponse(
                text=f"{self.provider_name} timed out. Please retry shortly.",
                finish_reason="error", provider=self.provider_name,
                raw_metadata={"error": "timeout"},
            )
        except Exception:
            logger.exception("Unexpected %s gateway failure", self.provider_name)
            return ModelResponse(
                text="The AI provider could not be reached.",
                finish_reason="error", provider=self.provider_name,
                raw_metadata={"error": "provider_unavailable"},
            )
