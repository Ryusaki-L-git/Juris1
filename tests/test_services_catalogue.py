from __future__ import annotations

from fastapi.testclient import TestClient

from api.deps import get_request_identity
from core.identity import JurisUser, RequestIdentity
from main import app


def _verified_identity() -> RequestIdentity:
    return RequestIdentity(
        user=JurisUser(user_id="catalogue-test-user", is_verified=True),
        session_id="catalogue-test-session",
        request_id="catalogue-test-request",
        source="test",
    )


def test_catalogue_is_authenticated_and_has_sixteen_distinct_services() -> None:
    async def override_identity() -> RequestIdentity:
        return _verified_identity()

    app.dependency_overrides[get_request_identity] = override_identity
    try:
        response = TestClient(app).get("/v1/services/catalogue")
    finally:
        app.dependency_overrides.pop(get_request_identity, None)

    assert response.status_code == 200
    services = response.json()
    assert len(services) == 16
    ids = [service["serviceId"] for service in services]
    assert len(set(ids)) == 16
    assert "case_search" not in ids

    smart_refresh = next(item for item in services if item["serviceId"] == "smart_refresh")
    assert smart_refresh["tokenPrice"] == 0.5
    assert smart_refresh["available"] is True

    gateway_services = {
        "case_detail", "cause_list_search", "cnr_cause_list_check",
        "order_download_pdf", "order_ai_summary", "order_markdown_pdf",
        "bulk_case_refresh",
    }
    for service in services:
        if service["serviceId"] in gateway_services:
            assert service["available"] is True
    legal_check = next(item for item in services if item["serviceId"] == "legal_check")
    assert legal_check["tokenPrice"] == 199
    assert legal_check["available"] is False

    # Never represent an unknown price as free or guess a number.
    unknown_price_services = [
        item for item in services if item["serviceId"] in {
            "case_provisions",
            "act_section",
            "act_sections_batch",
            "section_equivalents",
            "resolve_act_name",
            "search_sections",
            "electoral_search",
        }
    ]
    assert all(item["tokenPrice"] is None for item in unknown_price_services)


def test_catalogue_rejects_unverified_development_identity() -> None:
    response = TestClient(app).get("/v1/services/catalogue")
    assert response.status_code == 401
