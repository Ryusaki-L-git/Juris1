"""
gateway/echo_gateway.py — JURIS development gateway.

This is the ONLY gateway active during development baseline.

Purpose:
    Fulfil the JurisModelGateway interface without calling any external
    model, making any network request, or returning fake AI responses.

    The echo gateway makes the pipeline structurally complete — every
    code path from LED → API → orchestrator → gateway → response works
    and is testable — without requiring an AI model or API key.

What it does NOT do:
    - It does NOT simulate AI behaviour.
    - It does NOT return legal analysis.
    - It does NOT pretend to be an LLM.
    - It does NOT make network calls.

When to replace it:
    When a real model is ready, implement a new JurisModelGateway subclass
    (e.g. GeminiGateway) and update the factory in gateway/factory.py.
    The rest of the codebase changes nothing.
"""

from __future__ import annotations

from gateway.base import JurisModelGateway, ModelRequest, ModelResponse


class EchoGateway(JurisModelGateway):
    """
    Development gateway.  Returns a structured no-op response.

    The response text makes it explicit to developers that no model
    is connected, rather than silently returning empty output.
    """

    @property
    def provider_name(self) -> str:
        return "echo"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        tool_call_summary = ""
        if request.tool_results:
            names = [r.tool_name for r in request.tool_results]
            tool_call_summary = f"  Tools executed this turn: {', '.join(names)}."

        return ModelResponse(
            text=(
                "[JURIS — development mode]\n"
                "No model is connected yet. "
                "The gateway pipeline is structurally complete.\n"
                f"  Received message: \"{request.user_message}\"{tool_call_summary}"
            ),
            tool_calls=[],
            finish_reason="stop",
            provider=self.provider_name,
            raw_metadata={"session_id": request.session_id},
        )
