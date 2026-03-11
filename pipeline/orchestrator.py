"""
Orchestrator — outer pipeline coordinator with SQLite-backed global state.

Sequences all six stages, handles HALT signals, stores full audit trail,
and provides resume capability for HITL corrections.
"""
from __future__ import annotations

import json

from pipeline.db import SQLiteDB
from pipeline.models import (
    PipelineStage,
    PipelineStatus,
    STAGE_SEQUENCE,
    StageStatus,
)
from pipeline.state_manager import GlobalStateManager
from pipeline.stages.ingestion   import IngestionRunner
from pipeline.stages.po_matching import POMatchingRunner
from pipeline.stages.stubs       import (
    GLClassificationRunner,
    PrepaidAccrualRunner,
    ApprovalRoutingRunner,
    PostingRunner,
)


# ---------------------------------------------------------------------------
# Stage runner registry — one instance per stage, ordered by STAGE_SEQUENCE
# ---------------------------------------------------------------------------

_RUNNERS: dict[PipelineStage, object] = {
    PipelineStage.INGESTION:         IngestionRunner(),
    PipelineStage.PO_MATCHING:       POMatchingRunner(),
    PipelineStage.GL_CLASSIFICATION: GLClassificationRunner(),
    PipelineStage.PREPAID_ACCRUAL:   PrepaidAccrualRunner(),
    PipelineStage.APPROVAL_ROUTING:  ApprovalRoutingRunner(),
    PipelineStage.POSTING:           PostingRunner(),
}


