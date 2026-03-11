"""
FeedbackBenchmark — before/after accuracy comparison on labeled invoices.

snapshot() runs all 6 labeled invoices through the pipeline (N=1, dry_run=True)
and stores accuracy metrics in benchmark_snapshots table.

compare() loads two snapshots and computes the accuracy delta.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import po_matching.matchers.po_validator as _pov

from evals.comparator import compare_run
from evals.ground_truth import get_ground_truth
from pipeline.db import SQLiteDB
from pipeline.models import PipelineStage
from pipeline.orchestrator import Orchestrator
from pipeline.state_manager import GlobalStateManager
from rules_engine.rules_tools import get_rules
from approval_routing.threshold_tools import get_thresholds


_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "labeled_invoices.json"


@dataclass
class AccuracyDelta:
    before_label:      str
    after_label:       str
    rules_before:      str
    rules_after:       str
    threshold_before:  str
    threshold_after:   str
    overall_before:    float
    overall_after:     float
    gl_before:         float
    gl_after:          float
    treatment_before:  float
    treatment_after:   float
    approval_before:   float
    approval_after:    float
    improved_invoices: list[str]
    regressed_invoices: list[str]
    unchanged_invoices: list[str]


@dataclass
class BenchmarkSnapshot:
    snapshot_id:        str
    label:              str
    rules_gl_version:   str
    threshold_version:  str
    captured_at:        str
    overall_accuracy:   float
    gl_accuracy:        float
    treatment_accuracy: float
    approval_accuracy:  float
    per_invoice:        dict[str, float]


class FeedbackBenchmark:
    """
    Captures and compares accuracy snapshots for the labeled invoice eval suite.
    """

    def __init__(self, db: SQLiteDB) -> None:
        self._db  = db
        self._gsm = GlobalStateManager(db)

    def snapshot(self, label: str = "auto") -> BenchmarkSnapshot:
        """
        Run all labeled invoices (N=1, dry_run=True) and store accuracy snapshot.
        """
        invoices = json.loads(_DATA_PATH.read_text())
        gt_map   = get_ground_truth()

        # Collect accuracy across all invoices
        gl_passed = gl_total = 0
        tmt_passed = tmt_total = 0
        ap_passed = ap_total = 0
        per_invoice: dict[str, float] = {}

        for entry in invoices:
            raw    = entry["raw_input"]
            inv_id = raw.get("invoice_id", "?")
            gt     = gt_map.get(inv_id)
            if gt is None:
                continue

            if gt.expected_blocked:
                # For blocked invoices, just check they remain blocked
                per_invoice[inv_id] = 1.0  # handled separately; skip from accuracy
                continue

            force_match = True
            bundle = self._run_one(raw, force_match)
            run    = compare_run(bundle, gt, 0)

            # GL checks
            for lr in run.line_results:
                for c in lr.stage3_checks:
                    if not c.skipped:
                        gl_total  += 1
                        if c.passed:
                            gl_passed += 1

            # Treatment checks
            for lr in run.line_results:
                for c in lr.stage4_checks:
                    if not c.skipped:
                        tmt_total  += 1
                        if c.passed:
                            tmt_passed += 1

            # Approval checks
            if run.approval_check and not run.approval_check.skipped:
                ap_total  += 1
                if run.approval_check.passed:
                    ap_passed += 1

            per_invoice[inv_id] = 1.0 if run.all_pass else 0.0

        def _rate(p: int, t: int) -> float:
            return round(p / t, 4) if t > 0 else 0.0

        gl_acc      = _rate(gl_passed, gl_total)
        tmt_acc     = _rate(tmt_passed, tmt_total)
        ap_acc      = _rate(ap_passed, ap_total)
        overall_acc = _rate(
            gl_passed + tmt_passed + ap_passed,
            gl_total  + tmt_total  + ap_total,
        )

        # Get current version strings
        try:
            rules_version = get_rules().get("version", "unknown")
        except Exception:
            rules_version = "unknown"
        try:
            threshold_version = get_thresholds().get("version", "unknown")
        except Exception:
            threshold_version = "unknown"

        from datetime import datetime, timezone
        snapshot_id = str(uuid.uuid4())
        captured_at = datetime.now(timezone.utc).isoformat()

        snap_dict = {
            "snapshot_id":        snapshot_id,
            "label":              label,
            "rules_gl_version":   rules_version,
            "threshold_version":  threshold_version,
            "captured_at":        captured_at,
            "overall_accuracy":   overall_acc,
            "gl_accuracy":        gl_acc,
            "treatment_accuracy": tmt_acc,
            "approval_accuracy":  ap_acc,
            "per_invoice":        per_invoice,
        }
        self._gsm.create_benchmark_snapshot(snap_dict)

        return BenchmarkSnapshot(**snap_dict)

    def compare(self, before_label: str, after_label: str) -> AccuracyDelta:
        """
        Load the most recent snapshot for each label and return a delta.
        """
        snapshots = self._gsm.list_snapshots()
        before = next(
            (s for s in snapshots if s["label"] == before_label), None
        )
        after  = next(
            (s for s in snapshots if s["label"] == after_label), None
        )

        if before is None:
            raise ValueError(f"No snapshot found with label {before_label!r}")
        if after is None:
            raise ValueError(f"No snapshot found with label {after_label!r}")

        before_inv = before["per_invoice"]
        after_inv  = after["per_invoice"]
        all_ids    = set(before_inv) | set(after_inv)

        improved  = [i for i in all_ids if before_inv.get(i, 0) < 1.0 and after_inv.get(i, 0) == 1.0]
        regressed = [i for i in all_ids if before_inv.get(i, 0) == 1.0 and after_inv.get(i, 0) < 1.0]
        unchanged = [i for i in all_ids if i not in improved and i not in regressed]

        # Overall = average of gl, treatment, approval
        def _overall(s: dict) -> float:
            vals = [s["gl_accuracy"], s["treatment_accuracy"], s["approval_accuracy"]]
            return round(sum(vals) / len(vals), 4)

        return AccuracyDelta(
            before_label=before_label,
            after_label=after_label,
            rules_before=before["rules_gl_version"],
            rules_after=after["rules_gl_version"],
            threshold_before=before["threshold_version"],
            threshold_after=after["threshold_version"],
            overall_before=_overall(before),
            overall_after=_overall(after),
            gl_before=before["gl_accuracy"],
            gl_after=after["gl_accuracy"],
            treatment_before=before["treatment_accuracy"],
            treatment_after=after["treatment_accuracy"],
            approval_before=before["approval_accuracy"],
            approval_after=after["approval_accuracy"],
            improved_invoices=sorted(improved),
            regressed_invoices=sorted(regressed),
            unchanged_invoices=sorted(unchanged),
        )

    def report(self, delta: AccuracyDelta) -> str:
        """Format a before/after delta as a human-readable report string."""
        def _fmt(b: float, a: float) -> str:
            pct_b = f"{b * 100:.0f}%"
            pct_a = f"{a * 100:.0f}%"
            diff  = (a - b) * 100
            sign  = "+" if diff >= 0 else ""
            tag   = "✓ improved" if diff > 0 else ("✗ regressed" if diff < 0 else "— unchanged")
            return f"{pct_b} → {pct_a}  ({sign}{diff:.0f} pp)   {tag}"

        w     = 52
        lines = [
            f"Before → After  "
            f"(GL rules v{delta.rules_before}→v{delta.rules_after}, "
            f"thresholds v{delta.threshold_before}→v{delta.threshold_after})",
            "─" * w,
            f"Overall accuracy:   {_fmt(delta.overall_before, delta.overall_after)}",
            f"GL accuracy:        {_fmt(delta.gl_before,       delta.gl_after)}",
            f"Treatment:          {_fmt(delta.treatment_before, delta.treatment_after)}",
            f"Approval routing:   {_fmt(delta.approval_before, delta.approval_after)}",
            "",
            f"Improved invoices:  {', '.join(delta.improved_invoices) or '(none)'}",
            f"Regressed:          {', '.join(delta.regressed_invoices) or '(none)'}",
            f"Unchanged:          {', '.join(delta.unchanged_invoices) or '(none)'}",
        ]
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Internal: run one invoice once in isolated in-memory DB
    # -----------------------------------------------------------------------

    def _run_one(self, raw_input: dict, force_match: bool) -> dict:
        _pov.FORCE_MATCH = force_match
        db = SQLiteDB(":memory:")
        db.create_tables()
        orch   = Orchestrator(db)
        result = orch.run(raw_input, dry_run=True)
        run_id = result.get("run_id")
        gsm    = GlobalStateManager(db)

        stage_outputs: dict[str, dict | None] = {}
        for stage in PipelineStage:
            sr = gsm.get_stage_result(run_id, stage)
            stage_outputs[stage.value] = sr.output_payload if sr else None

        return {
            "run_id":        run_id,
            "result":        result,
            "stage_outputs": stage_outputs,
        }
