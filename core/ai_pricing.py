"""Usage-based Gemini pricing for JURIS Credits.

Credits are separate from eCourts Case Tokens. Prices are computed from actual
provider input/output usage, converted to INR, grossed up to the configured
margin, and rounded upward to the nearest paise (0.01 Credit).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING

from core.config import settings


class AIPriceConfigurationError(ValueError):
    """Raised when a model has no reviewed, configured price."""


@dataclass(frozen=True)
class ModelRates:
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal


@dataclass(frozen=True)
class AIUsageCharge:
    model: str
    input_tokens: int
    output_tokens: int
    provider_cost_inr: Decimal
    credits: Decimal
    credits_minor_units: int
    gross_margin_percent: Decimal


# Verified standard-tier rates for text usage, USD per 1M tokens.
# Gemini 3.8 Flash has a published promotional rate through 2026-12-31.
_DEFAULT_RATES: dict[str, ModelRates] = {
    "gemini-3.8-flash": ModelRates(Decimal("0.75"), Decimal("3.75")),
    "gemini-3.1-flash-lite": ModelRates(Decimal("0.25"), Decimal("1.50")),
    "gemini-2.5-flash-lite": ModelRates(Decimal("0.10"), Decimal("0.40")),
    "gemini-2.5-flash": ModelRates(Decimal("0.30"), Decimal("2.50")),
}
_GEMINI_38_FLASH_2027 = ModelRates(Decimal("1.50"), Decimal("7.50"))
_ONE_MILLION = Decimal("1000000")
_CENT = Decimal("0.01")


def _configured_rates() -> dict[str, ModelRates]:
    """Apply optional JSON overrides without silently accepting malformed rates."""
    rates = dict(_DEFAULT_RATES)
    raw = settings.ai_model_pricing_json.strip()
    if not raw:
        return rates
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AIPriceConfigurationError("JURIS AI model pricing JSON is invalid.") from exc
    if not isinstance(parsed, dict):
        raise AIPriceConfigurationError("JURIS AI model pricing must be a JSON object.")
    for model, values in parsed.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(values, dict):
            raise AIPriceConfigurationError("JURIS AI model pricing entry is invalid.")
        try:
            input_rate = Decimal(str(values["input_usd_per_million"]))
            output_rate = Decimal(str(values["output_usd_per_million"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise AIPriceConfigurationError("JURIS AI model pricing entry is incomplete.") from exc
        if (
            not input_rate.is_finite()
            or not output_rate.is_finite()
            or input_rate < 0
            or output_rate < 0
        ):
            raise AIPriceConfigurationError("JURIS AI model rates must be finite and non-negative.")
        rates[model.strip()] = ModelRates(input_rate, output_rate)
    return rates


def rates_for_model(model: str, *, on_date: date | None = None) -> ModelRates:
    """Return a reviewed or explicitly configured model rate; unknown models fail closed."""
    clean_model = (model or "").strip()
    rates = _configured_rates()
    if clean_model in rates:
        raw_overrides = settings.ai_model_pricing_json.strip()
        overridden_models: set[str] = set()
        if raw_overrides:
            try:
                parsed_overrides = json.loads(raw_overrides)
                if isinstance(parsed_overrides, dict):
                    overridden_models = {str(key).strip() for key in parsed_overrides}
            except json.JSONDecodeError:
                # _configured_rates reports the configuration error below.
                overridden_models = set()
        if (
            clean_model == "gemini-3.8-flash"
            and clean_model not in overridden_models
            and (on_date or date.today()) >= date(2027, 1, 1)
        ):
            return _GEMINI_38_FLASH_2027
        return rates[clean_model]
    raise AIPriceConfigurationError(
        f"No JURIS Credits price is configured for Gemini model '{clean_model}'."
    )


def _whole_nonnegative(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer.")
    return value


def _money_up(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_CEILING)


def price_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    usd_to_inr_rate: Decimal | str | float | None = None,
    gross_margin_percent: Decimal | str | float | None = None,
    on_date: date | None = None,
) -> AIUsageCharge:
    """Price one model call. Output tokens must include any separately reported thinking tokens."""
    input_count = _whole_nonnegative(input_tokens, "input_tokens")
    output_count = _whole_nonnegative(output_tokens, "output_tokens")
    fx = Decimal(str(settings.ai_usd_to_inr_rate if usd_to_inr_rate is None else usd_to_inr_rate))
    margin_pct = Decimal(str(settings.ai_gross_margin_percent if gross_margin_percent is None else gross_margin_percent))
    if not fx.is_finite() or fx <= 0:
        raise AIPriceConfigurationError("JURIS USD-to-INR rate must be positive and finite.")
    if not margin_pct.is_finite() or margin_pct < 0 or margin_pct >= 100:
        raise AIPriceConfigurationError("JURIS AI gross margin must be at least 0 and below 100 percent.")

    rates = rates_for_model(model, on_date=on_date)
    provider_cost_usd = (
        Decimal(input_count) * rates.input_usd_per_million
        + Decimal(output_count) * rates.output_usd_per_million
    ) / _ONE_MILLION
    provider_cost_inr = provider_cost_usd * fx
    margin = margin_pct / Decimal("100")
    sell_price = provider_cost_inr / (Decimal("1") - margin)
    credits = _money_up(sell_price) if provider_cost_inr > 0 else Decimal("0.00")
    minor_units = int((credits * 100).to_integral_value(rounding=ROUND_CEILING))
    return AIUsageCharge(
        model=model.strip(),
        input_tokens=input_count,
        output_tokens=output_count,
        provider_cost_inr=provider_cost_inr.quantize(Decimal("0.000001")),
        credits=credits,
        credits_minor_units=minor_units,
        gross_margin_percent=margin_pct,
    )


def estimate_reservation_minor_units(
    model: str,
    request_characters: int,
    *,
    max_output_tokens: int | None = None,
    buffer_multiplier: Decimal | str | float | None = None,
) -> int:
    """Conservative per-call reservation based on serialized request size and output cap."""
    if isinstance(request_characters, bool) or not isinstance(request_characters, int) or request_characters < 0:
        raise ValueError("request_characters must be a non-negative integer.")
    output_cap = settings.ai_max_output_tokens if max_output_tokens is None else max_output_tokens
    if isinstance(output_cap, bool) or not isinstance(output_cap, int) or output_cap < 1:
        raise ValueError("max_output_tokens must be a positive integer.")
    buffer = Decimal(str(settings.ai_reservation_buffer if buffer_multiplier is None else buffer_multiplier))
    if not buffer.is_finite() or buffer < 1:
        raise AIPriceConfigurationError("JURIS AI reservation buffer must be at least 1.")
    # Two characters per token is intentionally conservative. The output is capped
    # at the same value passed to Gemini's generationConfig.
    estimated_input_tokens = math.ceil(request_characters / 2) + 128
    estimate = price_usage(
        model,
        estimated_input_tokens,
        output_cap,
    ).credits
    reserved = _money_up(estimate * buffer)
    return int((reserved * 100).to_integral_value(rounding=ROUND_CEILING))
