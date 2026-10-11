# gateway — JURIS Model Gateway
#
# The gateway is the only place in JURIS that speaks to an AI model.
# Everything else in the system is gateway-agnostic.
#
# base.py          — JurisModelGateway ABC + ModelRequest/ModelResponse
# echo_gateway.py  — Development stub (no model, no network calls)
# gemini_gateway.py — (future) Gemini integration
