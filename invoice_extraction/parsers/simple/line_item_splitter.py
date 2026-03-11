"""
Pure Python line item splitting. No LLM, no side effects, deterministic.

Handles three input cases:
  Case A: line_items is list[dict]   → pass through
  Case B: line_items is list[str]    → extract amount from end of each string
  Case C: line_items is str          → split on numbered item pattern, then normalize
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Case B: amount at end of string
# ---------------------------------------------------------------------------

# Matches trailing amounts like: $18,000  |  18000  |  18,000.00
AMOUNT_AT_END = re.compile(r'(?:\$\s*)?([\d,]+(?:\.\d{2})?)\s*$')


def normalize_string_item(raw: str) -> dict:
    """
    Convert a raw string line item into {"description": ..., "amount": ...}.

    Extracts amount from the end of the string.
    If no amount found, amount=None (triggers missing-amount flag upstream).
    """
    raw = raw.strip()
    m = AMOUNT_AT_END.search(raw)
    if m:
        description = raw[:m.start()].strip()
        # Remove leading $ if stuck to description
        description = re.sub(r'\s*\$\s*$', '', description).strip()
        amount_str = m.group(1).replace(',', '')
        return {"description": description, "amount": amount_str}
    return {"description": raw, "amount": None}


# ---------------------------------------------------------------------------
# Case C tier-1: split on numbered items
# ---------------------------------------------------------------------------

# Split before patterns like "1. " or "\n1. " or "  2. "
NUMBERED_ITEM = re.compile(r'(?=\n?\s*\d+\.\s)')


def split_numbered_text(raw: str) -> list[dict] | None:
    """
    Attempt to split a raw text block into numbered line item dicts.

    Returns list[dict] if split is valid (2+ items, each has a detectable amount).
    Returns None if result looks like fragments — caller falls back to
    line_item_structurizer (haiku).

    Each returned dict: {"description": str, "amount": str | None}
    """
    parts = [p.strip() for p in NUMBERED_ITEM.split(raw.strip()) if p.strip()]

    if len(parts) < 2:
        return None  # didn't split — signal fallback

    # Remove leading "N. " numbering from each part
    cleaned = []
    for p in parts:
        # Strip "1. " or "1) " prefix
        p = re.sub(r'^\d+[.)]\s*', '', p)
        cleaned.append(p)

    items = [normalize_string_item(p) for p in cleaned]

    # If any item has no detectable amount, fall back to structurizer
    if any(item["amount"] is None for item in items):
        return None

    return items
