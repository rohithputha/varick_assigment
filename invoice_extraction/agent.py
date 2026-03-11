"""
Invoice Ingestion Agent — main entry point.

The agent IS the orchestrator. It accepts a JSON file path or a raw invoice dict,
calls granular tools step-by-step, makes decisions at each step, and returns a
finalized Invoice dict as the contract boundary with the PO Matching stage.
"""
from __future__ import annotations

import json
import os

import anthropic

from invoice_extraction.tools.input_tools    import load_invoice_from_file, load_invoice_from_dict
from invoice_extraction.tools.parse_tools    import parse_invoice_header, parse_invoice_line, parse_line_description
from invoice_extraction.tools.validate_tools import validate_structure, validate_business_rules, validate_single_rule
from invoice_extraction.tools.finalize_tools import compute_confidence, finalize_invoice
from invoice_extraction.tools.note_tools     import add_note
from invoice_extraction.state import StateManager


# ---------------------------------------------------------------------------
# Tool definitions for the Claude agent
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "load_invoice_from_file",
        "description": (
            "Load an invoice from a JSON file. Normalizes line_items to list[dict]. "
            "Creates ingestion state. Returns state_id, line_count, input_format, "
            "structurizer_used. Call this first if input is a file path string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to invoice JSON file"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "load_invoice_from_dict",
        "description": (
            "Load an invoice from a raw dict. Normalizes line_items to list[dict]. "
            "Creates ingestion state. Returns state_id, line_count, input_format, "
            "structurizer_used. Call this first if input is a dict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "object", "description": "Raw invoice dict"},
            },
            "required": ["data"],
        },
    },
    {
        "name": "parse_invoice_header",
        "description": (
            "Parse the invoice header (vendor_name, invoice_date, total_amount, "
            "po_number, department, currency, invoice_number) from raw_input using "
            "pure Python parsers — NO LLM. Writes parsed header to state. "
            "Advances stage to HEADER_PARSED. Returns header dict and confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id": {"type": "string"},
            },
            "required": ["state_id"],
        },
    },
    {
        "name": "parse_invoice_line",
        "description": (
            "Parse a single line item (amount + description) from raw_input. "
            "Amount: pure Python (deterministic). "
            "Description: 1 haiku LLM call (with internal retries). "
            "Returns success, line_item dict, confidence, issues. "
            "success=False with confidence=0.0 means description extraction failed — "
            "agent should add_note and continue (not halt)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id":   {"type": "string"},
                "line_index": {"type": "integer", "description": "0-based line item index"},
            },
            "required": ["state_id", "line_index"],
        },
    },
    {
        "name": "parse_line_description",
        "description": (
            "STATELESS — re-parse a line description directly without touching state. "
            "Use when retrying a low-confidence description parse without re-parsing the amount. "
            "Returns quantity, unit_cost, billing_type, service_period, "
            "category_hint, ambiguity_flags, service_precedes_invoice, reasoning, confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description":  {"type": "string"},
                "amount":       {"type": "string"},
                "invoice_date": {"type": "string"},
                "department":   {"type": "string"},
                "vendor":       {"type": "string"},
            },
            "required": ["description", "amount", "invoice_date", "department", "vendor"],
        },
    },
    {
        "name": "validate_structure",
        "description": (
            "Run structural validators: required_header_fields, line_item_structure, "
            "amount_types. Reads header and line_items from state. "
            "Returns has_errors, has_warnings, issues list. "
            "If has_errors=True, attempt fix via validate_single_rule before proceeding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id": {"type": "string"},
            },
            "required": ["state_id"],
        },
    },
    {
        "name": "validate_business_rules",
        "description": (
            "Run business validators: line_total_matches_header, po_number_present, "
            "invoice_date_not_future, service_periods_sanity. "
            "SKIPS if structural errors are present. "
            "Returns has_errors, has_warnings, issues, flags_added. "
            "AMOUNT_MISMATCH (ERROR) → HALT immediately. Return {halted: true, state_id}. "
            "MISSING_PO (WARNING) → add_note and continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id": {"type": "string"},
            },
            "required": ["state_id"],
        },
    },
    {
        "name": "validate_single_rule",
        "description": (
            "Re-run exactly ONE named rule after fixing a specific field. "
            "Does NOT change stage. "
            "Valid rule_name: 'required_header_fields' | 'line_item_structure' | "
            "'amount_types' | 'line_total_matches_header' | 'po_number_present' | "
            "'invoice_date_not_future' | 'service_periods_sanity'. "
            "Returns has_errors, issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id":  {"type": "string"},
                "rule_name": {"type": "string"},
            },
            "required": ["state_id", "rule_name"],
        },
    },
    {
        "name": "compute_confidence",
        "description": (
            "Compute overall invoice confidence as weighted mean of field_confidences. "
            "Applies penalties for ERROR (-0.15) and WARNING (-0.05) issues. "
            "Returns overall_confidence and breakdown. Call before finalize_invoice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id": {"type": "string"},
            },
            "required": ["state_id"],
        },
    },
    {
        "name": "finalize_invoice",
        "description": (
            "Assemble the final Invoice dict from state. "
            "Sets InvoiceStatus based on flags: "
            "AMOUNT_MISMATCH→FLAGGED_AMOUNT_MISMATCH, MISSING_PO→FLAGGED_NO_PO, "
            "AMBIGUOUS_CATEGORY→FLAGGED_AMBIGUOUS, MISSING_DATA→FLAGGED_MISSING_DATA, "
            "none→READY_FOR_MATCHING, unresolved errors→FAILED. "
            "Advances stage to COMPLETE. Returns invoice dict as contract boundary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id": {"type": "string"},
            },
            "required": ["state_id"],
        },
    },
    {
        "name": "add_note",
        "description": (
            "Record agent reasoning to state.agent_notes. "
            "Call whenever making a non-trivial decision: "
            "retry, flag-and-continue, ambiguity detected, halt. "
            "Notes become processing_notes in the final Invoice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_id": {"type": "string"},
                "note":     {"type": "string"},
            },
            "required": ["state_id", "note"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

_TOOL_FUNCTIONS = {
    "load_invoice_from_file":  lambda inp: load_invoice_from_file(**inp),
    "load_invoice_from_dict":  lambda inp: load_invoice_from_dict(**inp),
    "parse_invoice_header":    lambda inp: parse_invoice_header(**inp),
    "parse_invoice_line":      lambda inp: parse_invoice_line(**inp),
    "parse_line_description":  lambda inp: parse_line_description(**inp),
    "validate_structure":      lambda inp: validate_structure(**inp),
    "validate_business_rules": lambda inp: validate_business_rules(**inp),
    "validate_single_rule":    lambda inp: validate_single_rule(**inp),
    "compute_confidence":      lambda inp: compute_confidence(**inp),
    "finalize_invoice":        lambda inp: finalize_invoice(**inp),
    "add_note":                lambda inp: add_note(**inp),
}


def _dispatch(tool_name: str, tool_input: dict) -> str:
    """Call the appropriate tool function and return JSON string result."""
    fn = _TOOL_FUNCTIONS.get(tool_name)
    if fn is None:
        return json.dumps({"success": False, "error": f"Unknown tool: {tool_name!r}"})
    try:
        result = fn(tool_input)
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are the Invoice Ingestion Agent. Your job is to transform raw vendor
invoice data into a clean, validated, signal-enriched Invoice object ready for the PO Matching
and GL Classification stages.

You call tools step-by-step and make real decisions at each step.

═══════════════════════════════════════════════════════
RESUME PATH (only when resume_state_id is provided):
  Call validate_business_rules(state_id=resume_state_id) to re-run from VALIDATED.
  Skip directly to STEP 5. Use resume_state_id as state_id for all calls.
═══════════════════════════════════════════════════════

STEP 1: LOAD
  If input_type = "file" → call load_invoice_from_file(path=<input>)
  If input_type = "dict" → call load_invoice_from_dict(data=<input>)
  Record state_id from the result. Pass it to every subsequent tool call.
  If structurizer_used=True → call add_note(state_id, "structurizer used: regex split failed")
  If success=False → HALT. Return {"halted": True, "reason": "load_failed", "error": <error>}

STEP 2: PARSE HEADER
  → call parse_invoice_header(state_id)
  For each field with confidence < 0.75: call add_note with field name and confidence.
  For any required field (vendor_name, invoice_date, total_amount) with confidence == 0.0:
    → call add_note, retry parse_invoice_header once (increment retry count internally).
    If still 0.0 after retry → HALT. Return {"halted": True, "reason": "header_parse_failed", "state_id": ...}
  Proceed to STEP 3.

STEP 3: PARSE LINES
  For i = 0 to line_count-1:
    → call parse_invoice_line(state_id, line_index=i)
    If success=False (extraction_failed):
      → call add_note(state_id, "Line {i}: description extraction failed — {issues}")
      → continue to next line (do NOT halt for line failures)
    If confidence < 0.70:
      → call parse_line_description with the raw description text for a retry
      → call add_note with retry result
    If ambiguity_flags is non-empty:
      → call add_note(state_id, "Line {i}: ambiguous — {ambiguity_flags}")
      → continue

STEP 4: STRUCTURAL VALIDATION
  → call validate_structure(state_id)
  If has_errors=True:
    For each ERROR issue: call validate_single_rule(state_id, rule_name=issue.rule_name)
    If error persists after retry: call add_note and proceed (finalize will set FAILED)
  Proceed to STEP 5.

STEP 5: BUSINESS VALIDATION
  → call validate_business_rules(state_id)
  If AMOUNT_MISMATCH is in issues (ERROR):
    → call add_note(state_id, "HALT: AMOUNT_MISMATCH detected")
    → return {"halted": True, "state_id": state_id, "reason": "AMOUNT_MISMATCH"}
    Do NOT call finalize_invoice.
  If MISSING_PO is in issues (WARNING):
    → call add_note(state_id, "Invoice has no PO number — proceeding with FLAGGED_NO_PO status")
  If DATE_FUTURE is in issues (WARNING):
    → call add_note(state_id, "Invoice date is in the future — proceeding with flag")
  Proceed to STEP 6.

STEP 6: FINALIZE
  → call compute_confidence(state_id)
  → call finalize_invoice(state_id)
  → return the invoice dict from finalize_invoice result

═══════════════════════════════════════════════════════
DECISION RULES:

HALT conditions (return {"halted": True, "state_id": ..., "reason": ...}):
  - AMOUNT_MISMATCH ERROR in business validation
  - Required header field at confidence 0.0 after 1 retry
  - Load failure (success=False from load tool)

FLAG-AND-CONTINUE conditions (add_note, keep going):
  - MISSING_PO | confidence < 0.75 | extraction_failed on line | ambiguous category
  - Any WARNING issue

RETRY POLICY:
  - Max 1 retry per tool/rule combination
  - Never retry a logic error (invalid rule_name etc.) — fix the call instead

CONFIDENCE THRESHOLDS:
  ≥ 0.90  → proceed silently
  0.75–0.90 → add_note, proceed
  0.60–0.75 → add_note with flag, proceed with caution
  < 0.60    → add_note, flag as LOW_CONFIDENCE

IMPORTANT:
  - Always call add_note before any HALT
  - Always call compute_confidence before finalize_invoice
  - Never call finalize_invoice after a HALT
  - state_id from the load step must be passed to every subsequent tool call
═══════════════════════════════════════════════════════
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_ingestion_agent(
    input: str | dict,
    resume_state_id: str | None = None,
) -> dict:
    """
    Accept a JSON file path (str) or raw invoice dict.
    Optionally resume from a HITL-paused state (resume_state_id).

    Returns the finalize_invoice() result dict, or a halt dict.
    """
    client = anthropic.Anthropic()
    model  = "claude-sonnet-4-6"   # Orchestrator uses Sonnet; tools use Haiku internally

    # Build the initial user message
    if resume_state_id:
        user_content = (
            f"Resume invoice ingestion from HITL pause. "
            f"state_id: {resume_state_id}. "
            f"Re-run business validation from VALIDATED stage and proceed to finalize."
        )
    elif isinstance(input, str):
        user_content = f"Process this invoice file: {input}"
    else:
        user_content = (
            f"Process this invoice:\n\n{json.dumps(input, default=str, indent=2)}\n\n"
            f"Call load_invoice_from_dict with the data above."
        )

    messages = [{"role": "user", "content": user_content}]

    # Agentic loop
    max_iterations = 50  # safety cap
    for _ in range(max_iterations):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            tools=_TOOLS,
            messages=messages,
        )

        # Append assistant message
        messages.append({"role": "assistant", "content": response.content})

        # Check stop reason
        if response.stop_reason == "end_turn":
            # Agent finished — extract final answer from text blocks
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    # Try to parse JSON from the text
                    try:
                        result = json.loads(text)
                        return result
                    except json.JSONDecodeError:
                        pass
            # No parseable JSON found — return raw text
            return {
                "success": False,
                "error":   "Agent ended without returning a structured result",
                "raw":     str(response.content),
            }

        if response.stop_reason != "tool_use":
            return {
                "success":     False,
                "stop_reason": response.stop_reason,
                "error":       "Unexpected stop reason",
            }

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name  = block.name
            tool_input = block.input

            result_str = _dispatch(tool_name, tool_input)
            result_obj = json.loads(result_str)

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     result_str,
            })

            # Check for halt conditions in tool results
            if tool_name == "validate_business_rules" and result_obj.get("success"):
                for issue in result_obj.get("issues", []):
                    if issue.get("rule_name") == "line_total_matches_header" \
                            and issue.get("severity") == "ERROR":
                        # Let the agent handle the halt decision via its system prompt
                        pass

            # If finalize_invoice succeeded, return the invoice immediately
            if tool_name == "finalize_invoice" and result_obj.get("success"):
                return result_obj

        # Add tool results to messages
        messages.append({"role": "user", "content": tool_results})

    return {
        "success": False,
        "error":   "Agent exceeded maximum iterations without completing",
    }
