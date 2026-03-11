from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PipelineStatus(str, Enum):
    PENDING  = "PENDING"
    RUNNING  = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED   = "FAILED"
    HALTED   = "HALTED"


class StageStatus(str, Enum):
    PENDING  = "PENDING"
    RUNNING  = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED   = "FAILED"
    HALTED   = "HALTED"


class PipelineStage(str, Enum):
    INGESTION         = "INGESTION"
    PO_MATCHING       = "PO_MATCHING"
    GL_CLASSIFICATION = "GL_CLASSIFICATION"
    PREPAID_ACCRUAL   = "PREPAID_ACCRUAL"
    APPROVAL_ROUTING  = "APPROVAL_ROUTING"
    POSTING           = "POSTING"


# Ordered stage sequence — do not reorder without updating orchestrator logic
STAGE_SEQUENCE: list[PipelineStage] = [
    PipelineStage.INGESTION,
    PipelineStage.PO_MATCHING,
    PipelineStage.GL_CLASSIFICATION,
    PipelineStage.PREPAID_ACCRUAL,
    PipelineStage.APPROVAL_ROUTING,
    PipelineStage.POSTING,
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PipelineRun:
    run_id:        str
    invoice_id:    str | None       # None until INGESTION completes
    status:        PipelineStatus
    current_stage: PipelineStage
    created_at:    str              # ISO UTC
    updated_at:    str              # ISO UTC
    metadata:      dict             # {"source_type": str, "source_path": str | None}


@dataclass
class StageResult:
    result_id:      str
    run_id:         str
    stage:          PipelineStage
    status:         StageStatus
    input_payload:  dict
    output_payload: dict | None     # None until stage completes
    halted:         bool
    halt_reason:    str | None
    started_at:     str             # ISO UTC
    completed_at:   str | None      # None until stage finishes


@dataclass
class HaltRecord:
    halt_id:            str
    run_id:             str
    stage:              PipelineStage
    reason:             str
    ingestion_state_id: str | None  # only for INGESTION halts
    correction_input:   dict | None # set on resume
    resolved:           bool
    created_at:         str
    resolved_at:        str | None
