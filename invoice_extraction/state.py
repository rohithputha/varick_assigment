"""
IngestionState — single source of truth across all tool calls.
StateManager — CRUD + convenience setters for state fields.

All fields are JSON-serializable. v1 storage: module-level dict (in-memory).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from invoice_extraction.models import IngestionStage


# ---------------------------------------------------------------------------
# Legal stage transitions
# ---------------------------------------------------------------------------

_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    IngestionStage.INIT:          {IngestionStage.LOADED, IngestionStage.FAILED},
    IngestionStage.LOADED:        {IngestionStage.HEADER_PARSED, IngestionStage.FAILED},
    IngestionStage.HEADER_PARSED: {IngestionStage.LINES_PARSED, IngestionStage.FAILED},
    IngestionStage.LINES_PARSED:  {IngestionStage.VALIDATED, IngestionStage.FAILED},
    IngestionStage.VALIDATED:     {IngestionStage.COMPLETE, IngestionStage.LINES_PARSED, IngestionStage.FAILED},
    IngestionStage.COMPLETE:      set(),
    IngestionStage.FAILED:        set(),
}


# ---------------------------------------------------------------------------
# IngestionState
# ---------------------------------------------------------------------------

@dataclass
class IngestionState:
    # Identity
    state_id:   str
    created_at: str   # ISO UTC
    updated_at: str   # ISO UTC

    # Input
    raw_input:        dict | None     = None
    source_type:      str | None      = None   # "file" | "dict"
    source_path:      str | None      = None
    input_format:     str | None      = None   # "structured" | "list_strings" | "raw_text"
    structurizer_used: bool           = False

    # Parsing
    header:         dict | None       = None
    line_items:     list[dict]        = field(default_factory=list)
    lines_expected: int               = 0
    lines_parsed:   int               = 0

    # Validation
    structural_issues: list[dict]    = field(default_factory=list)
    business_issues:   list[dict]    = field(default_factory=list)
    validation_passed: bool          = False

    # Agent meta
    flags:         list[dict]        = field(default_factory=list)
    agent_notes:   list[str]         = field(default_factory=list)
    retry_counts:  dict[str, int]    = field(default_factory=dict)

    # Confidence
    field_confidences: dict[str, float] = field(default_factory=dict)
    overall_confidence: float           = 0.0

    # Stage
    current_stage: str               = IngestionStage.INIT
    stage_history: list[str]         = field(default_factory=list)

    # Output
    final_invoice: dict | None       = None


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

# v1 storage: module-level dict
_STORE: dict[str, IngestionState] = {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateManager:

    @staticmethod
    def create() -> IngestionState:
        """Create a new IngestionState and store it."""
        state_id = str(uuid.uuid4())
        now = _now_utc()
        state = IngestionState(
            state_id=state_id,
            created_at=now,
            updated_at=now,
            stage_history=[IngestionStage.INIT],
        )
        _STORE[state_id] = state
        return state

    @staticmethod
    def get(state_id: str) -> IngestionState:
        if state_id not in _STORE:
            raise KeyError(f"State not found: {state_id!r}")
        return _STORE[state_id]

    @staticmethod
    def update(state: IngestionState) -> None:
        state.updated_at = _now_utc()
        _STORE[state.state_id] = state

    @staticmethod
    def advance_stage(state_id: str, new_stage: str) -> None:
        """
        Advance state to new_stage. Validates transition legality.
        Raises ValueError on illegal transition.
        """
        state = StateManager.get(state_id)
        current = state.current_stage
        allowed = _LEGAL_TRANSITIONS.get(current, set())
        if new_stage not in allowed:
            raise ValueError(
                f"Illegal stage transition: {current} → {new_stage}. "
                f"Allowed: {allowed}"
            )
        state.current_stage = new_stage
        state.stage_history.append(new_stage)
        StateManager.update(state)

    @staticmethod
    def serialize(state_id: str) -> str:
        """Full JSON blob for HITL pause."""
        state = StateManager.get(state_id)
        return json.dumps(state.__dict__, default=str, indent=2)

    @staticmethod
    def deserialize(json_str: str) -> IngestionState:
        """Restore IngestionState from JSON blob (HITL resume)."""
        data = json.loads(json_str)
        state = IngestionState(**data)
        _STORE[state.state_id] = state
        return state

    # ------------------------------------------------------------------
    # Convenience setters (used by tools)
    # ------------------------------------------------------------------

    @staticmethod
    def get_line_item(state_id: str, line_index: int) -> dict | None:
        state = StateManager.get(state_id)
        if 0 <= line_index < len(state.line_items):
            return state.line_items[line_index]
        return None

    @staticmethod
    def set_line_item(state_id: str, line_index: int, item: dict) -> None:
        """Append or replace line item at line_index."""
        state = StateManager.get(state_id)
        # Grow list if needed
        while len(state.line_items) <= line_index:
            state.line_items.append({})
        state.line_items[line_index] = item
        state.lines_parsed = sum(1 for li in state.line_items if li)
        StateManager.update(state)

    @staticmethod
    def add_flag(state_id: str, flag_dict: dict) -> None:
        state = StateManager.get(state_id)
        state.flags.append(flag_dict)
        StateManager.update(state)

    @staticmethod
    def add_agent_note(state_id: str, note: str) -> None:
        state = StateManager.get(state_id)
        state.agent_notes.append(note)
        StateManager.update(state)

    @staticmethod
    def increment_retry(state_id: str, tool_name: str) -> int:
        """Increment retry counter for a tool. Returns new count."""
        state = StateManager.get(state_id)
        state.retry_counts[tool_name] = state.retry_counts.get(tool_name, 0) + 1
        count = state.retry_counts[tool_name]
        StateManager.update(state)
        return count
