"""
Shadow mode CLI.

Usage:
    # Process unlabeled invoices in shadow mode
    python -m pipeline.cli_shadow --batch data/unlabeled_invoices.json

    # Submit corrections after editing the review file
    python -m pipeline.cli_shadow --submit reports/shadow_review_20260311_143052_edited.json

    # Interactive review (terminal-based, secondary path)
    python -m pipeline.cli_shadow --review
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline.db import SQLiteDB
from pipeline.shadow import ShadowRunner, dict_to_proposal
from pipeline.shadow_report import format_proposal_summary, generate_review_file
from pipeline.state_manager import GlobalStateManager


_DB_PATH = Path(__file__).parent.parent / "pipeline.db"
_SEPARATOR = "─" * 65


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db() -> SQLiteDB:
    db = SQLiteDB(_DB_PATH)
    db.create_tables()
    return db


def _infer_stage(field: str) -> str:
    _map = {
        "gl_account":           "GL_CLASSIFICATION",
        "treatment":            "GL_CLASSIFICATION",
        "base_expense_account": "GL_CLASSIFICATION",
        "amortization_months":  "PREPAID_ACCRUAL",
        "monthly_amount":       "PREPAID_ACCRUAL",
        "accrual_account":      "PREPAID_ACCRUAL",
        "expense_account":      "PREPAID_ACCRUAL",
        "approval_outcome":     "APPROVAL_ROUTING",
    }
    return _map.get(field, "GL_CLASSIFICATION")


# ---------------------------------------------------------------------------
# --batch
# ---------------------------------------------------------------------------

def cmd_batch(batch_file: str) -> None:
    batch_path = Path(batch_file)
    if not batch_path.exists():
        print(f"ERROR: file not found: {batch_path}")
        sys.exit(1)

    invoice_inputs = json.loads(batch_path.read_text())

    # Support both raw arrays and labeled_invoices.json-style {"raw_input": {...}}
    if invoice_inputs and isinstance(invoice_inputs[0], dict) and "raw_input" in invoice_inputs[0]:
        invoice_inputs = [item["raw_input"] for item in invoice_inputs]

    total = len(invoice_inputs)
    print(f"\nProcessing {total} invoices in shadow mode (no posting)...")
    print(_SEPARATOR)

    db      = _get_db()
    runner  = ShadowRunner(db)
    proposals = runner.run_batch(invoice_inputs)

    print(_SEPARATOR)
    for p in proposals:
        print(format_proposal_summary(p))
    print(_SEPARATOR)

    if not proposals:
        print("No proposals generated.")
        return

    review_path = generate_review_file(proposals)
    print(f"\nReview file saved → {review_path}")
    print("Edit corrected_value fields where you disagree, then run:")
    print(f"  python -m pipeline.cli_shadow --submit {review_path}")


# ---------------------------------------------------------------------------
# --submit
# ---------------------------------------------------------------------------

def cmd_submit(review_file: str) -> None:
    review_path = Path(review_file)
    if not review_path.exists():
        print(f"ERROR: file not found: {review_path}")
        sys.exit(1)

    doc = json.loads(review_path.read_text())
    proposals_data = doc.get("proposals", [])

    db           = _get_db()
    gsm          = GlobalStateManager(db)
    runner       = ShadowRunner(db)
    reviewer_id  = "cli_reviewer"
    corrections_saved = 0
    proposals_read    = 0

    print(f"\nIngesting corrections from {review_path.name}...")

    for p_entry in proposals_data:
        proposals_read += 1
        proposal_id = p_entry.get("proposal_id", "")
        invoice_id  = p_entry.get("invoice_id", "")

        # Collect all non-null corrections from lines
        line_corrections_for_submit: list[dict] = []

        for line_entry in p_entry.get("lines", []):
            line_number = line_entry.get("line_number")
            for corr in line_entry.get("corrections", []):
                if corr.get("corrected_value") is None:
                    continue
                field           = corr["field"]
                proposed_value  = str(corr.get("proposed_value") or "")
                corrected_value = str(corr["corrected_value"])
                reason          = corr.get("reason")

                feedback_id = str(uuid.uuid4())
                gsm.create_feedback_record({
                    "feedback_id":       feedback_id,
                    "proposal_id":       proposal_id,
                    "invoice_id":        invoice_id,
                    "reviewer_id":       reviewer_id,
                    "stage":             _infer_stage(field),
                    "field":             field,
                    "line_number":       line_number,
                    "proposed_value":    proposed_value,
                    "corrected_value":   corrected_value,
                    "correction_reason": reason,
                    "created_at":        datetime.now(timezone.utc).isoformat(),
                })
                corrections_saved += 1
                line_corrections_for_submit.append({
                    "field":           field,
                    "line_number":     line_number,
                    "proposed_value":  proposed_value,
                    "corrected_value": corrected_value,
                    "reason":          reason,
                })
                print(f"  {invoice_id}  correction: {field} "
                      f"{proposed_value!r}→{corrected_value!r}"
                      + (f"  (reason: {reason!r})" if reason else ""))

        # Approval-level corrections
        approval_entry = p_entry.get("approval", {})
        for corr in approval_entry.get("corrections", []):
            if corr.get("corrected_value") is None:
                continue
            field           = corr["field"]
            proposed_value  = str(corr.get("proposed_value") or "")
            corrected_value = str(corr["corrected_value"])
            reason          = corr.get("reason")

            feedback_id = str(uuid.uuid4())
            gsm.create_feedback_record({
                "feedback_id":       feedback_id,
                "proposal_id":       proposal_id,
                "invoice_id":        invoice_id,
                "reviewer_id":       reviewer_id,
                "stage":             "APPROVAL_ROUTING",
                "field":             field,
                "line_number":       None,
                "proposed_value":    proposed_value,
                "corrected_value":   corrected_value,
                "correction_reason": reason,
                "created_at":        datetime.now(timezone.utc).isoformat(),
            })
            corrections_saved += 1
            line_corrections_for_submit.append({
                "field":           field,
                "line_number":     None,
                "proposed_value":  proposed_value,
                "corrected_value": corrected_value,
                "reason":          reason,
            })
            print(f"  {invoice_id}  correction: {field} "
                  f"{proposed_value!r}→{corrected_value!r}"
                  + (f"  (reason: {reason!r})" if reason else ""))

        if not line_corrections_for_submit:
            print(f"  {invoice_id}  accepted (no corrections)")

        # Mark proposal reviewed/corrected
        runner.submit_corrections(
            proposal_id=proposal_id,
            corrections=line_corrections_for_submit,
            reviewer_id=reviewer_id,
        )

    print(f"\nSaved {corrections_saved} corrections from {proposals_read} proposals.")
    if corrections_saved:
        print("Run `python -m evals.feedback.cli --analyze` to see error patterns.")


# ---------------------------------------------------------------------------
# --review (interactive)
# ---------------------------------------------------------------------------

def cmd_review() -> None:
    db     = _get_db()
    runner = ShadowRunner(db)
    pending = runner.get_pending_proposals()

    if not pending:
        print("No pending proposals to review.")
        return

    print(f"\n{len(pending)} pending proposal(s) to review.\n")

    for p in pending:
        print(f"\n{'═' * 65}")
        print(f"  {p.invoice_id}  |  {p.vendor}  |  ${p.invoice_total:,.2f}")
        print(f"  PO: {p.po_status}  |  Approval: {p.approval_proposal}")
        print(f"{'─' * 65}")
        for lp in p.line_proposals:
            print(f"  Line {lp.line_number}: {lp.description[:50]}")
            print(f"    GL: {lp.gl_account}  Treatment: {lp.treatment}  conf={lp.confidence:.2f}")

        print("\nAccept this proposal? [Y/n] ", end="", flush=True)
        choice = input().strip().lower()

        corrections: list[dict] = []
        if choice == "n":
            while True:
                print("Field to correct (or blank to stop): ", end="", flush=True)
                field = input().strip()
                if not field:
                    break
                print(f"Line number (blank for invoice-level): ", end="", flush=True)
                ln_str = input().strip()
                line_number = int(ln_str) if ln_str.isdigit() else None
                print(f"Proposed value for {field!r}: ", end="", flush=True)
                proposed = input().strip()
                print(f"Corrected value: ", end="", flush=True)
                corrected = input().strip()
                print(f"Reason (optional): ", end="", flush=True)
                reason = input().strip() or None
                corrections.append({
                    "field":           field,
                    "line_number":     line_number,
                    "proposed_value":  proposed,
                    "corrected_value": corrected,
                    "reason":          reason,
                })

        runner.submit_corrections(
            proposal_id=p.proposal_id,
            corrections=corrections,
            reviewer_id="interactive_reviewer",
        )
        print("  Saved." if corrections else "  Accepted.")

    print("\nAll proposals reviewed.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shadow mode — run invoices without posting and collect feedback"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--batch",  metavar="FILE",
                       help="Run all invoices in FILE (JSON array) through shadow mode")
    group.add_argument("--submit", metavar="FILE",
                       help="Submit an edited review JSON file to ingest corrections")
    group.add_argument("--review", action="store_true",
                       help="Interactive terminal-based review of pending proposals")

    args = parser.parse_args()

    if args.batch:
        cmd_batch(args.batch)
    elif args.submit:
        cmd_submit(args.submit)
    elif args.review:
        cmd_review()


if __name__ == "__main__":
    main()
