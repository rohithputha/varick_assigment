"""
SOP rule engine — pure interpreter for rules.json.

This file never changes. Only rules.json changes.

Public API:
  load_rules_config(path?) -> dict
  normalise_signals(signals, rules_config) -> LineSignals
  classify_line_signals(signals, rules_config) -> GLClassificationResult
"""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from models import GLClassificationResult, LineSignals
from rules_engine.rules_tools import load_rules_config
from rules_engine.classifier.condition_eval import matches


# ---------------------------------------------------------------------------
# Signal normalisation
# ---------------------------------------------------------------------------

def normalise_signals(signals: LineSignals, rules_config: dict) -> LineSignals:
    """
    Apply category and billing_type normalisation tables from rules_config.

    e.g. "saas" → "software", "yearly" → "annual"
    Returns a new LineSignals with normalised string fields.
    """
    cat_norm = rules_config.get("category_normalisation", {})
    bt_norm  = rules_config.get("billing_type_normalisation", {})

    cat = signals.category_hint.lower().strip()
    bt  = signals.billing_type.lower().strip()

    return replace(
        signals,
        category_hint=cat_norm.get(cat, cat),
        billing_type=bt_norm.get(bt, bt),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_line_signals(
    signals: LineSignals,
    rules_config: dict | None = None,
) -> GLClassificationResult:
    """
    Classify one line's signals against the rule set.

    If rules_config is None, loads rules.json fresh from disk.

    Evaluation order:
      1. Pre-check: ambiguity_flags present → FLAGGED immediately
      2. Normalise signals
      3. Iterate enabled rules in priority order; first match wins
      4. No match → FLAGGED with "no_rule_matched"

    Never raises.
    """
    if rules_config is None:
        rules_config = load_rules_config()

    rules_version = rules_config.get("version", "unknown")

    # Pre-check: ambiguity → flag without attempting rule match
    if signals.ambiguity_flags:
        return _flagged(
            signals.line_number,
            rules_version,
            reason=", ".join(signals.ambiguity_flags),
        )

    signals = normalise_signals(signals, rules_config)
    signals_dict = asdict(signals)

    active_rules = [r for r in rules_config.get("rules", []) if r.get("enabled", True)]
    for rule in sorted(active_rules, key=lambda r: r["priority"]):
        if matches(rule["condition"], signals_dict):
            return _build_result(rule, signals.line_number, rules_version)

    return _flagged(signals.line_number, rules_version, reason="no_rule_matched")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_result(rule: dict, line_number: int, rules_version: str) -> GLClassificationResult:
    output = rule["output"]
    return GLClassificationResult(
        line_number=line_number,
        gl_account=output.get("gl_account"),
        treatment=output.get("treatment"),
        base_expense_account=output.get("base_expense_account"),
        confidence=float(output.get("confidence", 1.0)),
        flagged=False,
        flag_reason=None,
        rules_version=rules_version,
    )


def _flagged(line_number: int, rules_version: str, reason: str) -> GLClassificationResult:
    return GLClassificationResult(
        line_number=line_number,
        gl_account=None,
        treatment=None,
        base_expense_account=None,
        confidence=0.0,
        flagged=True,
        flag_reason=reason,
        rules_version=rules_version,
    )
