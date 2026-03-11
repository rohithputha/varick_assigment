"""
Read/write tools for rules_engine/prompts.json.

All functions operate directly on the JSON file — no caching.
Prompt updates take effect on the next description_extractor call
(which loads prompts.json fresh each time).

DB change-logging (prompt_changes table) is reserved for the feedback loop
and is not implemented here yet.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rules_engine.rules_tools import load_rules_config


_PROMPTS_PATH = Path(__file__).parent / "prompts.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    with open(_PROMPTS_PATH) as f:
        return json.load(f)


def _save(config: dict) -> None:
    config["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with open(_PROMPTS_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _bump_version(config: dict) -> None:
    try:
        major, minor = config.get("version", "1.0").split(".")
        config["version"] = f"{major}.{int(minor) + 1}"
    except (ValueError, AttributeError):
        config["version"] = "1.1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _derive_valid_signals(rules_config: dict) -> dict:
    """
    Derive valid_category_hints and valid_billing_types from rules.json.

    Scans all rule conditions for eq/in values on category_hint and billing_type,
    then adds all normalisation target values so the prompt covers every value
    the rule engine can receive after normalisation.

    "unknown" is always included as the fallback for both fields.
    """
    categories    = set()
    billing_types = set()

    for rule in rules_config.get("rules", []):
        condition = rule.get("condition", {})

        cat_constraint = condition.get("category_hint", {})
        if "eq" in cat_constraint:
            categories.add(cat_constraint["eq"])
        if "in" in cat_constraint:
            categories.update(cat_constraint["in"])

        bt_constraint = condition.get("billing_type", {})
        if "eq" in bt_constraint:
            billing_types.add(bt_constraint["eq"])
        if "in" in bt_constraint:
            billing_types.update(bt_constraint["in"])

    # Include normalisation targets so haiku can output canonical values directly
    for target in rules_config.get("category_normalisation", {}).values():
        categories.add(target)
    for target in rules_config.get("billing_type_normalisation", {}).values():
        billing_types.add(target)

    categories.add("unknown")
    billing_types.add("unknown")

    return {
        "valid_category_hints": sorted(categories),
        "valid_billing_types":  sorted(billing_types),
    }


def _build_prompt_config(prompt_id: str) -> dict:
    """
    Load prompt config from prompts.json and inject valid signal values
    derived from rules.json. This ensures the prompt stays in sync with
    the rule set automatically — no manual list to maintain.
    """
    with open(_PROMPTS_PATH) as f:
        config = json.load(f)["prompts"][prompt_id]

    derived = _derive_valid_signals(load_rules_config())
    config["valid_category_hints"] = derived["valid_category_hints"]
    config["valid_billing_types"]  = derived["valid_billing_types"]

    return config


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load_prompt_config(prompt_id: str = "description_extractor") -> dict:
    """
    Load prompt config fresh from disk with valid signal values derived
    from rules.json at call time.

    Called by description_extractor on every haiku invocation — no caching.
    Adding a new rule automatically expands the valid_category_hints/billing_types
    injected into the system prompt.
    """
    return _build_prompt_config(prompt_id)


def get_prompt(prompt_id: str = "description_extractor") -> dict:
    """
    Return the full prompt config for prompt_id with derived valid signal values.
    Includes system_prompt, valid_category_hints, valid_billing_types,
    valid_quantity_sources, valid_period_sources, few_shot_examples.
    """
    return _build_prompt_config(prompt_id)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def add_few_shot_example(example: dict, prompt_id: str = "description_extractor") -> dict:
    """
    Append a new few-shot example to prompts.json.

    The example dict must contain:
        input:  { description, vendor, department, invoice_date, amount }
        output: full LineDescriptionResult-shaped dict

    Optional fields (auto-filled if missing):
        id, added_at, added_by, triggered_by

    Returns:
        { "success": bool, "example_id": str, "prompt_version": str }
    """
    config = _load()
    prompt = config["prompts"][prompt_id]

    example_id = example.get("id") or f"ex{str(uuid4())[:8]}"
    new_example = {
        "id":           example_id,
        "input":        example["input"],
        "output":       example["output"],
        "added_at":     example.get("added_at", _now_iso()),
        "added_by":     example.get("added_by", "agent"),
        "triggered_by": example.get("triggered_by"),
    }

    prompt.setdefault("few_shot_examples", []).append(new_example)
    _bump_version(config)
    _save(config)

    return {
        "success":        True,
        "example_id":     example_id,
        "prompt_version": config["version"],
    }


def update_system_prompt(new_prompt: str, prompt_id: str = "description_extractor") -> dict:
    """
    Replace the system_prompt for a given prompt_id.

    NOTE: Per the plan, agents should NEVER call this autonomously.
    This is a human-only operation. The agent generates a suggestion and escalates;
    a human engineer calls this after review.

    Returns:
        { "success": bool, "prompt_version": str }
    """
    config = _load()
    config["prompts"][prompt_id]["system_prompt"] = new_prompt
    _bump_version(config)
    _save(config)
    return {"success": True, "prompt_version": config["version"]}
