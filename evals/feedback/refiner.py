"""
RuleRefiner — translates systematic error patterns into concrete rule changes,
and applies approved changes via rules_tools / threshold_tools.

Also provides simulate() for in-memory accuracy delta before applying.
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.feedback.analyzer import ErrorPattern, ErrorPatternReport
from rules_engine.rules_tools import get_rules, update_rule, add_rule, disable_rule, add_normalisation
from approval_routing.threshold_tools import get_thresholds, update_threshold


@dataclass
class RuleChange:
    change_id:   str
    rule_system: str           # "gl" | "approval_threshold"
    change_type: str           # UPDATE_GL | UPDATE_TREATMENT | UPDATE_BASE |
                               # ADD_NORMALISATION | DISABLE_RULE | UPDATE_THRESHOLD
    rule_id:     str | None    # rule_id in rules.json, or threshold key name
    field:       str
    old_value:   Any
    new_value:   Any
    rationale:   str
    based_on:    list[str]     # feedback_ids
    frequency:   int

    # The actual api_call this translates to:
    api_call:    str


@dataclass
class ApplyResult:
    applied:  list[str]   # change_ids successfully applied
    failed:   list[str]   # change_ids that failed
    errors:   dict[str, str]  # change_id → error message


@dataclass
class SimulationResult:
    gl_accuracy_before:       float
    gl_accuracy_after:        float
    treatment_accuracy_before: float
    treatment_accuracy_after: float
    approval_accuracy_before: float
    approval_accuracy_after:  float
    improved_invoices: list[str]
    regressed_invoices: list[str]
    per_invoice_before: dict[str, float]
    per_invoice_after:  dict[str, float]


# ---------------------------------------------------------------------------
# RuleRefiner
# ---------------------------------------------------------------------------

class RuleRefiner:
    """
    Translates systematic error patterns into RuleChange proposals,
    previews them, simulates their effect, and applies approved ones.
    """

    def __init__(self) -> None:
        pass

    def propose_changes(self, report: ErrorPatternReport) -> list[RuleChange]:
        """
        Translate each systematic error pattern into a RuleChange.
        Does NOT write anything — returns proposals only.
        """
        changes: list[RuleChange] = []
        seen: dict[tuple, RuleChange] = {}   # (rule_id, field) → existing change (conflict detection)

        for pattern in report.systematic_errors:
            change = self._pattern_to_change(pattern)
            if change is None:
                continue

            # Conflict detection: same (rule_id, field) targeted by a different correction
            key = (change.rule_id, change.field)
            if key in seen:
                existing = seen[key]
                if existing.new_value != change.new_value:
                    # Conflict — keep most-frequent, note conflict
                    if change.frequency > existing.frequency:
                        change.rationale += (
                            f" [CONFLICT with change {existing.change_id}: "
                            f"other correction said {existing.new_value!r}; "
                            f"this change has higher frequency so wins]"
                        )
                        seen[key] = change
                        # Remove the old change and replace with this one
                        changes = [c for c in changes if c.change_id != existing.change_id]
                        changes.append(change)
                else:
                    # Same correction — just extend based_on
                    existing.based_on.extend(change.based_on)
                    existing.frequency = max(existing.frequency, change.frequency)
                continue
            else:
                seen[key] = change
                changes.append(change)

        return changes

    def preview(self, changes: list[RuleChange]) -> str:
        """Human-readable diff of proposed changes."""
        if not changes:
            return "(no changes proposed)"
        lines = [f"Generated {len(changes)} rule change(s):"]
        for c in changes:
            lines.append(f"  [{c.change_id[:6]}] {c.change_type}: {c.api_call}")
            lines.append(f"          {c.field}: {c.old_value!r} → {c.new_value!r}")
            lines.append(f"          rationale: {c.rationale}")
        return "\n".join(lines)

    def apply(self, changes: list[RuleChange], feedback_ids: list[str]) -> ApplyResult:
        """
        Execute each change by calling rules_tools or threshold_tools.
        Mark feedback_records.applied=1 for all feedback_ids when done.
        """
        applied: list[str] = []
        failed:  list[str] = []
        errors:  dict[str, str] = {}

        for change in changes:
            try:
                result = self._execute_change(change)
                if result.get("success", True):
                    applied.append(change.change_id)
                else:
                    failed.append(change.change_id)
                    errors[change.change_id] = result.get("error", "unknown error")
            except Exception as e:
                failed.append(change.change_id)
                errors[change.change_id] = str(e)

        return ApplyResult(applied=applied, failed=failed, errors=errors)

    def simulate(self, changes: list[RuleChange]) -> SimulationResult:
        """
        Apply changes to an in-memory rules copy, re-run labeled evals,
        and return accuracy delta. Does NOT touch disk.
        """
        import po_matching.matchers.po_validator as _pov
        from pipeline.db import SQLiteDB
        from pipeline.orchestrator import Orchestrator
        from pipeline.models import PipelineStage
        from pipeline.state_manager import GlobalStateManager
        from evals.ground_truth import get_ground_truth
        from evals.comparator import compare_run
        import rules_engine.rules_tools as rrt

        _DATA_PATH = Path(__file__).parent.parent.parent / "data" / "labeled_invoices.json"
        invoices = json.loads(_DATA_PATH.read_text())
        gt_map   = get_ground_truth()

        # Build modified rules config in memory
        original_rules  = get_rules()
        modified_rules  = copy.deepcopy(original_rules)
        for change in changes:
            if change.rule_system == "gl":
                _apply_change_to_config(change, modified_rules)

        def _run_once_with_config(raw_input: dict, rules_cfg: dict, force_match: bool) -> dict:
            _pov.FORCE_MATCH = force_match
            original_get_rules = rrt.get_rules
            rrt.get_rules = lambda: rules_cfg
            try:
                db = SQLiteDB(":memory:")
                db.create_tables()
                orch   = Orchestrator(db)
                result = orch.run(raw_input, dry_run=True)
                run_id = result.get("run_id")
                gsm    = GlobalStateManager(db)
                stage_outputs: dict[str, Any] = {}
                for stage in PipelineStage:
                    sr = gsm.get_stage_result(run_id, stage)
                    stage_outputs[stage.value] = sr.output_payload if sr else None
                return {"run_id": run_id, "result": result, "stage_outputs": stage_outputs}
            finally:
                rrt.get_rules = original_get_rules

        # Run both before and after for each labeled invoice
        before_gl = before_tmt = before_ap = 0
        after_gl  = after_tmt  = after_ap  = 0
        total_gl  = total_tmt  = total_ap  = 0

        per_before: dict[str, float] = {}
        per_after:  dict[str, float] = {}
        improved:   list[str] = []
        regressed:  list[str] = []

        for entry in invoices:
            raw    = entry["raw_input"]
            inv_id = raw.get("invoice_id", "?")
            gt     = gt_map.get(inv_id)
            if gt is None or gt.expected_blocked:
                continue

            force_match = True

            bundle_before = _run_once_with_config(raw, original_rules, force_match)
            bundle_after  = _run_once_with_config(raw, modified_rules, force_match)

            run_before = compare_run(bundle_before, gt, 0)
            run_after  = compare_run(bundle_after,  gt, 0)

            # Tally GL checks
            for lr in run_before.line_results:
                for c in lr.stage3_checks:
                    if not c.skipped:
                        total_gl += 1
                        if c.passed:
                            before_gl += 1
            for lr in run_after.line_results:
                for c in lr.stage3_checks:
                    if not c.skipped:
                        if c.passed:
                            after_gl += 1

            # Tally treatment checks
            for lr in run_before.line_results:
                for c in lr.stage4_checks:
                    if not c.skipped:
                        total_tmt += 1
                        if c.passed:
                            before_tmt += 1
            for lr in run_after.line_results:
                for c in lr.stage4_checks:
                    if not c.skipped:
                        if c.passed:
                            after_tmt += 1

            # Tally approval checks
            if run_before.approval_check and not run_before.approval_check.skipped:
                total_ap += 1
                if run_before.approval_check.passed:
                    before_ap += 1
            if run_after.approval_check and not run_after.approval_check.skipped:
                if run_after.approval_check.passed:
                    after_ap += 1

            # Per-invoice pass rate
            all_before = run_before.all_pass
            all_after  = run_after.all_pass
            per_before[inv_id] = 1.0 if all_before else 0.0
            per_after[inv_id]  = 1.0 if all_after  else 0.0

            if not all_before and all_after:
                improved.append(inv_id)
            elif all_before and not all_after:
                regressed.append(inv_id)

        def _rate(passed: int, total: int) -> float:
            return round(passed / total, 4) if total > 0 else 0.0

        return SimulationResult(
            gl_accuracy_before=_rate(before_gl, total_gl),
            gl_accuracy_after=_rate(after_gl, total_gl),
            treatment_accuracy_before=_rate(before_tmt, total_tmt),
            treatment_accuracy_after=_rate(after_tmt, total_tmt),
            approval_accuracy_before=_rate(before_ap, total_ap),
            approval_accuracy_after=_rate(after_ap, total_ap),
            improved_invoices=improved,
            regressed_invoices=regressed,
            per_invoice_before=per_before,
            per_invoice_after=per_after,
        )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _pattern_to_change(self, pattern: ErrorPattern) -> RuleChange | None:
        """Translate a single ErrorPattern into a RuleChange."""
        rule_id    = pattern.affected_rule_id
        field      = pattern.field
        old_val    = pattern.proposed_value
        new_val    = pattern.corrected_value
        freq       = pattern.frequency
        rationale  = (
            f"Systematic error: {field} {old_val!r}→{new_val!r} "
            f"on {freq} invoice(s): {', '.join(pattern.affected_invoices)}"
        )

        # GL account correction
        if field == "gl_account" and rule_id:
            change_type = "UPDATE_GL"
            api_call    = f"update_rule({rule_id!r}, 'gl_account', {new_val!r})"
            return RuleChange(
                change_id=str(uuid.uuid4())[:8],
                rule_system="gl",
                change_type=change_type,
                rule_id=rule_id,
                field=field,
                old_value=old_val,
                new_value=new_val,
                rationale=rationale,
                based_on=[],
                frequency=freq,
                api_call=api_call,
            )

        # Treatment correction
        if field == "treatment" and rule_id:
            change_type = "UPDATE_TREATMENT"
            api_call    = f"update_rule({rule_id!r}, 'treatment', {new_val!r})"
            return RuleChange(
                change_id=str(uuid.uuid4())[:8],
                rule_system="gl",
                change_type=change_type,
                rule_id=rule_id,
                field=field,
                old_value=old_val,
                new_value=new_val,
                rationale=rationale,
                based_on=[],
                frequency=freq,
                api_call=api_call,
            )

        # Base expense account correction
        if field == "base_expense_account" and rule_id:
            change_type = "UPDATE_BASE"
            api_call    = f"update_rule({rule_id!r}, 'base_expense_account', {new_val!r})"
            return RuleChange(
                change_id=str(uuid.uuid4())[:8],
                rule_system="gl",
                change_type=change_type,
                rule_id=rule_id,
                field=field,
                old_value=old_val,
                new_value=new_val,
                rationale=rationale,
                based_on=[],
                frequency=freq,
                api_call=api_call,
            )

        # Approval threshold correction (approval_outcome: VP_FINANCE → DEPT_MANAGER)
        if field == "approval_outcome":
            # Try to infer threshold adjustment
            change_type = "UPDATE_THRESHOLD"
            # Map outcome transition to threshold key heuristic
            threshold_key = "dept_manager_max"
            if old_val == "VP_FINANCE" and new_val == "DEPT_MANAGER":
                threshold_key = "dept_manager_max"
            elif old_val == "DEPT_MANAGER" and new_val == "AUTO_APPROVE":
                threshold_key = "auto_approve_max"

            try:
                current = get_thresholds()["thresholds"].get(threshold_key, 0)
            except Exception:
                current = 0

            api_call = f"update_threshold({threshold_key!r}, <new_value>)"
            return RuleChange(
                change_id=str(uuid.uuid4())[:8],
                rule_system="approval_threshold",
                change_type=change_type,
                rule_id=threshold_key,
                field=field,
                old_value=str(current),
                new_value="<requires_human_judgment>",
                rationale=rationale + f" [threshold {threshold_key}={current}]",
                based_on=[],
                frequency=freq,
                api_call=api_call,
            )

        return None

    def _execute_change(self, change: RuleChange) -> dict:
        """Execute a single RuleChange via the appropriate API."""
        if change.rule_system == "gl":
            if change.change_type == "DISABLE_RULE":
                return disable_rule(change.rule_id)
            elif change.change_type == "ADD_NORMALISATION":
                parts = change.field.split(":")
                return add_normalisation(parts[0], change.new_value, parts[1] if len(parts) > 1 else "category")
            else:
                return update_rule(change.rule_id, change.field, change.new_value)
        elif change.rule_system == "approval_threshold":
            return update_threshold(change.rule_id, change.new_value)
        return {"success": False, "error": f"unknown rule_system: {change.rule_system!r}"}


def _apply_change_to_config(change: RuleChange, rules_config: dict) -> None:
    """Apply a RuleChange to an in-memory rules_config dict (for simulation)."""
    if change.rule_system != "gl":
        return
    if change.change_type == "DISABLE_RULE":
        for rule in rules_config.get("rules", []):
            if rule.get("id") == change.rule_id:
                rule["enabled"] = False
    elif change.change_type == "ADD_NORMALISATION":
        table = "category_normalisation"
        rules_config.setdefault(table, {})[change.field.lower()] = change.new_value
    else:
        for rule in rules_config.get("rules", []):
            if rule.get("id") == change.rule_id:
                rule.setdefault("output", {})[change.field] = change.new_value
