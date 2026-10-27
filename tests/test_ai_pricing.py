from datetime import date
from decimal import Decimal

import pytest

from core.ai_pricing import (
    AIPriceConfigurationError,
    estimate_reservation_minor_units,
    price_usage,
    rates_for_model,
)


def test_gemini_38_flash_uses_published_promotional_rate_until_year_end():
    rates = rates_for_model("gemini-3.8-flash", on_date=date(2026, 12, 31))
    assert rates.input_usd_per_million == Decimal("0.75")
    assert rates.output_usd_per_million == Decimal("3.75")


def test_gemini_38_flash_switches_to_published_2027_rate():
    rates = rates_for_model("gemini-3.8-flash", on_date=date(2027, 1, 1))
    assert rates.input_usd_per_million == Decimal("1.50")
    assert rates.output_usd_per_million == Decimal("7.50")


def test_flash_lite_uses_current_official_model_id_and_rates():
    rates = rates_for_model("gemini-3.1-flash-lite")
    assert rates.input_usd_per_million == Decimal("0.25")
    assert rates.output_usd_per_million == Decimal("1.50")


def test_customer_price_targets_gross_margin_and_rounds_up_to_paise():
    charge = price_usage(
        "gemini-3.8-flash",
        1_000_000,
        0,
        usd_to_inr_rate=95,
        gross_margin_percent=40,
        on_date=date(2026, 10, 27),
    )
    assert charge.provider_cost_inr == Decimal("71.250000")
    assert charge.credits == Decimal("118.75")
    assert charge.credits_minor_units == 11875
    assert charge.gross_margin_percent == Decimal("40")


def test_zero_usage_has_zero_charge():
    charge = price_usage("gemini-2.5-flash-lite", 0, 0)
    assert charge.credits == Decimal("0.00")
    assert charge.credits_minor_units == 0


def test_unknown_model_fails_closed():
    with pytest.raises(AIPriceConfigurationError):
        price_usage("unpriced-model", 10, 10)


def test_reservation_is_positive_and_rounded_to_minor_units():
    reserve = estimate_reservation_minor_units(
        "gemini-2.5-flash-lite",
        1000,
        max_output_tokens=2048,
        buffer_multiplier=1.25,
    )
    assert reserve > 0
    assert isinstance(reserve, int)
