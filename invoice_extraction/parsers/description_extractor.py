"""
LLM-based line description parser.

Single structured claude-haiku-4-5 call per line item.
Extracts semantic signals: qty, unit_cost, billing_type, service_period,
category_hint, ambiguity_flags.

Never raises — always returns a valid LineDescriptionResult (degraded on failure).

System prompt, valid enum values, and few-shot examples are loaded from
rules_engine/prompts.json at call time via rules_engine.prompts_tools.
Source code never changes — only the metadata file does.
"""
from __future__ import annotations

import json
import time
import re
from datetime import date

import anthropic

from models import LineDescriptionResult
from rules_engine.prompts_tools import load_prompt_config


_CLIENT = anthropic.Anthropic()
_MODEL  = "claude-haiku-4-5-20251001"

_SCHEMA_KEYS = {
    "quantity", "unit_cost", "quantity_source", "billing_type", "billing_confidence",
    "service_period_start", "service_period_end", "service_period_days", "period_source",
    "category_hint", "category_confidence", "service_precedes_invoice",
    "ambiguity_flags", "reasoning", "overall_confidence",
}


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_system_prompt(config: dict) -> str:
    """
    Combine the base system prompt with valid enum values injected inline.
    Haiku always sees the current allowed values from prompts.json.
    """
    system  = config["system_prompt"]
    system += f"\n\ncategory_hint must be one of: {config['valid_category_hints']}"
    system += f"\nbilling_type must be one of: {config['valid_billing_types']}"
    return system


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


def _build_messages(
    config: dict,
    description: str,
    amount: str,
    invoice_date: str,
    department: str,
    vendor: str,
    strict: bool = False,
) -> list[dict]:
    """
    Build the messages list with few-shot examples as alternating user/assistant
    turns, followed by the actual line item to classify.

    Few-shot examples are injected as real conversation turns so haiku sees
    exactly what a correct assistant response looks like and continues the pattern.
    """
    messages: list[dict] = []

    for ex in config.get("few_shot_examples", []):
        inp = ex["input"]
        messages.append({
            "role": "user",
            "content": _make_user_message(
                description=inp.get("description", ""),
                amount=inp.get("amount", ""),
                invoice_date=inp.get("invoice_date", ""),
                department=inp.get("department", ""),
                vendor=inp.get("vendor", ""),
            ),
        })
        messages.append({
            "role": "assistant",
            "content": json.dumps(ex["output"]),
        })

    messages.append({
        "role": "user",
        "content": _make_user_message(
            description, amount, invoice_date, department, vendor, strict=strict
        ),
    })
    return messages


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


def _validate_schema(data: dict, config: dict) -> list[str]:
    """Return list of validation error strings. Empty list = valid."""
    errors = []
    missing = _SCHEMA_KEYS - set(data.keys())
    if missing:
        errors.append(f"missing keys: {missing}")

    valid_cats    = set(config.get("valid_category_hints", []))
    valid_bts     = set(config.get("valid_billing_types", []))
    valid_qty_src = set(config.get("valid_quantity_sources", []))
    valid_per_src = set(config.get("valid_period_sources", []))

    if "category_hint" in data and data["category_hint"] not in valid_cats:
        errors.append(f"invalid category_hint: {data['category_hint']!r}")
    if "billing_type" in data and data["billing_type"] not in valid_bts:
        errors.append(f"invalid billing_type: {data['billing_type']!r}")
    if "quantity_source" in data and data["quantity_source"] not in valid_qty_src:
        errors.append(f"invalid quantity_source: {data['quantity_source']!r}")
    if "period_source" in data and data["period_source"] not in valid_per_src:
        errors.append(f"invalid period_source: {data['period_source']!r}")
    if "ambiguity_flags" in data and not isinstance(data["ambiguity_flags"], list):
        errors.append("ambiguity_flags must be a list")

    return errors


def _build_result(data: dict, raw_description: str, prompt_version: str) -> LineDescriptionResult:
    """Convert validated dict to LineDescriptionResult."""
    start_str = data.get("service_period_start")
    end_str   = data.get("service_period_end")

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
        ambiguity_flags=[],
        reasoning=data.get("reasoning", ""),
        overall_confidence=float(data.get("overall_confidence", 0.0)),
        raw_description=raw_description,
        prompt_version=prompt_version,
    )


def _degraded_result(raw_description: str, error_tag: str, prompt_version: str = "unknown") -> LineDescriptionResult:
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
        prompt_version=prompt_version,
    )


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

    Loads system prompt, valid enum values, and few-shot examples from
    rules_engine/prompts.json at call time. No hardcoded prompt constants.

    Error handling:
      Layer 1 — internal retries (network/rate-limit/malformed JSON/schema)
      Layer 2 — _degraded_result() on max retries (never raises)

    Returns a valid LineDescriptionResult always.
    """
    config         = load_prompt_config("description_extractor")
    system_prompt  = _build_system_prompt(config)
    prompt_version = config.get("version", "unknown")

    max_attempts   = 3
    last_error_tag = "unknown"

    for attempt in range(1, max_attempts + 1):
        strict = attempt > 1  # stricter prompt on retry
        try:
            response = _CLIENT.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=system_prompt,
                messages=_build_messages(
                    config, description, amount, invoice_date, department, vendor,
                    strict=strict,
                ),
            )
        except anthropic.RateLimitError as e:
            retry_after = getattr(e, "response", None)
            wait = 5
            if retry_after and hasattr(retry_after, "headers"):
                wait = int(retry_after.headers.get("retry-after", 5))
            time.sleep(wait)
            last_error_tag = "rate_limit"
            continue
        except anthropic.APIStatusError as e:
            if e.status_code and 400 <= e.status_code < 500:
                return _degraded_result(description, f"api_error_{e.status_code}", prompt_version)
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
                continue
            return _degraded_result(description, "malformed_response", prompt_version)

        # Validate schema
        errors = _validate_schema(data, config)
        if errors:
            last_error_tag = "schema_validation_error"
            if attempt < max_attempts:
                continue
            return _degraded_result(description, "schema_validation_error", prompt_version)

        return _build_result(data, description, prompt_version)

    return _degraded_result(description, last_error_tag, prompt_version)
