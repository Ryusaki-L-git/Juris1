"""Authenticated, honest service catalogue for LED's JURIS integration.

This catalogue is metadata only. A service is marked available only when its
execution path is currently implemented; listing a provider operation does not
pretend that an upstream endpoint is wired.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from api.deps import get_request_identity
from core.authorization import require_verified_user
from core.identity import RequestIdentity

router = APIRouter(prefix="/v1", tags=["services"])


class ServiceCatalogueItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    service_id: str = Field(alias="serviceId")
    display_name: str = Field(alias="displayName")
    description: str
    token_price: float | None = Field(alias="tokenPrice")
    billing_unit: str = Field(alias="billingUnit")
    available: bool


# Prices are the approved customer-facing token prices currently present in the
# project context. Unknown prices deliberately remain null rather than guessed.
# Smart Refresh is the existing LED-owned gateway action; the other provider
# operations remain unavailable until their authenticated handlers are wired.
_CATALOGUE = (
    ServiceCatalogueItem(
        serviceId="smart_refresh",
        displayName="Smart Refresh",
        description="Refresh an owned case through LED's authorized case actions.",
        tokenPrice=0.5,
        billingUnit="per case",
        available=True,
    ),
    ServiceCatalogueItem(
        serviceId="case_detail",
        displayName="Case Detail",
        description="Retrieve the detailed case record from an authorized provider source.",
        tokenPrice=5,
        billingUnit="per request",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="cause_list_search",
        displayName="Cause List Search",
        description="Search cause-list records using supported court and date filters.",
        tokenPrice=9,
        billingUnit="per request",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="cnr_cause_list_check",
        displayName="CNR Cause List Check",
        description="Check cause-list appearances for one or more supplied CNRs.",
        tokenPrice=1,
        billingUnit="per CNR",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="order_download_pdf",
        displayName="Order Download (PDF)",
        description="Retrieve an order PDF from an authorized source.",
        tokenPrice=11,
        billingUnit="per order",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="order_ai_summary",
        displayName="Order + AI Summary",
        description="Retrieve an order and produce a clearly labelled AI summary.",
        tokenPrice=22,
        billingUnit="per order",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="order_markdown_pdf",
        displayName="Order Markdown & PDF",
        description="Retrieve an order in supported text and PDF formats.",
        tokenPrice=15,
        billingUnit="per order",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="bulk_case_refresh",
        displayName="Bulk Case Refresh",
        description="Refresh multiple owned cases through the authorized gateway.",
        tokenPrice=0.5,
        billingUnit="per CNR",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="legal_check",
        displayName="Legal Check",
        description="Run the configured legal-check workflow against authorized sources.",
        tokenPrice=199,
        billingUnit="per check",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="case_provisions",
        displayName="Case Provisions",
        description="Retrieve provisions associated with a case from a supported source.",
        tokenPrice=None,
        billingUnit="per request",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="act_section",
        displayName="Act Section",
        description="Retrieve one Act section from a supported legal source.",
        tokenPrice=None,
        billingUnit="per section",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="act_sections_batch",
        displayName="Act Sections (Batch)",
        description="Retrieve multiple Act sections in one supported request.",
        tokenPrice=None,
        billingUnit="per section",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="section_equivalents",
        displayName="Section Equivalents",
        description="Find equivalent section references using a supported legal source.",
        tokenPrice=None,
        billingUnit="per request",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="resolve_act_name",
        displayName="Resolve Act Name",
        description="Resolve an Act name or abbreviation using a supported legal source.",
        tokenPrice=None,
        billingUnit="per request",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="search_sections",
        displayName="Search Sections",
        description="Search statutory sections using a supported legal source.",
        tokenPrice=None,
        billingUnit="per request",
        available=False,
    ),
    ServiceCatalogueItem(
        serviceId="electoral_search",
        displayName="Electoral Search",
        description="Search electoral records only where lawful and supported by an authorized provider.",
        tokenPrice=None,
        billingUnit="per request",
        available=False,
    ),
)


@router.get("/services/catalogue", response_model=list[ServiceCatalogueItem])
def get_services_catalogue(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> list[ServiceCatalogueItem]:
    """Return service metadata only to a verified LED account."""
    require_verified_user(identity)
    return list(_CATALOGUE)
