"""
LLM-based line description parser.

Single structured claude-haiku-4-5 call per line item.
Extracts semantic signals: qty, unit_cost, billing_type, service_period,
category_hint, ambiguity_flags.

Never raises — always returns a valid LineDescriptionResult (degraded on failure).
"""
from __future__ import annotations

import json
import time
import re
from datetime import date, timedelta

import anthropic

from models import LineDescriptionResult


_CLIENT = anthropic.Anthropic()
_MODEL  = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# System prompt (bakes in the SOP classification knowledge)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an invoice line item extraction specialist. Your job is to extract
structured signals from invoice line item descriptions. You have deep knowledge of accounting
SOPs for GL classification.

CATEGORY CLASSIFICATION RULES (SOP):
- equipment: physical hardware, computers, servers, machinery (MacBook, server, printer)
- software: software licenses, SaaS subscriptions, apps (unless annual → check billing_type)
- cloud: cloud infrastructure, hosting, AWS/GCP/Azure, CDN, reserved instances
- professional_services: consulting, advisory, implementation, training services
  * "compliance review & advisory" → professional_services (advisory qualifier overrides compliance)
  * "regulatory compliance review" alone → could be professional_services or legal — flag ambiguity
- physical_goods: tangible goods that are not equipment (t-shirts, branded items, gift bags, supplies)
  * Marketing-ordered physical goods → physical_goods (overrides marketing category)
- marketing: advertising spend, campaigns, digital ads, SEO — NOT physical goods
- legal: legal counsel, attorney fees, litigation — NOT general advisory/consulting
- travel: flights, hotels, ground transport, per diem
- facilities: office lease, rent, utilities, renovation, construction, FF&E
  * "demolition" or "construction" → check if capitalize (>$2500 threshold) vs expense
- insurance: premiums, liability, D&O, errors & omissions
- telecom: phone, internet, SMS, API usage-based billing
- training: employee training, courses, certifications

BILLING TYPE RULES:
- annual: "Annual", "1 year", "12-month", date range spanning ~12 months
- monthly: "Monthly", "per month", date range of ~1 month
- usage-based: "overage", "usage-based", "per unit", "API calls"
- one-time: single purchase, no recurring indicators
- unknown: cannot determine

SERVICE PERIOD RULES:
- "Feb 26–Jan 27" → service_period_start="2026-02-01", service_period_end="2027-01-31"
  (two-digit years relative to invoice_date context)
- "Jan–Dec 2026" → start="2026-01-01", end="2026-12-31"
- "Dec 2025" on a Jan 2026 invoice → service_precedes_invoice=True (accrual signal)
- If service_period_end < invoice_date → service_precedes_invoice=True

AMBIGUITY FLAGS (use snake_case strings):
- "could_be_legal_or_consulting": compliance/regulatory work without advisory qualifier
- "could_be_marketing_or_consulting": brand work that could be either
- "could_be_expense_or_capitalize": facilities work near capitalization threshold
- "could_be_software_or_cloud": hosted service ambiguity

