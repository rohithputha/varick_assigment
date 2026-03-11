"""
GlobalStateManager — CRUD operations on all three pipeline tables.
All methods take/return dataclasses; serialization stays inside this class.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from pipeline.db import SQLiteDB
from pipeline.models import (
    HaltRecord,
    PipelineRun,
    PipelineStage,
    PipelineStatus,
    StageResult,
    StageStatus,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class GlobalStateManager:
    """
    CRUD operations on all three pipeline tables.
    All methods accept/return dataclasses; JSON serialization is internal.
    """

    def __init__(self, db: SQLiteDB) -> None:
        self._db = db

    # -----------------------------------------------------------------------
    # pipeline_runs
    # -----------------------------------------------------------------------

    def create_run(
        self,
        first_stage: PipelineStage,
        metadata: dict,
    ) -> PipelineRun:
        run_id = str(uuid.uuid4())
        now    = _now_utc()
        run    = PipelineRun(
            run_id=run_id,
            invoice_id=None,
            status=PipelineStatus.RUNNING,
            current_stage=first_stage,
            created_at=now,
            updated_at=now,
            metadata=metadata,
        )
        self._db.connect().execute(
            """INSERT INTO pipeline_runs
               (run_id, invoice_id, status, current_stage, created_at, updated_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, None, run.status.value, run.current_stage.value,
             now, now, json.dumps(metadata)),
        )
        self._db.connect().commit()
        return run

    def get_run(self, run_id: str) -> PipelineRun:
        row = self._db.connect().execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"run_id not found: {run_id!r}")
        return PipelineRun(
            run_id=row["run_id"],
            invoice_id=row["invoice_id"],
            status=PipelineStatus(row["status"]),
            current_stage=PipelineStage(row["current_stage"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata"]),
        )

    def update_run_status(
        self,
        run_id: str,
        status: PipelineStatus,
        current_stage: PipelineStage | None = None,
        invoice_id: str | None = None,
    ) -> None:
        """
        Partial update — only changes what is explicitly passed.
        Always updates updated_at.
        """
        conn = self._db.connect()
        row  = conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"run_id not found: {run_id!r}")

        new_stage      = current_stage.value if current_stage else row["current_stage"]
        new_invoice_id = invoice_id if invoice_id is not None else row["invoice_id"]

        conn.execute(
            """UPDATE pipeline_runs
               SET status=?, current_stage=?, invoice_id=?, updated_at=?
               WHERE run_id=?""",
            (status.value, new_stage, new_invoice_id, _now_utc(), run_id),
        )
        conn.commit()

    def list_runs(
        self,
        status: PipelineStatus | None = None,
        limit: int = 50,
    ) -> list[PipelineRun]:
        """Return runs ordered by created_at DESC, optionally filtered by status."""
        conn = self._db.connect()
        if status:
            rows = conn.execute(
                "SELECT * FROM pipeline_runs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pipeline_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            PipelineRun(
                run_id=r["run_id"],
                invoice_id=r["invoice_id"],
                status=PipelineStatus(r["status"]),
                current_stage=PipelineStage(r["current_stage"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                metadata=json.loads(r["metadata"]),
            )
            for r in rows
        ]

    # -----------------------------------------------------------------------
    # stage_results
    # -----------------------------------------------------------------------

    def create_stage_result(
        self,
        run_id: str,
        stage: PipelineStage,
        input_payload: dict,
    ) -> StageResult:
        result_id = str(uuid.uuid4())
        now       = _now_utc()
        result    = StageResult(
            result_id=result_id,
            run_id=run_id,
            stage=stage,
            status=StageStatus.RUNNING,
            input_payload=input_payload,
            output_payload=None,
            halted=False,
            halt_reason=None,
            started_at=now,
            completed_at=None,
        )
        self._db.connect().execute(
            """INSERT INTO stage_results
               (result_id, run_id, stage, status, input_payload,
                output_payload, halted, halt_reason, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, ?, NULL)""",
            (result_id, run_id, stage.value, StageStatus.RUNNING.value,
             json.dumps(input_payload, default=str), now),
        )
        self._db.connect().commit()
        return result

    def complete_stage_result(
        self,
        run_id: str,
        stage: PipelineStage,
        output_payload: dict,
    ) -> None:
        self._db.connect().execute(
            """UPDATE stage_results
               SET status=?, output_payload=?, completed_at=?
               WHERE run_id=? AND stage=?""",
            (StageStatus.COMPLETE.value,
             json.dumps(output_payload, default=str),
             _now_utc(), run_id, stage.value),
        )
        self._db.connect().commit()

    def halt_stage_result(
        self,
        run_id: str,
        stage: PipelineStage,
        halt_reason: str,
        output_payload: dict | None = None,
    ) -> None:
        self._db.connect().execute(
            """UPDATE stage_results
               SET status=?, halted=1, halt_reason=?, output_payload=?, completed_at=?
               WHERE run_id=? AND stage=?""",
            (StageStatus.HALTED.value, halt_reason,
             json.dumps(output_payload, default=str) if output_payload else None,
             _now_utc(), run_id, stage.value),
        )
        self._db.connect().commit()

    def fail_stage_result(
        self,
        run_id: str,
        stage: PipelineStage,
        error: str,
        output_payload: dict | None = None,
    ) -> None:
        self._db.connect().execute(
            """UPDATE stage_results
               SET status=?, halt_reason=?, output_payload=?, completed_at=?
               WHERE run_id=? AND stage=?""",
            (StageStatus.FAILED.value, error,
             json.dumps(output_payload, default=str) if output_payload else None,
             _now_utc(), run_id, stage.value),
        )
        self._db.connect().commit()

    def get_stage_result(self, run_id: str, stage: PipelineStage) -> StageResult | None:
        row = self._db.connect().execute(
            "SELECT * FROM stage_results WHERE run_id=? AND stage=?",
            (run_id, stage.value),
        ).fetchone()
        if row is None:
            return None
        return StageResult(
            result_id=row["result_id"],
            run_id=row["run_id"],
            stage=PipelineStage(row["stage"]),
            status=StageStatus(row["status"]),
            input_payload=json.loads(row["input_payload"]),
            output_payload=json.loads(row["output_payload"]) if row["output_payload"] else None,
            halted=bool(row["halted"]),
            halt_reason=row["halt_reason"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def list_stage_results(self, run_id: str) -> list[StageResult]:
        rows = self._db.connect().execute(
            "SELECT * FROM stage_results WHERE run_id=? ORDER BY started_at",
            (run_id,),
        ).fetchall()
        return [
            StageResult(
                result_id=r["result_id"],
                run_id=r["run_id"],
                stage=PipelineStage(r["stage"]),
                status=StageStatus(r["status"]),
                input_payload=json.loads(r["input_payload"]),
                output_payload=json.loads(r["output_payload"]) if r["output_payload"] else None,
                halted=bool(r["halted"]),
                halt_reason=r["halt_reason"],
                started_at=r["started_at"],
                completed_at=r["completed_at"],
            )
            for r in rows
        ]

    # -----------------------------------------------------------------------
    # halt_records
    # -----------------------------------------------------------------------

    def create_halt_record(
        self,
        run_id: str,
        stage: PipelineStage,
        reason: str,
        ingestion_state_id: str | None = None,
    ) -> HaltRecord:
        halt_id = str(uuid.uuid4())
        now     = _now_utc()
        self._db.connect().execute(
            """INSERT INTO halt_records
               (halt_id, run_id, stage, reason, ingestion_state_id,
                correction_input, resolved, created_at, resolved_at)
               VALUES (?, ?, ?, ?, ?, NULL, 0, ?, NULL)""",
            (halt_id, run_id, stage.value, reason, ingestion_state_id, now),
        )
        self._db.connect().commit()
        return HaltRecord(
            halt_id=halt_id,
            run_id=run_id,
            stage=stage,
            reason=reason,
            ingestion_state_id=ingestion_state_id,
            correction_input=None,
            resolved=False,
            created_at=now,
            resolved_at=None,
        )

    def resolve_halt_record(
        self,
        run_id: str,
        correction_input: dict | None = None,
    ) -> None:
        """Mark the active (unresolved) halt record for a run as resolved."""
        now = _now_utc()
        self._db.connect().execute(
            """UPDATE halt_records
               SET resolved=1, resolved_at=?, correction_input=?
               WHERE run_id=? AND resolved=0""",
            (now,
             json.dumps(correction_input, default=str) if correction_input else None,
             run_id),
        )
        self._db.connect().commit()

    def get_active_halt(self, run_id: str) -> HaltRecord | None:
        """Return the most recent unresolved halt record for a run."""
        row = self._db.connect().execute(
            """SELECT * FROM halt_records
               WHERE run_id=? AND resolved=0
               ORDER BY created_at DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return HaltRecord(
            halt_id=row["halt_id"],
            run_id=row["run_id"],
            stage=PipelineStage(row["stage"]),
            reason=row["reason"],
            ingestion_state_id=row["ingestion_state_id"],
            correction_input=json.loads(row["correction_input"]) if row["correction_input"] else None,
            resolved=bool(row["resolved"]),
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )
