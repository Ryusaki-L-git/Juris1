# tools — JURIS Authorized Tool Layer
#
# Tools are the ONLY mechanism by which the model may access LED data.
# The model requests a tool call; JURIS validates, permissions-checks,
# and executes it.  The model never calls tools directly.
#
# base.py                    — JurisTool ABC + ToolInvocation/ToolResult
# case_tools.py              — Case retrieval and search
# client_tools.py            — Client profile access
# hearing_tools.py           — Hearing schedule access
# document_tools.py          — Document access (local-first aware)
# legal_research_tools.py    — Indian law / eCourts read-only research
# controlled_action_tools.py — Write actions (drafting, reminders, notifications)
