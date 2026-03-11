"""
Read/write tools for approval_routing/thresholds.json.

Same pattern as rules_engine/rules_tools.py — reads fresh from disk on every call.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_THRESHOLDS_PATH = Path(__file__).parent / "thresholds.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    with open(_THRESHOLDS_PATH) as f:
        return json.load(f)


def _save(config: dict) -> None:
    config["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with open(_THRESHOLDS_PATH, "w") as f:
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

def get_thresholds() -> dict:
    """
    Return the full thresholds config dict (version, updated_at, thresholds, cloud_software_accounts).
    Reads fresh from disk — no caching.
    """
    return _load()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def update_threshold(key: str, value: Any) -> dict:
    """
    Update a single threshold value.

    Args:
        key:   One of: auto_approve_max | dept_manager_max | marketing_max | engineering_max
        value: New numeric value

    Returns:
        { "success": bool, "key": str, "before": Any, "after": Any }
    """
    config = _load()
    thresholds = config.get("thresholds", {})
    if key not in thresholds:
        return {"success": False, "error": f"threshold key {key!r} not found"}

    before = thresholds[key]
    thresholds[key] = value
    config["thresholds"] = thresholds
    _bump_version(config)
    _save(config)
    return {"success": True, "key": key, "before": before, "after": value}


def update_cloud_software_accounts(accounts: list[str]) -> dict:
    """
    Replace the cloud_software_accounts list.

    Returns:
        { "success": bool, "before": list, "after": list }
    """
    config = _load()
    before = config.get("cloud_software_accounts", [])
    config["cloud_software_accounts"] = accounts
    _bump_version(config)
    _save(config)
    return {"success": True, "before": before, "after": accounts}
