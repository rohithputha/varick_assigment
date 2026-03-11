"""
Shadow Mode — ShadowRunner, ShadowProposal, LineProposal.

ShadowRunner.run_batch(invoice_inputs) runs each invoice through the full pipeline
(dry_run=True, stops before Posting) and captures proposals for human review.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pipeline.db import SQLiteDB
from pipeline.models import PipelineStage
from pipeline.orchestrator import Orchestrator
from pipeline.state_manager import GlobalStateManager


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LineProposal:
    line_number:          int
    description:          str
    amount:               float
    category_hint:        str
    gl_account:           str
    treatment:            str           # EXPENSE | PREPAID | CAPITALIZE | ACCRUAL
    base_expense_account: str | None
    confidence:           float
    flags:                list[str]
    amortization_months:  int | None    # PREPAID only
    monthly_amount:       str | None    # PREPAID only (string to preserve decimal precision)
    accrual_account:      str | None    # ACCRUAL only
    expense_account:      str | None    # ACCRUAL only


@dataclass
class ShadowProposal:
    proposal_id:       str
    invoice_id:        str
    run_id:            str
    vendor:            str
    invoice_total:     float
    invoice_date:      str
    po_number:         str | None
    po_status:         str
    line_proposals:    list[LineProposal]
    approval_proposal: str
    applied_rule:      str | None
    reasoning:         str | None
    flags:             list[str]
    notes:             list[str]
    review_status:     str = "PENDING"
    reviewer_id:       str | None = None
    reviewed_at:       str | None = None
    corrections:       list[dict] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_header_value(header: dict, field_name: str) -> Any:
    """Extract value from a header field that may be a ParsedField dict or a scalar."""
    val = header.get(field_name)
    if isinstance(val, dict):
        return val.get("value")
    return val


def _extract_line_description(invoice: dict, line_number: int) -> str:
    for li in invoice.get("line_items", []):
        if li.get("line_number") == line_number:
            desc = li.get("description") or ""
            if isinstance(desc, dict):
                return str(desc.get("value") or "")
            return str(desc)
    return ""


def _extract_line_amount(invoice: dict, line_number: int) -> float:
    for li in invoice.get("line_items", []):
        if li.get("line_number") == line_number:
            amt = li.get("amount") or 0
            if isinstance(amt, dict):
                return float(amt.get("value") or 0)
            try:
                return float(amt)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _extract_category_hint(invoice: dict, line_number: int) -> str:
    """Extract category_hint from the parsed_description of a line item."""
    for li in invoice.get("line_items", []):
        if li.get("line_number") == line_number:
            parsed = li.get("parsed_description") or {}
            if isinstance(parsed, dict):
                return parsed.get("category_hint", "unknown") or "unknown"
    return "unknown"


def _proposal_to_dict(proposal: ShadowProposal) -> dict:
    """Convert ShadowProposal (with nested LineProposal list) to a plain dict for DB storage."""
    return {
        "proposal_id":       proposal.proposal_id,
        "invoice_id":        proposal.invoice_id,
        "run_id":            proposal.run_id,
        "vendor":            proposal.vendor,
        "invoice_total":     proposal.invoice_total,
        "po_status":         proposal.po_status,
        "line_proposals":    [vars(lp) for lp in proposal.line_proposals],
        "approval_proposal": proposal.approval_proposal,
        "applied_rule":      proposal.applied_rule,
        "reasoning":         proposal.reasoning,
        "flags":             proposal.flags,
        "notes":             proposal.notes,
        "review_status":     proposal.review_status,
        "reviewer_id":       proposal.reviewer_id,
        "reviewed_at":       proposal.reviewed_at,
        "corrections":       proposal.corrections,
        "created_at":        datetime.now(timezone.utc).isoformat(),
    }


def dict_to_proposal(d: dict) -> ShadowProposal:
    """Convert a plain dict (from DB) back to a ShadowProposal."""
    line_proposals = [
        LineProposal(**lp) if isinstance(lp, dict) else lp
        for lp in (d.get("line_proposals") or [])
    ]
    return ShadowProposal(
        proposal_id=d["proposal_id"],
        invoice_id=d["invoice_id"],
        run_id=d["run_id"],
        vendor=d.get("vendor", ""),
        invoice_total=d.get("invoice_total", 0.0),
        invoice_date=d.get("invoice_date", ""),
        po_number=d.get("po_number"),
        po_status=d.get("po_status", "UNKNOWN"),
        line_proposals=line_proposals,
        approval_proposal=d.get("approval_proposal", "UNKNOWN"),
        applied_rule=d.get("applied_rule"),
        reasoning=d.get("reasoning"),
        flags=d.get("flags", []),
        notes=d.get("notes", []),
        review_status=d.get("review_status", "PENDING"),
        reviewer_id=d.get("reviewer_id"),
        reviewed_at=d.get("reviewed_at"),
        corrections=d.get("corrections"),
    )


# ---------------------------------------------------------------------------
# ShadowRunner
# ---------------------------------------------------------------------------

class ShadowRunner:
    """
    Runs invoices in shadow mode (dry_run=True — no Posting) and captures
    structured proposals for human review.
    """

    def __init__(self, db: SQLiteDB) -> None:
        self._db  = db
        self._gsm = GlobalStateManager(db)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run_batch(self, invoice_inputs: list[dict]) -> list[ShadowProposal]:
        """Run each invoice through the pipeline and return all proposals."""
        proposals: list[ShadowProposal] = []
        total = len(invoice_inputs)
        for i, inv_input in enumerate(invoice_inputs, 1):
            inv_id = inv_input.get("invoice_id", f"#{i}")
            print(f"  [{i}/{total}] {inv_id} ...", flush=True)
            try:
                p = self.run_one(inv_input)
                proposals.append(p)
            except Exception as e:
                print(f"    ERROR: {e}")
        return proposals

    def run_one(self, invoice_input: dict) -> ShadowProposal:
        """
        Run one invoice through the pipeline (dry_run=True).
        Extracts structured proposal from the APPROVAL_ROUTING stage output.
        Persists to shadow_proposals table and returns ShadowProposal.
        """
        orch   = Orchestrator(self._db)
        result = orch.run(invoice_input, dry_run=True)
        run_id = result.get("run_id", "")

        # Retrieve the full APPROVAL_ROUTING stage output from DB
        ar_sr      = self._gsm.get_stage_result(run_id, PipelineStage.APPROVAL_ROUTING)
        routed     = ar_sr.output_payload if ar_sr else {}

        proposal = self._extract_proposal(run_id, routed, invoice_input, result)

        # Persist to DB
        self._gsm.create_shadow_proposal(_proposal_to_dict(proposal))
        return proposal

    def get_pending_proposals(self) -> list[ShadowProposal]:
        return [dict_to_proposal(d) for d in self._gsm.list_pending_proposals()]

    def get_proposal(self, proposal_id: str) -> ShadowProposal | None:
        d = self._gsm.get_shadow_proposal(proposal_id)
        return dict_to_proposal(d) if d else None

    def submit_corrections(
        self,
        proposal_id: str,
        corrections: list[dict],
        reviewer_id: str,
    ) -> None:
        """Mark a proposal as REVIEWED (no corrections) or CORRECTED."""
        review_status = "CORRECTED" if corrections else "REVIEWED"
        self._gsm.update_shadow_review(
            proposal_id,
            review_status=review_status,
            reviewer_id=reviewer_id,
            corrections=corrections if corrections else None,
        )

    # -----------------------------------------------------------------------
    # Private extraction logic
    # -----------------------------------------------------------------------

    def _extract_proposal(
        self,
        run_id: str,
        routed: dict,
        invoice_input: dict,
        orch_result: dict,
    ) -> ShadowProposal:
        """Extract a ShadowProposal from the RoutedInvoice dict."""
        recognized   = routed.get("recognized_invoice", {})
        classified   = recognized.get("classified_invoice", {})
        invoice      = classified.get("invoice", {})
        header       = invoice.get("header", {})
        line_results = recognized.get("line_results", [])
        line_cls     = classified.get("line_classifications", [])
        routing      = routed.get("routing", {})

        # Invoice-level fields
        vendor        = str(_extract_header_value(header, "vendor_name") or
                             invoice_input.get("vendor_name", "Unknown"))
        invoice_total_raw = _extract_header_value(header, "total_amount")
        try:
            invoice_total = float(invoice_total_raw or invoice_input.get("total_amount", 0))
        except (TypeError, ValueError):
            invoice_total = 0.0

        invoice_date = str(_extract_header_value(header, "invoice_date") or
                           invoice_input.get("invoice_date", ""))
        po_number    = _extract_header_value(header, "po_number") or invoice_input.get("po_number")
        invoice_id   = (invoice.get("invoice_id") or
                        _extract_header_value(header, "invoice_id") or
                        invoice_input.get("invoice_id", "UNKNOWN"))

        # PO status
        po_match  = classified.get("po_match", {})
        if isinstance(po_match, dict):
            po_status = po_match.get("status", "UNKNOWN")
        else:
            po_status = str(po_match) if po_match else "UNKNOWN"

        # Routing fields
        approval_proposal = str(routing.get("outcome") or orch_result.get("outcome", "UNKNOWN"))
        applied_rule      = routing.get("applied_rule")
        reasoning         = routing.get("reasoning")

        # Notes and flags
        all_notes = list(routed.get("notes", [])) + list(recognized.get("notes", []))
        flags     = [n for n in all_notes if any(
            kw in n.upper() for kw in ("FLAG", "WARN", "ERROR", "MISSING", "MISMATCH")
        )]

        # Build per-line proposals
        lr_by_ln = {lr.get("line_number"): lr for lr in line_results}
        line_proposals: list[LineProposal] = []

        for lc in line_cls:
            ln            = lc.get("line_number", 0)
            lr            = lr_by_ln.get(ln, {})
            prepaid_res   = lr.get("prepaid_result")
            accrual_res   = lr.get("accrual_result")
            final_tmt     = lr.get("final_treatment") or lc.get("treatment", "EXPENSE")
            final_gl      = lr.get("final_gl_account") or lc.get("gl_account", "")
            category_hint = _extract_category_hint(invoice, ln) or lc.get("category_hint", "unknown")

            # Amortization
            amort_months = None
            monthly_amt  = None
            if prepaid_res:
                amort_months = prepaid_res.get("amortization_months")
                entries = prepaid_res.get("amortization_entries") or []
                if entries:
                    monthly_amt = entries[0].get("amount")

            line_proposals.append(LineProposal(
                line_number=ln,
                description=_extract_line_description(invoice, ln),
                amount=_extract_line_amount(invoice, ln),
                category_hint=category_hint,
                gl_account=final_gl,
                treatment=final_tmt,
                base_expense_account=lc.get("base_expense_account"),
                confidence=lc.get("confidence", 0.0),
                flags=[lc["flag_reason"]] if lc.get("flag_reason") else [],
                amortization_months=amort_months,
                monthly_amount=monthly_amt,
                accrual_account=accrual_res.get("accrual_account") if accrual_res else None,
                expense_account=accrual_res.get("expense_account") if accrual_res else None,
            ))

        return ShadowProposal(
            proposal_id=str(uuid.uuid4()),
            invoice_id=str(invoice_id),
            run_id=run_id,
            vendor=vendor,
            invoice_total=invoice_total,
            invoice_date=invoice_date,
            po_number=str(po_number) if po_number else None,
            po_status=po_status,
            line_proposals=line_proposals,
            approval_proposal=approval_proposal,
            applied_rule=applied_rule,
            reasoning=reasoning,
            flags=flags,
            notes=all_notes,
        )
