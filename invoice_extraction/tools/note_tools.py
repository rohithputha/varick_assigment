"""
Note tool — agent's only mechanism for recording reasoning to state.
"""
from __future__ import annotations

from invoice_extraction.state import StateManager


def add_note(state_id: str, note: str) -> dict:
    """
    Append a reasoning note to state.agent_notes.

    The agent calls this whenever it makes a non-trivial decision:
    retry, flag-and-continue, ambiguity detected, or halt.

    Returns:
        {"success": True, "note_count": int, "confidence": 1.0}
    """
    try:
        StateManager.add_agent_note(state_id, note)
        state = StateManager.get(state_id)
        return {
            "success":    True,
            "note_count": len(state.agent_notes),
            "confidence": 1.0,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}
