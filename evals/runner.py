"""
Eval runner — executes the pipeline for a single invoice and returns
per-stage output payloads alongside the top-level orchestrator result.

Each call to run_invoice_once() creates a fresh in-memory SQLite DB so
that runs are fully isolated from each other.

FORCE_MATCH handling:
  - INV-001..005: FORCE_MATCH=True  (PO validation bypassed; GL classification runs)
  - INV-006:      FORCE_MATCH=False (real NO_PO behavior is what we test)
"""
from __future__ import annotations

import po_matching.matchers.po_validator as _pov

from pipeline.db import SQLiteDB
from pipeline.models import PipelineStage
from pipeline.orchestrator import Orchestrator
from pipeline.state_manager import GlobalStateManager


def run_invoice_once(raw_input: dict, force_match: bool) -> dict:
    """
    Run the full pipeline (dry_run=True — stops after Approval Routing) for
    one invoice and return a bundle containing the orchestrator result plus
    per-stage output payloads retrieved from the DB.

    Returns:
        {
            "run_id":        str,
            "result":        dict,   # orch.run() return value
            "stage_outputs": {       # keyed by stage name string
                "GL_CLASSIFICATION": dict | None,
                "PREPAID_ACCRUAL":   dict | None,
                "APPROVAL_ROUTING":  dict | None,
                "PO_MATCHING":       dict | None,
                "INGESTION":         dict | None,
            },
        }
    """
    _pov.FORCE_MATCH = force_match

    db = SQLiteDB(":memory:")
    db.create_tables()
    orch = Orchestrator(db)

    result = orch.run(raw_input, dry_run=True)
    run_id = result.get("run_id")

    gsm = GlobalStateManager(db)
    stage_outputs: dict[str, dict | None] = {}

    for stage in PipelineStage:
        sr = gsm.get_stage_result(run_id, stage)
        stage_outputs[stage.value] = sr.output_payload if sr else None

    return {
        "run_id":        run_id,
        "result":        result,
        "stage_outputs": stage_outputs,
    }


def run_invoice_n_times(raw_input: dict, force_match: bool, n: int) -> list[dict]:
    """Run run_invoice_once n times and return the list of bundles."""
    return [run_invoice_once(raw_input, force_match) for _ in range(n)]
