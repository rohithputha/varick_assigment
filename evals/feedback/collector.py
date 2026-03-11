"""
FeedbackCollector — ingests corrections from submitted review JSON files
into the feedback_records table.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json

from pipeline.db import SQLiteDB
from pipeline.state_manager import GlobalStateManager


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


@dataclass
class FeedbackRecord:
    feedback_id:       str
    proposal_id:       str
    invoice_id:        str
    reviewer_id:       str
    stage:             str
    field:             str
    line_number:       int | None
    proposed_value:    str
    corrected_value:   str
    correction_reason: str | None
    applied:           bool
    created_at:        str


@dataclass
class IngestResult:
    proposals_read:      int
    corrections_saved:   int
    proposals_corrected: int


class FeedbackCollector:
    """
    Ingests corrections from a submitted shadow review JSON file into the
    feedback_records table.
    """

    def __init__(self, db: SQLiteDB) -> None:
        self._db  = db
        self._gsm = GlobalStateManager(db)

    # -----------------------------------------------------------------------
    # Primary entry point
    # -----------------------------------------------------------------------

    def ingest_review_file(self, review_file_path: str) -> IngestResult:
        """
        Read an edited review JSON file, extract non-null corrected_value entries,
        write feedback_records rows, and mark proposals REVIEWED or CORRECTED.

        Returns summary stats.
        """
        path = Path(review_file_path)
        doc  = json.loads(path.read_text())

        proposals_data      = doc.get("proposals", [])
        proposals_read      = 0
        corrections_saved   = 0
        proposals_corrected = 0
        reviewer_id = doc.get("reviewer_id", "file_reviewer")

        for p_entry in proposals_data:
            proposals_read += 1
            proposal_id = p_entry.get("proposal_id", "")
            invoice_id  = p_entry.get("invoice_id", "")
            line_corrections: list[dict] = []

            # Per-line corrections
            for line_entry in p_entry.get("lines", []):
                ln = line_entry.get("line_number")
                for corr in line_entry.get("corrections", []):
                    if corr.get("corrected_value") is None:
                        continue
                    field    = corr["field"]
                    record   = self._build_record(
                        proposal_id=proposal_id,
                        invoice_id=invoice_id,
                        reviewer_id=reviewer_id,
                        field=field,
                        line_number=ln,
                        proposed_value=str(corr.get("proposed_value") or ""),
                        corrected_value=str(corr["corrected_value"]),
                        reason=corr.get("reason"),
                    )
                    self._gsm.create_feedback_record(record)
                    corrections_saved += 1
                    line_corrections.append({
                        "field":           field,
                        "line_number":     ln,
                        "proposed_value":  str(corr.get("proposed_value") or ""),
                        "corrected_value": str(corr["corrected_value"]),
                        "reason":          corr.get("reason"),
                    })

            # Approval-level corrections
            for corr in p_entry.get("approval", {}).get("corrections", []):
                if corr.get("corrected_value") is None:
                    continue
                field  = corr["field"]
                record = self._build_record(
                    proposal_id=proposal_id,
                    invoice_id=invoice_id,
                    reviewer_id=reviewer_id,
                    field=field,
                    line_number=None,
                    proposed_value=str(corr.get("proposed_value") or ""),
                    corrected_value=str(corr["corrected_value"]),
                    reason=corr.get("reason"),
                )
                self._gsm.create_feedback_record(record)
                corrections_saved += 1
                line_corrections.append({
                    "field":           field,
                    "line_number":     None,
                    "proposed_value":  str(corr.get("proposed_value") or ""),
                    "corrected_value": str(corr["corrected_value"]),
                    "reason":          corr.get("reason"),
                })

            # Mark proposal reviewed/corrected
            if line_corrections:
                proposals_corrected += 1
                self._gsm.update_shadow_review(
                    proposal_id,
                    review_status="CORRECTED",
                    reviewer_id=reviewer_id,
                    corrections=line_corrections,
                )
            else:
                self._gsm.update_shadow_review(
                    proposal_id,
                    review_status="REVIEWED",
                    reviewer_id=reviewer_id,
                )

        return IngestResult(
            proposals_read=proposals_read,
            corrections_saved=corrections_saved,
            proposals_corrected=proposals_corrected,
        )

    # -----------------------------------------------------------------------
    # Query methods
    # -----------------------------------------------------------------------

    def get_all_corrections(self, since: str | None = None) -> list[FeedbackRecord]:
        rows = self._gsm.get_feedback_records(since=since)
        return [_row_to_record(r) for r in rows]

    def get_corrections_by_field(self, field: str) -> list[FeedbackRecord]:
        rows = self._gsm.get_feedback_records(field=field)
        return [_row_to_record(r) for r in rows]

    def mark_applied(self, feedback_ids: list[str]) -> None:
        self._gsm.mark_feedback_applied(feedback_ids)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _build_record(
        self,
        proposal_id: str,
        invoice_id: str,
        reviewer_id: str,
        field: str,
        line_number: int | None,
        proposed_value: str,
        corrected_value: str,
        reason: str | None,
    ) -> dict:
        return {
            "feedback_id":       str(uuid.uuid4()),
            "proposal_id":       proposal_id,
            "invoice_id":        invoice_id,
            "reviewer_id":       reviewer_id,
            "stage":             _FIELD_STAGE_MAP.get(field, "GL_CLASSIFICATION"),
            "field":             field,
            "line_number":       line_number,
            "proposed_value":    proposed_value,
            "corrected_value":   corrected_value,
            "correction_reason": reason,
            "created_at":        datetime.now(timezone.utc).isoformat(),
        }


def _row_to_record(row: dict) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=row["feedback_id"],
        proposal_id=row["proposal_id"],
        invoice_id=row["invoice_id"],
        reviewer_id=row["reviewer_id"],
        stage=row["stage"],
        field=row["field"],
        line_number=row.get("line_number"),
        proposed_value=row["proposed_value"],
        corrected_value=row["corrected_value"],
        correction_reason=row.get("correction_reason"),
        applied=bool(row.get("applied", 0)),
        created_at=row["created_at"],
    )