OUTPUT: Respond ONLY with a valid JSON object matching this exact schema:
{
  "quantity": <int or null>,
  "unit_cost": <"decimal_string" or null>,
  "quantity_source": <"explicit_pattern"|"inferred"|"not_present">,
  "billing_type": <"annual"|"monthly"|"usage-based"|"one-time"|"unknown">,
  "billing_confidence": <0.0-1.0>,
  "service_period_start": <"YYYY-MM-DD" or null>,
  "service_period_end": <"YYYY-MM-DD" or null>,
  "service_period_days": <int or null>,
  "period_source": <"explicit_range"|"inferred_from_keyword"|"not_present">,
  "category_hint": <"equipment"|"software"|"cloud"|"professional_services"|"physical_goods"|"marketing"|"legal"|"travel"|"facilities"|"insurance"|"telecom"|"training"|"unknown">,
  "category_confidence": <0.0-1.0>,
  "service_precedes_invoice": <bool>,
  "ambiguity_flags": [<list of snake_case strings>],
  "reasoning": <"brief explanation string">,
  "overall_confidence": <0.0-1.0>
}
"""

_SCHEMA_KEYS = {
    "quantity", "unit_cost", "quantity_source", "billing_type", "billing_confidence",
    "service_period_start", "service_period_end", "service_period_days", "period_source",
    "category_hint", "category_confidence", "service_precedes_invoice",
    "ambiguity_flags", "reasoning", "overall_confidence",
}

_VALID_CATEGORIES = {
    "equipment", "software", "cloud", "professional_services", "physical_goods",
    "marketing", "legal", "travel", "facilities", "insurance", "telecom", "training", "unknown",
}

_VALID_BILLING_TYPES = {"annual", "monthly", "usage-based", "one-time", "unknown"}
_VALID_QTY_SOURCES   = {"explicit_pattern", "inferred", "not_present"}
_VALID_PERIOD_SOURCES = {"explicit_range", "inferred_from_keyword", "not_present"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_service_days(start_str: str | None, end_str: str | None) -> int | None:
    if not start_str or not end_str:
        return None
    try:
        s = date.fromisoformat(start_str)
        e = date.fromisoformat(end_str)
        delta = (e - s).days + 1  # inclusive
        return max(0, delta)
    except ValueError:
        return None


def _validate_schema(data: dict) -> list[str]:
    """Return list of validation error strings. Empty list = valid."""
    errors = []
    missing = _SCHEMA_KEYS - set(data.keys())
    if missing:
        errors.append(f"missing keys: {missing}")

    if "category_hint" in data and data["category_hint"] not in _VALID_CATEGORIES:
        errors.append(f"invalid category_hint: {data['category_hint']!r}")
    if "billing_type" in data and data["billing_type"] not in _VALID_BILLING_TYPES:
        errors.append(f"invalid billing_type: {data['billing_type']!r}")
    if "quantity_source" in data and data["quantity_source"] not in _VALID_QTY_SOURCES:
        errors.append(f"invalid quantity_source: {data['quantity_source']!r}")
    if "period_source" in data and data["period_source"] not in _VALID_PERIOD_SOURCES:
        errors.append(f"invalid period_source: {data['period_source']!r}")
    if "ambiguity_flags" in data and not isinstance(data["ambiguity_flags"], list):
        errors.append("ambiguity_flags must be a list")

    return errors


def _build_result(data: dict, raw_description: str) -> LineDescriptionResult:
    """Convert validated dict to LineDescriptionResult."""
    start_str = data.get("service_period_start")
    end_str   = data.get("service_period_end")

    # Compute service_period_days if not provided by model
    service_days = data.get("service_period_days") or _compute_service_days(start_str, end_str)

    return LineDescriptionResult(
        quantity=data.get("quantity"),
        unit_cost=data.get("unit_cost"),
        quantity_source=data.get("quantity_source", "not_present"),
        billing_type=data.get("billing_type", "unknown"),
        billing_confidence=float(data.get("billing_confidence", 0.0)),
        service_period_start=start_str,
        service_period_end=end_str,
        service_period_days=service_days,
        period_source=data.get("period_source", "not_present"),
        category_hint=data.get("category_hint", "unknown"),
        category_confidence=float(data.get("category_confidence", 0.0)),
        service_precedes_invoice=bool(data.get("service_precedes_invoice", False)),
        ambiguity_flags=data.get("ambiguity_flags", []),
        reasoning=data.get("reasoning", ""),
        overall_confidence=float(data.get("overall_confidence", 0.0)),
        raw_description=raw_description,
    )


def _degraded_result(raw_description: str, error_tag: str) -> LineDescriptionResult:
    """Return a valid LineDescriptionResult with zero confidence on all signals."""
    return LineDescriptionResult(
        quantity=None,
        unit_cost=None,
        quantity_source="not_present",
        billing_type="unknown",
        billing_confidence=0.0,
        service_period_start=None,
        service_period_end=None,
        service_period_days=None,
        period_source="not_present",
        category_hint="unknown",
        category_confidence=0.0,
        service_precedes_invoice=False,
        ambiguity_flags=[f"extraction_failed:{error_tag}"],
        reasoning=f"LLM call failed after retries — {error_tag}",
        overall_confidence=0.0,
        raw_description=raw_description,
    )


def _make_user_message(
    description: str,
    amount: str,
    invoice_date: str,
    department: str,
    vendor: str,
    strict: bool = False,
) -> str:
    strict_note = "\nIMPORTANT: Respond ONLY with a JSON object. No prose, no markdown fences." if strict else ""
    return f"""Extract structured signals from this invoice line item.

Invoice context:
- Vendor: {vendor}
- Department: {department}
- Invoice date: {invoice_date}

Line item:
- Description: {description}
- Amount: {amount}
{strict_note}

Respond with the JSON schema only."""


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def parse_line_description(
    description:  str,
    amount:       str,
    invoice_date: str,
    department:   str,
    vendor:       str,
) -> LineDescriptionResult:
    """
    Single structured haiku call to extract semantic signals from a line item description.

    Error handling:
      Layer 1 — internal retries (network/rate-limit/malformed JSON/schema)
      Layer 2 — _degraded_result() on max retries (never raises)

    Returns a valid LineDescriptionResult always.
    """
    max_attempts = 3
    last_error_tag = "unknown"

    for attempt in range(1, max_attempts + 1):
        strict = attempt > 1  # stricter prompt on retry
        try:
            response = _CLIENT.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": _make_user_message(
                        description, amount, invoice_date, department, vendor, strict=strict
                    ),
                }],
            )
        except anthropic.RateLimitError as e:
            # Respect retry-after if present, then retry
            retry_after = getattr(e, "response", None)
            wait = 5
            if retry_after and hasattr(retry_after, "headers"):
                wait = int(retry_after.headers.get("retry-after", 5))
            time.sleep(wait)
            last_error_tag = "rate_limit"
            continue
        except anthropic.APIStatusError as e:
            if e.status_code and 400 <= e.status_code < 500:
                # 4xx — no retry
                return _degraded_result(description, f"api_error_{e.status_code}")
            # 5xx — retry with backoff
            time.sleep(2 ** (attempt - 1))
            last_error_tag = f"api_error_{e.status_code}"
            continue
        except (anthropic.APIConnectionError, anthropic.APITimeoutError):
            time.sleep(2 ** (attempt - 1))
            last_error_tag = "network_error"
            continue

        # Extract text content
        text = response.content[0].text.strip() if response.content else ""

        # Strip markdown fences if present
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        # Parse JSON
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            last_error_tag = "malformed_response"
            if attempt < max_attempts:
                continue  # retry with strict prompt
            return _degraded_result(description, "malformed_response")

        # Validate schema
        errors = _validate_schema(data)
        if errors:
            last_error_tag = "schema_validation_error"
            if attempt < max_attempts:
                continue  # retry with strict prompt
            return _degraded_result(description, "schema_validation_error")

        return _build_result(data, description)

    return _degraded_result(description, last_error_tag)
