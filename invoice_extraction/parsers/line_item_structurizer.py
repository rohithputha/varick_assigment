"""
LLM-based fallback for unstructured invoice text → list of line item dicts.

Fires only when line_item_splitter.split_numbered_text() returns None.
Called once per invoice at LOAD time.

CAN raise StructurizerError — unprocessable input halts the agent at LOAD stage.
"""
from __future__ import annotations

import json
import re

import anthropic

from invoice_extraction.exceptions import StructurizerError


_CLIENT = anthropic.Anthropic()
_MODEL  = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = (
    "You are an invoice line item extractor. Split the raw invoice text into individual "
    "line items. Return ONLY a JSON array where each item has exactly these keys: "
    "{\"line_number\": <int>, \"description\": <str>, \"amount_raw\": <str>}. "
    "Preserve description text exactly — do not interpret or rephrase it. "
    "Extract amount_raw as a plain numeric string (no currency symbol, no commas). "
    "If an item has no discernible amount, use \"0\" as amount_raw."
)


def structure_raw_line_items(
    raw_text: str,
    expected_total: str | None = None,
) -> list[dict]:
    """
    Single haiku call to split unstructured invoice text into line item dicts.

    Args:
        raw_text:       Raw invoice text block containing line items.
        expected_total: Header total as a sanity hint (optional, sent to model as context).

    Returns:
        List of dicts: [{"line_number": int, "description": str, "amount_raw": str}, ...]

    Raises:
        StructurizerError: If the haiku call fails or returns unusable output.
                           Caller (input_tools) halts load on this error.
    """
    context = f"Invoice total: {expected_total}\n\n" if expected_total else ""
    user_message = (
        f"{context}Raw invoice text to split into line items:\n\n{raw_text}\n\n"
        "Respond ONLY with the JSON array."
    )

    try:
        response = _CLIENT.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        raise StructurizerError(f"API error during structurization: {e}") from e

    text = response.content[0].text.strip() if response.content else ""

    # Strip markdown fences
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    try:
        items = json.loads(text)
    except json.JSONDecodeError as e:
        raise StructurizerError(f"Structurizer returned malformed JSON: {e}") from e

    if not isinstance(items, list) or len(items) == 0:
        raise StructurizerError("Structurizer returned empty or non-list result")

    # Validate each item has required keys
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise StructurizerError(f"Item {i} is not a dict: {item!r}")
        if "description" not in item:
            raise StructurizerError(f"Item {i} missing 'description' key")
        if "amount_raw" not in item:
            item["amount_raw"] = "0"  # tolerate missing amount_raw
        if "line_number" not in item:
            item["line_number"] = i + 1

    return items
