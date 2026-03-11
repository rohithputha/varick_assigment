"""
ShadowReport — generate a human-editable review JSON file from a batch of ShadowProposals.

The reviewer fills in corrected_value fields where the pipeline was wrong,
then submits the file via cli_shadow --submit.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline.shadow import LineProposal, ShadowProposal


_REPORTS_DIR = Path(__file__).parent.parent / "reports"


# ---------------------------------------------------------------------------
# Stage mapping — infer which pipeline stage each field belongs to
# ---------------------------------------------------------------------------

_FIELD_STAGE_MAP = {
    "gl_account":           "GL_CLASSIFICATION",
    "treatment":            "GL_CLASSIFICATION",
    "base_expense_account": "GL_CLASSIFICATION",
    "amortization_months":  "PREPAID_ACCRUAL",
    "monthly_amount":       "PREPAID_ACCRUAL",
    "accrual_account":      "PREPAID_ACCRUAL",
    "expense_account":      "PREPAID_ACCRUAL",
    "approval_outcome":     "APPROVAL_ROUTING",
}


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_review_file(
    proposals: list[ShadowProposal],
    batch_id: str | None = None,
) -> Path:
    """
    Write a shadow_review_{ts}.json file to reports/ and return the path.

    The file contains per-line correction slots (corrected_value=null) for the reviewer
    to fill in where they disagree with the pipeline's proposal.
    """
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    ts      = now_utc.strftime("%Y%m%d_%H%M%S")
    if batch_id is None:
        batch_id = f"batch_{ts}"

    doc = {
        "batch_id":     batch_id,
        "generated_at": now_utc.isoformat(),
        "instructions": (
            "For each line: leave corrected_value as null if the proposal is correct. "
            "Fill in corrected_value (and optional reason) where you disagree."
        ),
        "proposals": [_proposal_to_review_entry(p) for p in proposals],
    }

    out_path = _REPORTS_DIR / f"shadow_review_{ts}.json"
    out_path.write_text(json.dumps(doc, indent=2, default=str))
    return out_path


def _proposal_to_review_entry(p: ShadowProposal) -> dict:
    """Convert one ShadowProposal into its review JSON entry."""
    return {
        "proposal_id":       p.proposal_id,
        "invoice_id":        p.invoice_id,
        "vendor":            p.vendor,
        "invoice_total":     p.invoice_total,
        "invoice_date":      p.invoice_date,
        "po_number":         p.po_number,
        "po_status":         p.po_status,
        "flags":             p.flags,
        "lines":             [_line_to_review_entry(lp) for lp in p.line_proposals],
        "approval": {
            "corrections": [
                {
                    "field":           "approval_outcome",
                    "proposed_value":  p.approval_proposal,
                    "corrected_value": None,
                    "reason":          None,
                }
            ]
        },
    }


def _line_to_review_entry(lp: LineProposal) -> dict:
    """Build a line entry with empty correction slots."""
    corrections: list[dict] = []

    # Always include GL-level fields
    corrections.append({
        "field":           "gl_account",
        "proposed_value":  lp.gl_account,
        "corrected_value": None,
        "reason":          None,
    })
    corrections.append({
        "field":           "treatment",
        "proposed_value":  lp.treatment,
        "corrected_value": None,
        "reason":          None,
    })
    if lp.base_expense_account is not None:
        corrections.append({
            "field":           "base_expense_account",
            "proposed_value":  lp.base_expense_account,
            "corrected_value": None,
            "reason":          None,
        })

    # PREPAID-specific fields
    if lp.treatment == "PREPAID" or lp.amortization_months is not None:
        corrections.append({
            "field":           "amortization_months",
            "proposed_value":  lp.amortization_months,
            "corrected_value": None,
            "reason":          None,
        })

    # ACCRUAL-specific fields
    if lp.treatment == "ACCRUAL" or lp.accrual_account is not None:
        corrections.append({
            "field":           "accrual_account",
            "proposed_value":  lp.accrual_account,
            "corrected_value": None,
            "reason":          None,
        })
        corrections.append({
            "field":           "expense_account",
            "proposed_value":  lp.expense_account,
            "corrected_value": None,
            "reason":          None,
        })

    return {
        "line_number": lp.line_number,
        "description": lp.description,
        "amount":      lp.amount,
        "category_hint": lp.category_hint,
        "confidence":  lp.confidence,
        "corrections": corrections,
    }


# ---------------------------------------------------------------------------
# Summary line for CLI display
# ---------------------------------------------------------------------------

def format_proposal_summary(p: ShadowProposal) -> str:
    """Return one-line summary string for CLI display."""
    treatments = []
    for lp in p.line_proposals:
        treatments.append(lp.treatment)
    tmt_counts: dict[str, int] = {}
    for t in treatments:
        tmt_counts[t] = tmt_counts.get(t, 0) + 1
    tmt_str = " + ".join(
        f"{t} ×{c}" if c > 1 else t
        for t, c in tmt_counts.items()
    )
    avg_conf = (
        sum(lp.confidence for lp in p.line_proposals) / len(p.line_proposals)
        if p.line_proposals else 0.0
    )
    return (
        f"  {p.invoice_id:<8}  {p.vendor:<25}  "
        f"${p.invoice_total:>10,.0f}   {p.approval_proposal:<14}  "
        f"{tmt_str}   conf={avg_conf:.2f}"
    )
