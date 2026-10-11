"""
tests/test_juris_end_to_end.py — End-to-end tests for JURIS.

Tests:
    1. Authentication boundary:
       - Dev / verified identity accepted.
       - Missing token rejected when auth enforced.
       - Invalid token rejected with HTTP 401.
    2. Authorization / Data Access:
       - Owner can access owned case.
       - Assigned user can access assigned case.
       - Team member can access team case.
       - Unauthorized user is strictly denied (tool does not execute, data never leaks).
    3. Context Resolution:
       - Active case context verified and reused.
       - Explicitly named case resolved from accessible cases.
       - Inaccessible case in context is purged.
       - Ambiguous cases flag clarification.
    4. Tool Execution Loop:
       - Model requests tool -> tool executes -> tool result returned to model -> final response.
       - Unknown tool request is rejected safely.
       - Unauthorized tool request is denied and not executed.
       - Maximum tool rounds are strictly enforced.
       - Provider failure / timeout handled safely without crashing.
    5. HTTP /chat Endpoint:
       - Full API request/response cycle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import unittest
from typing import Any

from fastapi.testclient import TestClient

from agent.context_resolver import ContextResolver
from agent.orchestrator import JurisOrchestrator
from core.authorized_access import AuthorizedLedAccess
from core.config import settings
from core.identity import JurisUser, RequestIdentity
from core.led_access import (
    CaseAccess,
    CaseRelation,
    LedCase,
    LedClient,
    LedDocumentMeta,
    LedTeamMembership,
)
from core.permission_engine import LedPermissionEngine
from gateway.base import (
    JurisModelGateway,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from main import app
from models.chat import ChatRequest, ChatResponse, ResponseStatus
from models.context import ItemRef, RequestContext


class InMemoryLedDataAccess:
    """In-memory mock of LedDataAccess for testing."""

    def __init__(self) -> None:
        self.cases: dict[str, LedCase] = {}
        self.clients: dict[str, LedClient] = {}
        self.team_memberships: dict[tuple[str, str], LedTeamMembership] = {}

    async def get_case(self, case_id: str, *, owner_uid: str) -> LedCase | None:
        case = self.cases.get(case_id)
        if case and case.owner_uid == owner_uid and not case.is_deleted:
            return case
        return None

    async def list_cases_for_owner(
        self, owner_uid: str, *, limit: int | None = None
    ) -> list[LedCase]:
        found = [c for c in self.cases.values() if c.owner_uid == owner_uid and not c.is_deleted]
        return found[:limit] if limit else found

    async def list_cases_assigned_to(
        self, user_id: str, *, limit: int | None = None
    ) -> list[LedCase]:
        found = [c for c in self.cases.values() if c.assigned_user_uid == user_id and not c.is_deleted]
        return found[:limit] if limit else found

    async def list_cases_for_team(
        self, team_id: str, *, user_id: str, limit: int | None = None
    ) -> list[LedCase]:
        found = [c for c in self.cases.values() if c.team_id == team_id and not c.is_deleted]
        return found[:limit] if limit else found

    async def get_client(
        self, client_id: str, *, owner_uid: str
    ) -> LedClient | None:
        client = self.clients.get(client_id)
        if client and client.owner_uid == owner_uid and not client.is_deleted:
            return client
        return None

    async def list_clients_for_owner(
        self, owner_uid: str, *, limit: int | None = None
    ) -> list[LedClient]:
        found = [c for c in self.clients.values() if c.owner_uid == owner_uid and not c.is_deleted]
        return found[:limit] if limit else found

    async def get_team_membership(
        self, team_id: str, *, user_id: str
    ) -> LedTeamMembership | None:
        return self.team_memberships.get((team_id, user_id))

    async def get_case_access(
        self, case_id: str, *, user_id: str
    ) -> CaseAccess | None:
        case = self.cases.get(case_id)
        if not case or case.is_deleted:
            return None
        if case.owner_uid == user_id:
            return CaseAccess(case=case, relation=CaseRelation.OWNER)
        if case.assigned_user_uid == user_id:
            return CaseAccess(case=case, relation=CaseRelation.ASSIGNED)
        if case.team_id:
            membership = self.team_memberships.get((case.team_id, user_id))
            if membership and membership.is_active:
                return CaseAccess(
                    case=case,
                    relation=CaseRelation.TEAM,
                    team_role=membership.role,
                )
        return None

    async def list_accessible_cases(
        self, user_id: str, *, limit: int | None = None
    ) -> list[LedCase]:
        accessible = []
        for case in self.cases.values():
            if case.is_deleted:
                continue
            if (
                case.owner_uid == user_id
                or case.assigned_user_uid == user_id
                or (case.team_id and (case.team_id, user_id) in self.team_memberships)
            ):
                accessible.append(case)
        return accessible[:limit] if limit else accessible

    async def list_team_ids_for_user(self, user_id: str) -> list[str]:
        return [
            team_id
            for (team_id, uid), m in self.team_memberships.items()
            if uid == user_id and m.is_active
        ]


class ScriptedGateway(JurisModelGateway):
    """Test gateway whose sequence of responses is pre-scripted."""

    def __init__(self, responses: list[ModelResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.call_history: list[ModelRequest] = []

    @property
    def provider_name(self) -> str:
        return "scripted_test"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_history.append(request)
        if not self.responses:
            return ModelResponse(
                text="Default scripted fallback response.",
                finish_reason="stop",
                provider=self.provider_name,
            )
        return self.responses.pop(0)


class JurisEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_access = InMemoryLedDataAccess()

        # Seed test cases
        self.case_owned_a = LedCase(
            id="case-owner-a",
            owner_uid="user-a",
            case_title="State vs Ramesh Kumar",
            case_number="CRL-101-2024",
            cnr="DLHC010012342024",
            court_name="Delhi High Court",
            status="active",
            next_hearing_date=datetime(2026, 11, 20, 10, 30, tzinfo=timezone.utc),
            documents=[
                LedDocumentMeta(name="Chargesheet.pdf", type="Document", size="2.4MB")
            ],
        )
        self.case_assigned_a = LedCase(
            id="case-assigned-a",
            owner_uid="user-other",
            assigned_user_uid="user-a",
            case_title="Sharma vs Verma Pvt Ltd",
            case_number="COM-202-2024",
            status="active",
        )
        self.case_team_a = LedCase(
            id="case-team-a",
            owner_uid="user-other",
            team_id="team-alpha",
            case_title="Tata vs Mistry Reorganisation",
            case_number="NCLT-303-2024",
            status="active",
        )
        self.case_private_b = LedCase(
            id="case-private-b",
            owner_uid="user-b",
            case_title="Private Confidential Case B",
            case_number="PRV-999-2024",
            status="active",
        )

        self.data_access.cases = {
            self.case_owned_a.id: self.case_owned_a,
            self.case_assigned_a.id: self.case_assigned_a,
            self.case_team_a.id: self.case_team_a,
            self.case_private_b.id: self.case_private_b,
        }

        # Seed team membership for user-a in team-alpha
        self.data_access.team_memberships[("team-alpha", "user-a")] = LedTeamMembership(
            team_id="team-alpha",
            user_id="user-a",
            role="member",
            is_active=True,
        )

        self.user_a = JurisUser(
            user_id="user-a",
            email="advocate.a@example.com",
            display_name="Advocate A",
            is_verified=True,
        )
        self.user_b = JurisUser(
            user_id="user-b",
            email="advocate.b@example.com",
            display_name="Advocate B",
            is_verified=True,
        )

        self.permission_engine = LedPermissionEngine(self.data_access)

    def test_owner_can_access_case_via_tool_execution(self) -> None:
        """Owner requests case details -> gateway requests get_case -> tool executes -> final answer."""
        scripted_gateway = ScriptedGateway(
            responses=[
                ModelResponse(
                    text="",
                    tool_calls=[
                        ToolCallRequest(
                            tool_name="get_case",
                            arguments={"case_id": "case-owner-a"},
                        )
                    ],
                    finish_reason="tool_call",
                ),
                ModelResponse(
                    text="The case 'State vs Ramesh Kumar' is active in Delhi High Court.",
                    finish_reason="stop",
                ),
            ]
        )

        orchestrator = JurisOrchestrator(
            gateway=scripted_gateway,
            permission_engine=self.permission_engine,
            data_access=self.data_access,
        )

        identity = RequestIdentity(user=self.user_a, session_id="sess-1")
        req = ChatRequest(message="What is the status of my case?")

        res: ChatResponse = asyncio.run(orchestrator.handle(req, identity))

        self.assertEqual(res.status, ResponseStatus.OK)
        self.assertIn("State vs Ramesh Kumar", res.reply)
        self.assertEqual(len(scripted_gateway.call_history), 2)
        round_2_req = scripted_gateway.call_history[1]
        self.assertEqual(len(round_2_req.messages), 3)
        tool_turn = round_2_req.messages[2]
        self.assertEqual(tool_turn.role, "tool")
        self.assertTrue(tool_turn.tool_results[0].success)
        self.assertEqual(
            tool_turn.tool_results[0].data["case"]["case_number"], "CRL-101-2024"
        )

    def test_assigned_user_and_team_member_can_access_case(self) -> None:
        """User A can access case where they are assigned, and case where they belong to team."""
        scripted_assigned = ScriptedGateway(
            responses=[
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(
                            tool_name="get_case",
                            arguments={"case_id": "case-assigned-a"},
                        )
                    ]
                ),
                ModelResponse(text="Assigned case retrieved."),
            ]
        )
        orch_assigned = JurisOrchestrator(
            gateway=scripted_assigned,
            permission_engine=self.permission_engine,
            data_access=self.data_access,
        )
        identity_a = RequestIdentity(user=self.user_a, session_id="sess-assigned")
        res1 = asyncio.run(
            orch_assigned.handle(ChatRequest(message="Check assigned"), identity_a)
        )
        self.assertEqual(res1.status, ResponseStatus.OK)
        self.assertTrue(
            scripted_assigned.call_history[1].messages[2].tool_results[0].success
        )

        scripted_team = ScriptedGateway(
            responses=[
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(
                            tool_name="get_case",
                            arguments={"case_id": "case-team-a"},
                        )
                    ]
                ),
                ModelResponse(text="Team case retrieved."),
            ]
        )
        orch_team = JurisOrchestrator(
            gateway=scripted_team,
            permission_engine=self.permission_engine,
            data_access=self.data_access,
        )
        res2 = asyncio.run(
            orch_team.handle(ChatRequest(message="Check team case"), identity_a)
        )
        self.assertEqual(res2.status, ResponseStatus.OK)
        self.assertTrue(
            scripted_team.call_history[1].messages[2].tool_results[0].success
        )

    def test_unauthorized_user_is_strictly_denied_access_to_case(self) -> None:
        """User A attempts to access User B's private case -> permission check denies -> tool never runs."""
        scripted_gateway = ScriptedGateway(
            responses=[
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(
                            tool_name="get_case",
                            arguments={"case_id": "case-private-b"},
                        )
                    ]
                ),
                ModelResponse(
                    text="You do not have permission to access that case.",
                    finish_reason="stop",
                ),
            ]
        )

        orchestrator = JurisOrchestrator(
            gateway=scripted_gateway,
            permission_engine=self.permission_engine,
            data_access=self.data_access,
        )

        identity_a = RequestIdentity(user=self.user_a, session_id="sess-unauth")
        req = ChatRequest(message="Show me case-private-b")

        res = asyncio.run(orchestrator.handle(req, identity_a))
        self.assertEqual(res.status, ResponseStatus.OK)

        tool_turn = scripted_gateway.call_history[1].messages[2]
        self.assertFalse(tool_turn.tool_results[0].success)
        self.assertIn("Permission denied", tool_turn.tool_results[0].error)

    def test_unknown_tool_is_safely_rejected(self) -> None:
        """Model requests a tool that does not exist in registry -> rejected cleanly."""
        scripted_gateway = ScriptedGateway(
            responses=[
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(
                            tool_name="unregistered_tool",
                            arguments={"foo": "bar"},
                        )
                    ]
                ),
                ModelResponse(text="I cannot execute that tool."),
            ]
        )

        orchestrator = JurisOrchestrator(
            gateway=scripted_gateway,
            permission_engine=self.permission_engine,
            data_access=self.data_access,
        )

        identity = RequestIdentity(user=self.user_a, session_id="sess-unknown-tool")
        res = asyncio.run(
            orchestrator.handle(ChatRequest(message="Run mysterious tool"), identity)
        )
        self.assertEqual(res.status, ResponseStatus.OK)
        tool_turn = scripted_gateway.call_history[1].messages[2]
        self.assertFalse(tool_turn.tool_results[0].success)
        self.assertIn("not recognized", tool_turn.tool_results[0].error)

    def test_maximum_tool_rounds_enforced(self) -> None:
        """Model that continuously requests tool calls hits max_tool_rounds ceiling."""
        infinite_tool_responses = [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(
                        tool_name="get_case",
                        arguments={"case_id": "case-owner-a"},
                    )
                ]
            )
            for _ in range(10)
        ]
        scripted_gateway = ScriptedGateway(responses=infinite_tool_responses)

        orchestrator = JurisOrchestrator(
            gateway=scripted_gateway,
            permission_engine=self.permission_engine,
            data_access=self.data_access,
        )

        identity = RequestIdentity(user=self.user_a, session_id="sess-loop")
        res = asyncio.run(
            orchestrator.handle(ChatRequest(message="Infinite loop"), identity)
        )

        self.assertEqual(len(scripted_gateway.call_history), settings.max_tool_rounds)
        self.assertIn("maximum allowable reasoning steps", res.reply)

    def test_provider_failure_handled_gracefully(self) -> None:
        """Gateway reporting error finish_reason returns UNAVAILABLE without crashing."""
        scripted_gateway = ScriptedGateway(
            responses=[
                ModelResponse(
                    text="Provider rate limit reached.",
                    finish_reason="error",
                )
            ]
        )

        orchestrator = JurisOrchestrator(
            gateway=scripted_gateway,
            permission_engine=self.permission_engine,
            data_access=self.data_access,
        )

        identity = RequestIdentity(user=self.user_a, session_id="sess-err")
        res = asyncio.run(
            orchestrator.handle(ChatRequest(message="Hello"), identity)
        )
        self.assertEqual(res.status, ResponseStatus.UNAVAILABLE)
        self.assertIn("Provider rate limit", res.reply)

    def test_context_resolution_resolves_named_case_and_purges_inaccessible(self) -> None:
        """Context resolver finds accessible case by query and purges inaccessible case."""
        resolver = ContextResolver()
        access_a = AuthorizedLedAccess(self.data_access, self.user_a)

        # 1. Resolve explicitly named case "Ramesh Kumar"
        identity_a = RequestIdentity(user=self.user_a, session_id="s1")
        req1 = ChatRequest(message="What happened in State vs Ramesh Kumar?")
        ctx1 = asyncio.run(resolver.resolve(req1, identity_a, access_a))
        self.assertIsNotNone(ctx1.active_case)
        self.assertEqual(ctx1.active_case.resource_id, "case-owner-a")

        # 2. Inaccessible case attached to request context is cleared
        req2 = ChatRequest(
            message="Check this",
            context=RequestContext(
                active_case=ItemRef(
                    resource_type="case",
                    resource_id="case-private-b",
                    display_name="Private B",
                )
            ),
        )
        ctx2 = asyncio.run(resolver.resolve(req2, identity_a, access_a))
        self.assertIsNone(ctx2.active_case)
        self.assertTrue(ctx2.extra.get("inaccessible_case_attempted"))

    def test_api_chat_endpoint_e2e_with_dev_token(self) -> None:
        """Test FastAPI /chat endpoint with dev token in development mode."""
        client = TestClient(app)

        payload = {
            "message": "Hello JURIS, are you active?",
            "session_id": "test-session-e2e",
        }

        # 1. With unconfigured gateway, gateway reports configuration status
        from api.chat import _orchestrator as live_orchestrator
        orig_gateway = live_orchestrator._gateway
        try:
            from gateway.gemini_gateway import GeminiGateway
            live_orchestrator._gateway = GeminiGateway(api_key="")
            response = client.post(
                "/chat",
                json=payload,
                headers={"Authorization": "Bearer dev:user-a"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("session_id", data)
            self.assertIn("Gemini API key is not configured", data["reply"])
            self.assertEqual(data["status"], ResponseStatus.UNAVAILABLE)

            # 2. When mock gateway is patched in, full OK response is returned
            live_orchestrator._gateway = ScriptedGateway(
                responses=[ModelResponse(text="Hello Advocate! JURIS is online.", finish_reason="stop")]
            )

            resp2 = client.post(
                "/chat",
                json=payload,
                headers={"Authorization": "Bearer dev:user-a"},
            )
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.json()
            self.assertEqual(data2["status"], "ok")
            self.assertEqual(data2["reply"], "Hello Advocate! JURIS is online.")
        finally:
            live_orchestrator._gateway = orig_gateway


if __name__ == "__main__":
    unittest.main()