class Orchestrator:
    """
    Outer pipeline orchestrator with SQLite-backed global state.

    Usage:
        db = SQLiteDB()
        db.create_tables()
        orch = Orchestrator(db)

        # New run
        result = orch.run("/path/to/invoice.json")
        result = orch.run({"vendor_name": "Acme", ...})

        # Resume a halted run
        result = orch.resume(run_id, corrected_input={"vendor_name": "Acme", ...})

        # Query status
        status = orch.get_status(run_id)
    """

    def __init__(self, db: SQLiteDB) -> None:
        self._db  = db
        self._gsm = GlobalStateManager(db)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self, input: str | dict) -> dict:
        """
        Start a new pipeline run.

        Args:
            input: File path (str) or raw invoice dict.

        Returns:
            On success:
                {"run_id": str, "status": "COMPLETE", "invoice_id": str, "output": dict}
            On halt:
                {"run_id": str, "status": "HALTED", "stage": str,
                 "reason": str, "halt_id": str}
            On failure:
                {"run_id": str, "status": "FAILED", "stage": str, "error": str}
        """
        metadata = {
            "source_type": "file" if isinstance(input, str) else "dict",
            "source_path": input  if isinstance(input, str) else None,
        }
        run = self._gsm.create_run(STAGE_SEQUENCE[0], metadata)

        # INGESTION receives the raw input wrapped in a payload envelope
        stage_input = {"input": input}

        return self._execute_from(run.run_id, PipelineStage.INGESTION, stage_input)

    def resume(
        self,
        run_id: str,
        corrected_input: dict | None = None,
    ) -> dict:
        """
        Resume a halted pipeline run.

        Args:
            run_id:          ID from a previous run() or resume() call.
            corrected_input: Corrected invoice data for INGESTION HITL resume.
                             Pass None to resume without correction
                             (e.g., re-run from same state after external fix).

        Returns:
            Same shape as run().
        """
        run = self._gsm.get_run(run_id)

        if run.status != PipelineStatus.HALTED:
            return {
                "run_id": run_id,
                "error":  f"Run is not halted (current status: {run.status.value})",
            }

        halt = self._gsm.get_active_halt(run_id)
        if halt is None:
            return {
                "run_id": run_id,
                "error":  "No active halt record found for this run",
            }

        # Mark the halt as resolved (records corrected_input for audit trail)
        self._gsm.resolve_halt_record(run_id, correction_input=corrected_input)

        halted_stage = halt.stage

        if halted_stage == PipelineStage.INGESTION:
            # HITL resume: re-run ingestion with corrected data + resume_state_id
            # so the ingestion agent skips LOAD/PARSE and re-runs from VALIDATED.
            #
            # If corrected_input is None, re-run with original input from stage_results
            # (the ingestion agent will re-run from the existing halted state).
            stage_result   = self._gsm.get_stage_result(run_id, PipelineStage.INGESTION)
            original_input = stage_result.input_payload if stage_result else {}

            if corrected_input is not None:
                resume_payload = {
                    "input":           corrected_input,
                    "resume_state_id": halt.ingestion_state_id,
                }
            else:
                resume_payload = {
                    "input":           original_input.get("input", {}),
                    "resume_state_id": halt.ingestion_state_id,
                }
        else:
            # Future stages: resume with corrected input or prior stage output
            stage_idx = STAGE_SEQUENCE.index(halted_stage)
            if stage_idx > 0:
                prior_stage  = STAGE_SEQUENCE[stage_idx - 1]
                prior_result = self._gsm.get_stage_result(run_id, prior_stage)
                resume_payload = corrected_input or (
                    prior_result.output_payload if prior_result else {}
                )
            else:
                resume_payload = corrected_input or {}

        # Update run back to RUNNING at the halted stage
        self._gsm.update_run_status(
            run_id,
            PipelineStatus.RUNNING,
            current_stage=halted_stage,
        )

        return self._execute_from(run_id, halted_stage, resume_payload)

    def get_status(self, run_id: str) -> dict:
        """
        Return current run status with full stage breakdown.

        Returns:
            {
                "run_id":        str,
                "invoice_id":    str | None,
                "status":        str,
                "current_stage": str,
                "created_at":    str,
                "updated_at":    str,
                "metadata":      dict,
                "stages": [
                    {
                        "stage":        str,
                        "status":       str,
                        "halted":       bool,
                        "halt_reason":  str | None,
                        "started_at":   str,
                        "completed_at": str | None,
                    },
                    ...
                ],
                "active_halt": {
                    "halt_id":            str,
                    "stage":              str,
                    "reason":             str,
                    "ingestion_state_id": str | None,
                    "created_at":         str,
                } | None,
            }
        """
        run          = self._gsm.get_run(run_id)
        stage_results = self._gsm.list_stage_results(run_id)
        active_halt  = self._gsm.get_active_halt(run_id)

        return {
            "run_id":        run.run_id,
            "invoice_id":    run.invoice_id,
            "status":        run.status.value,
            "current_stage": run.current_stage.value,
            "created_at":    run.created_at,
            "updated_at":    run.updated_at,
            "metadata":      run.metadata,
            "stages": [
                {
                    "stage":        sr.stage.value,
                    "status":       sr.status.value,
                    "halted":       sr.halted,
                    "halt_reason":  sr.halt_reason,
                    "started_at":   sr.started_at,
                    "completed_at": sr.completed_at,
                }
                for sr in stage_results
            ],
            "active_halt": {
                "halt_id":            active_halt.halt_id,
                "stage":              active_halt.stage.value,
                "reason":             active_halt.reason,
                "ingestion_state_id": active_halt.ingestion_state_id,
                "created_at":         active_halt.created_at,
            } if active_halt else None,
        }

    # -----------------------------------------------------------------------
    # Internal execution engine
    # -----------------------------------------------------------------------

    def _execute_from(
        self,
        run_id: str,
        start_stage: PipelineStage,
        start_input: dict,
    ) -> dict:
        """
        Execute pipeline stages starting at start_stage with start_input.
        Advances through STAGE_SEQUENCE until HALT, FAILED, or COMPLETE.

        On resume, the halted stage_results row is updated in-place
        (HALTED → COMPLETE) rather than creating a new row.
        """
        stage_idx     = STAGE_SEQUENCE.index(start_stage)
        current_input = start_input

        for stage in STAGE_SEQUENCE[stage_idx:]:
            runner = _RUNNERS[stage]

            # Record stage start — insert or skip if row exists (resume case)
            existing = self._gsm.get_stage_result(run_id, stage)
            if existing is None:
                self._gsm.create_stage_result(run_id, stage, current_input)
            # else: row already exists from the original halted attempt — reuse it

            self._gsm.update_run_status(
                run_id, PipelineStatus.RUNNING, current_stage=stage
            )

            # Execute the stage
            result = runner.run(run_id, current_input)

            # ── Unexpected error (success=False, halted=False) ──────────────
            if not result.get("halted", False) and not result.get("success", True):
                error_msg = result.get("error", "unknown_error")
                self._gsm.fail_stage_result(
                    run_id, stage,
                    error=error_msg,
                    output_payload=result,
                )
                self._gsm.update_run_status(
                    run_id, PipelineStatus.FAILED, current_stage=stage
                )
                return {
                    "run_id": run_id,
                    "status": "FAILED",
                    "stage":  stage.value,
                    "error":  error_msg,
                }

            # ── HALT signal ─────────────────────────────────────────────────
            if result.get("halted", False):
                halt_reason        = result.get("reason", "unknown")
                ingestion_state_id = result.get("state_id")   # only for INGESTION halts

                self._gsm.halt_stage_result(
                    run_id, stage,
                    halt_reason=halt_reason,
                    output_payload=result,
                )
                halt = self._gsm.create_halt_record(
                    run_id=run_id,
                    stage=stage,
                    reason=halt_reason,
                    ingestion_state_id=ingestion_state_id,
                )
                self._gsm.update_run_status(
                    run_id, PipelineStatus.HALTED, current_stage=stage
                )
                return {
                    "run_id":  run_id,
                    "status":  "HALTED",
                    "stage":   stage.value,
                    "reason":  halt_reason,
                    "halt_id": halt.halt_id,
                }

            # ── Stage complete ───────────────────────────────────────────────
            self._gsm.complete_stage_result(run_id, stage, result)

            # After INGESTION: extract and store invoice_id on the run record
            if stage == PipelineStage.INGESTION:
                invoice_id = _extract_invoice_id(result)
                if invoice_id:
                    self._gsm.update_run_status(
                        run_id,
                        PipelineStatus.RUNNING,
                        current_stage=stage,
                        invoice_id=invoice_id,
                    )

            # Output of this stage becomes the input of the next
            current_input = result

        # ── All stages complete ──────────────────────────────────────────────
        self._gsm.update_run_status(
            run_id,
            PipelineStatus.COMPLETE,
            current_stage=STAGE_SEQUENCE[-1],
        )
        return {
            "run_id":     run_id,
            "status":     "COMPLETE",
            "invoice_id": self._gsm.get_run(run_id).invoice_id,
            "output":     current_input,
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _extract_invoice_id(ingestion_result: dict) -> str | None:
    """
    Pull invoice_id from the ingestion result dict.

    finalize_invoice returns the invoice nested under result["invoice"]["invoice_id"].
    The agent may also return it at the top level.
    """
    # Top-level key
    if "invoice_id" in ingestion_result:
        return ingestion_result["invoice_id"]
    # Nested under invoice dict
    invoice = ingestion_result.get("invoice")
    if isinstance(invoice, dict):
        return invoice.get("invoice_id")
    return None
