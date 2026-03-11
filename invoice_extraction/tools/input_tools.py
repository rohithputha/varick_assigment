"""
Input tools — load invoice data from file or dict.
Normalizes line_items to list[dict] before storing in state.
"""
from __future__ import annotations

import json
from pathlib import Path

from invoice_extraction.exceptions import StructurizerError
from invoice_extraction.parsers.simple.line_item_splitter import (
    normalize_string_item,
    split_numbered_text,
)
from invoice_extraction.parsers.line_item_structurizer import structure_raw_line_items
from invoice_extraction.state import StateManager


# ---------------------------------------------------------------------------
# Internal normalizer
# ---------------------------------------------------------------------------

def _normalize_line_items(raw: dict) -> tuple[dict, str, bool]:
    """
    Normalize raw["line_items"] to list[dict] in-place.

    Returns:
        (normalized_raw_dict, input_format, structurizer_used)

    input_format: "structured" | "list_strings" | "raw_text"
    structurizer_used: True if haiku fallback was needed.

    Raises StructurizerError if raw_text cannot be structured.
    """
    raw = dict(raw)  # shallow copy — never mutate caller's dict
    line_items = raw.get("line_items")

    expected_total = str(raw.get("total_amount", "")) or None

    # Case A: already list[dict]
    if isinstance(line_items, list) and all(isinstance(i, dict) for i in line_items):
        # Ensure each item has a "description" key
        for item in line_items:
            if "description" not in item:
                item["description"] = ""
        raw["line_items"] = line_items
        return raw, "structured", False

    # Case B: list of strings
    if isinstance(line_items, list) and all(isinstance(i, str) for i in line_items):
        raw["line_items"] = [normalize_string_item(s) for s in line_items]
        return raw, "list_strings", False

    # Case C: single string (OCR / concatenated text)
    if isinstance(line_items, str):
        result = split_numbered_text(line_items)
        if result is not None:
            raw["line_items"] = result
            return raw, "raw_text", False

        # Regex failed — fall back to LLM structurizer
        structured = structure_raw_line_items(line_items, expected_total=expected_total)
        # Convert amount_raw → amount for consistency
        for item in structured:
            item["amount"] = item.pop("amount_raw", "0")
        raw["line_items"] = structured
        return raw, "raw_text", True

    # Fallback: empty or None line_items
    if line_items is None:
        raw["line_items"] = []
        return raw, "structured", False

    raise StructurizerError(
        f"Unexpected line_items type: {type(line_items).__name__}"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def load_invoice_from_file(path: str) -> dict:
    """
    Load an invoice from a JSON file, normalize line_items, create state.

    Args:
        path: Path to a JSON file containing invoice data.

    Returns:
        {
            "success": bool,
            "state_id": str,
            "source_path": str,
            "line_count": int,
            "input_format": str,
            "structurizer_used": bool,
            "confidence": 1.0,
        }
        On failure: {"success": False, "error": str, "confidence": 0.0}
    """
    try:
        p = Path(path)
        if not p.exists():
            return {"success": False, "error": f"File not found: {path!r}", "confidence": 0.0}
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse error: {e}", "confidence": 0.0}
    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}

    try:
        normalized, input_format, structurizer_used = _normalize_line_items(data)
    except StructurizerError as e:
        return {
            "success": False,
            "error": "unstructured_input_unresolvable",
            "detail": str(e),
            "confidence": 0.0,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}

    state = StateManager.create()
    state.raw_input         = normalized
    state.source_type       = "file"
    state.source_path       = str(path)
    state.input_format      = input_format
    state.structurizer_used = structurizer_used
    state.lines_expected    = len(normalized.get("line_items", []))
    StateManager.advance_stage(state.state_id, "LOADED")

    return {
        "success":           True,
        "state_id":          state.state_id,
        "source_path":       str(path),
        "line_count":        state.lines_expected,
        "input_format":      input_format,
        "structurizer_used": structurizer_used,
        "confidence":        1.0,
    }


def load_invoice_from_dict(data: dict) -> dict:
    """
    Load an invoice from a dict, normalize line_items, create state.

    Args:
        data: Raw invoice dict.

    Returns:
        {
            "success": bool,
            "state_id": str,
            "line_count": int,
            "input_format": str,
            "structurizer_used": bool,
            "confidence": 1.0,
        }
        On failure: {"success": False, "error": str, "confidence": 0.0}
    """
    try:
        normalized, input_format, structurizer_used = _normalize_line_items(data)
    except StructurizerError as e:
        return {
            "success": False,
            "error": "unstructured_input_unresolvable",
            "detail": str(e),
            "confidence": 0.0,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}

    state = StateManager.create()
    state.raw_input         = normalized
    state.source_type       = "dict"
    state.source_path       = None
    state.input_format      = input_format
    state.structurizer_used = structurizer_used
    state.lines_expected    = len(normalized.get("line_items", []))
    StateManager.advance_stage(state.state_id, "LOADED")

    return {
        "success":           True,
        "state_id":          state.state_id,
        "line_count":        state.lines_expected,
        "input_format":      input_format,
        "structurizer_used": structurizer_used,
        "confidence":        1.0,
    }
