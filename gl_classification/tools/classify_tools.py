"""
GL Classification tools — agent-callable wrappers around the rule engine.

classify_line()      — classify a single line (requires pre-loaded rules_config)
classify_all_lines() — classify all lines on an invoice (loads rules once)
"""
from __future__ import annotations

from dataclasses import asdict

from models import LineSignals
from rules_engine.rules_tools import get_rules
from rules_engine.classifier.sop import classify_line_signals


# ---------------------------------------------------------------------------
# classify_line
# ---------------------------------------------------------------------------

def classify_line(invoice_dict: dict, line_index: int, rules_config: dict) -> dict:
    """
    Classify one line item against the rule set.

    Reads:   invoice_dict["line_items"][line_index]["parsed_description"]
    Calls:   classify_line_signals(signals, rules_config)

    Returns:
        {
            success:              bool,
            line_number:          int,
            gl_account:           str | None,
            treatment:            str | None,
            base_expense_account: str | None,
            confidence:           float,
            reasoning:            str,
            applied_rule:         str,
            rules_version:        str,
            flagged:              bool,
            flag_reason:          str | None,
        }

    Never raises — exceptions are caught and returned as:
        { success: False, flagged: True, flag_reason: "classifier_error: ..." }
    """
    line_items  = invoice_dict.get("line_items") or []
    line_number = line_index  # fallback

    try:
        if line_index >= len(line_items):
            return _error_result(
                line_number=line_index,
                rules_version=rules_config.get("version", "unknown"),
                reason=f"line_index {line_index} out of range (invoice has {len(line_items)} lines)",
            )

        line = line_items[line_index]
        line_number = line.get("line_number", line_index)
        parsed      = line.get("parsed_description")

        if parsed is None:
            return _error_result(
                line_number=line_number,
                rules_version=rules_config.get("version", "unknown"),
                reason="no_parsed_description: description extraction failed during ingestion",
            )

        # Build LineSignals — float conversions for numeric fields
        unit_cost_raw   = parsed.get("unit_cost")
        unit_cost: float | None = float(unit_cost_raw) if unit_cost_raw is not None else None

        amount_field      = line.get("amount") or {}
        line_amount_raw   = amount_field.get("value")
        line_amount: float | None = (
            float(line_amount_raw) if line_amount_raw is not None else None
        )

        signals = LineSignals(
            line_number=line_number,
            category_hint=parsed.get("category_hint", "unknown"),
            billing_type=parsed.get("billing_type", "unknown"),
            unit_cost=unit_cost,
            line_amount=line_amount,
            ambiguity_flags=parsed.get("ambiguity_flags") or [],
            reasoning=parsed.get("reasoning", ""),
        )

        result = classify_line_signals(signals, rules_config)

        return {
            "success":              True,
            "line_number":          result.line_number,
            "gl_account":           result.gl_account,
            "treatment":            result.treatment,
            "base_expense_account": result.base_expense_account,
            "confidence":           result.confidence,
            "reasoning":            result.reasoning,
            "applied_rule":         result.applied_rule,
            "rules_version":        result.rules_version,
            "flagged":              result.flagged,
            "flag_reason":          result.flag_reason,
        }

    except Exception as e:
        return _error_result(
            line_number=line_number,
            rules_version=rules_config.get("version", "unknown"),
            reason=f"classifier_error: {e}",
        )


# ---------------------------------------------------------------------------
# classify_all_lines
# ---------------------------------------------------------------------------

def classify_all_lines(invoice_dict: dict) -> dict:
    """
    Classify every line item on the invoice.

    Calls get_rules() once — all lines share the same rules_version.
    A single line failure does NOT abort the rest — all lines are always attempted.

    Returns:
        {
            success:            bool,
            results:            list[dict],   # one per line, same schema as classify_line
            flagged_lines:      list[int],    # line_numbers with flagged=True
            all_classified:     bool,
            overall_confidence: float,
        }
    """
    try:
        rules_config = get_rules()
    except Exception as e:
        return {
            "success":            False,
            "error":              f"failed to load rules: {e}",
            "results":            [],
            "flagged_lines":      [],
            "all_classified":     False,
            "overall_confidence": 0.0,
        }

    line_items = invoice_dict.get("line_items") or []
    results: list[dict] = []

    for i in range(len(line_items)):
        result = classify_line(invoice_dict, i, rules_config)
        results.append(result)

    flagged_lines = [r["line_number"] for r in results if r.get("flagged")]
    confidences   = [r.get("confidence", 0.0) for r in results]
    overall       = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        "success":            True,
        "results":            results,
        "flagged_lines":      flagged_lines,
        "all_classified":     len(flagged_lines) == 0,
        "overall_confidence": round(overall, 4),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_result(line_number: int, rules_version: str, reason: str) -> dict:
    return {
        "success":              False,
        "line_number":          line_number,
        "gl_account":           None,
        "treatment":            None,
        "base_expense_account": None,
        "confidence":           0.0,
        "reasoning":            reason,
        "applied_rule":         "no_rule_matched",
        "rules_version":        rules_version,
        "flagged":              True,
        "flag_reason":          reason,
    }
