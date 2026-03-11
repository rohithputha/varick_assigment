"""
Read/write tools for rules_engine/rules.json.

All functions operate directly on the JSON file — no caching.
Rule updates take effect on the next classify_line_signals() call
(which loads rules.json fresh each time).

DB change-logging (rule_changes table) is reserved for the feedback loop
and is not implemented here yet.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_RULES_PATH = Path(__file__).parent / "rules.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    with open(_RULES_PATH) as f:
        return json.load(f)


def _save(config: dict) -> None:
    config["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with open(_RULES_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _bump_version(config: dict) -> None:
    """Increment patch version e.g. '1.0' → '1.1'."""
    try:
        major, minor = config.get("version", "1.0").split(".")
        config["version"] = f"{major}.{int(minor) + 1}"
    except (ValueError, AttributeError):
        config["version"] = "1.1"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load_rules_config(path: Path = _RULES_PATH) -> dict:
    """
    Load rules.json fresh from disk.
    Called by sop.py on every agent invocation — no caching.
    """
    with open(path) as f:
        return json.load(f)


def get_rules() -> dict:
    """
    Return the full rules_config dict.
    Includes version, updated_at, rules[], category_normalisation,
    billing_type_normalisation.
    """
    return _load()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def update_rule(rule_id: str, field: str, value: Any) -> dict:
    """
    Update a single output field on an existing rule.

    Args:
        rule_id: e.g. "rule2_equipment_capitalize"
        field:   output field name e.g. "gl_account" | "confidence" | "treatment"
        value:   new value

    Returns:
        { "success": bool, "rule_id": str, "before": Any, "after": Any }
    """
    config = _load()
    for rule in config.get("rules", []):
        if rule.get("id") == rule_id:
            before = rule["output"].get(field)
            rule["output"][field] = value
            _bump_version(config)
            _save(config)
            return {"success": True, "rule_id": rule_id, "before": before, "after": value}
    return {"success": False, "error": f"rule_id {rule_id!r} not found"}


def add_rule(rule: dict) -> dict:
    """
    Insert a new rule into rules[].

    The rule dict must include: id, priority, condition, output, enabled.
    Priority must not conflict with an existing rule.

    Returns:
        { "success": bool, "rule_id": str }
    """
    config = _load()
    existing_ids        = {r["id"] for r in config.get("rules", [])}
    existing_priorities = {r["priority"] for r in config.get("rules", [])}

    rule_id  = rule.get("id", "")
    priority = rule.get("priority")

    if not rule_id:
        return {"success": False, "error": "rule must have an 'id' field"}
    if rule_id in existing_ids:
        return {"success": False, "error": f"rule_id {rule_id!r} already exists"}
    if priority in existing_priorities:
        return {"success": False, "error": f"priority {priority} already in use — choose a different value"}

    config.setdefault("rules", []).append(rule)
    _bump_version(config)
    _save(config)
    return {"success": True, "rule_id": rule_id}


def disable_rule(rule_id: str) -> dict:
    """
    Set enabled=false on a rule. Does not delete it.

    Returns:
        { "success": bool, "rule_id": str }
    """
    config = _load()
    for rule in config.get("rules", []):
        if rule.get("id") == rule_id:
            rule["enabled"] = False
            _bump_version(config)
            _save(config)
            return {"success": True, "rule_id": rule_id}
    return {"success": False, "error": f"rule_id {rule_id!r} not found"}


def add_normalisation(raw: str, normalised: str, norm_type: str) -> dict:
    """
    Add an entry to category_normalisation or billing_type_normalisation.

    Args:
        raw:        The raw string to normalise (stored lower-cased)
        normalised: The canonical target value
        norm_type:  "category" | "billing_type"

    Returns:
        { "success": bool, "raw": str, "normalised": str }
    """
    table_key = {
        "category":     "category_normalisation",
        "billing_type": "billing_type_normalisation",
    }.get(norm_type)

    if table_key is None:
        return {
            "success": False,
            "error": f"norm_type must be 'category' or 'billing_type', got {norm_type!r}",
        }

    config = _load()
    config.setdefault(table_key, {})[raw.lower().strip()] = normalised
    _bump_version(config)
    _save(config)
    return {"success": True, "raw": raw.lower().strip(), "normalised": normalised}
