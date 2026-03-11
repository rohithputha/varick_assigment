"""
FeedbackAnalyzer — detects systematic error patterns in correction data.

A systematic error is a (field, proposed_value, corrected_value) triple
that appears across ≥2 different invoices.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from evals.feedback.collector import FeedbackCollector, FeedbackRecord
from pipeline.db import SQLiteDB
from pipeline.state_manager import GlobalStateManager
from rules_engine.rules_tools import get_rules


@dataclass
class ErrorPattern:
    field:             str
    proposed_value:    str
    corrected_value:   str
    frequency:         int
    affected_invoices: list[str]
    affected_rule_id:  str | None       # which rule in rules.json produced proposed_value
    correction_type:   str              # WRONG_GL | WRONG_TREATMENT | WRONG_BASE |
                                        # WRONG_APPROVAL | WRONG_NORMALISATION


@dataclass
class ErrorPatternReport:
    total_corrections:  int
    by_stage:           dict[str, int]
    by_field:           dict[str, int]
    systematic_errors:  list[ErrorPattern]   # frequency >= 2
    one_off_errors:     list[ErrorPattern]   # frequency == 1
    rule_error_rates:   dict[str, float]     # rule_id → fraction of usages corrected
    top_problem_rules:  list[str]            # error_rate > 0.25


# ---------------------------------------------------------------------------
# Helper: infer correction_type from field name
# ---------------------------------------------------------------------------

_CORRECTION_TYPE_MAP = {
    "gl_account":           "WRONG_GL",
    "treatment":            "WRONG_TREATMENT",
    "base_expense_account": "WRONG_BASE",
    "amortization_months":  "WRONG_TREATMENT",
    "monthly_amount":       "WRONG_TREATMENT",
    "accrual_account":      "WRONG_TREATMENT",
    "expense_account":      "WRONG_TREATMENT",
    "approval_outcome":     "WRONG_APPROVAL",
}


class FeedbackAnalyzer:
    """
    Analyzes feedback_records to detect systematic error patterns.
    """

    def __init__(self, db: SQLiteDB) -> None:
        self._db        = db
        self._gsm       = GlobalStateManager(db)
        self._collector = FeedbackCollector(db)

    def analyze(self, since: str | None = None) -> ErrorPatternReport:
        """
        1. Load feedback_records (filtered by created_at >= since if given)
        2. Group by (field, proposed_value, corrected_value) → count
        3. freq >= 2 → systematic error; freq == 1 → one-off
        4. Cross-reference with rules.json for rule attribution
        5. Compute per-rule error rates
        """
        records = self._collector.get_all_corrections(since=since)
        total   = len(records)

        # Stage and field tallies
        by_stage: dict[str, int] = {}
        by_field: dict[str, int] = {}
        for r in records:
            by_stage[r.stage] = by_stage.get(r.stage, 0) + 1
            by_field[r.field] = by_field.get(r.field, 0) + 1

        # Group by (field, proposed_value, corrected_value)
        groups: dict[tuple, list[FeedbackRecord]] = {}
        for r in records:
            key = (r.field, r.proposed_value, r.corrected_value)
            groups.setdefault(key, []).append(r)

        # Load rules for attribution
        try:
            rules_config = get_rules()
        except Exception:
            rules_config = {"rules": []}

        # Build error patterns
        systematic: list[ErrorPattern] = []
        one_offs:   list[ErrorPattern] = []

        for (fld, proposed, corrected), recs in groups.items():
            affected_invoices = list({r.invoice_id for r in recs})
            freq              = len(set(r.invoice_id for r in recs))  # unique invoices

            # Rule attribution (for GL fields)
            rule_id = self._find_rule_id(fld, proposed, recs, rules_config)

            correction_type = _CORRECTION_TYPE_MAP.get(fld, "WRONG_GL")

            pattern = ErrorPattern(
                field=fld,
                proposed_value=proposed,
                corrected_value=corrected,
                frequency=freq,
                affected_invoices=affected_invoices,
                affected_rule_id=rule_id,
                correction_type=correction_type,
            )
            if freq >= 2:
                systematic.append(pattern)
            else:
                one_offs.append(pattern)

        # Sort by frequency desc
        systematic.sort(key=lambda p: p.frequency, reverse=True)

        # Per-rule error rates
        rule_error_rates = self._compute_rule_error_rates(records, rules_config)
        top_problem_rules = [rid for rid, rate in rule_error_rates.items() if rate > 0.25]

        return ErrorPatternReport(
            total_corrections=total,
            by_stage=by_stage,
            by_field=by_field,
            systematic_errors=systematic,
            one_off_errors=one_offs,
            rule_error_rates=rule_error_rates,
            top_problem_rules=top_problem_rules,
        )

    # -----------------------------------------------------------------------
    # Private: rule attribution
    # -----------------------------------------------------------------------

    def _find_rule_id(
        self,
        field: str,
        proposed_value: str,
        records: list[FeedbackRecord],
        rules_config: dict,
    ) -> str | None:
        """
        For a GL-level correction, find the rule that produces proposed_value
        for the category seen on affected proposals.
        """
        if field not in ("gl_account", "treatment", "base_expense_account"):
            return None

        # Gather category_hints from affected shadow_proposals
        category_hints: set[str] = set()
        for r in records:
            proposal = self._gsm.get_shadow_proposal(r.proposal_id)
            if proposal:
                for lp in proposal.get("line_proposals", []):
                    if lp.get("line_number") == r.line_number:
                        hint = lp.get("category_hint")
                        if hint:
                            category_hints.add(hint)

        if not category_hints:
            return None

        # Find rule that matches any of these categories and produces proposed_value
        for rule in rules_config.get("rules", []):
            if not rule.get("enabled", True):
                continue
            condition = rule.get("condition", {})
            output    = rule.get("output", {})

            # Check if rule's output field matches proposed_value
            if str(output.get(field, "")) != proposed_value:
                continue

            # Check if rule's condition matches one of our category_hints
            cat_condition = condition.get("category_hint", {})
            if isinstance(cat_condition, dict) and cat_condition.get("eq") in category_hints:
                return rule.get("id")

        return None

    def _compute_rule_error_rates(
        self,
        records: list[FeedbackRecord],
        rules_config: dict,
    ) -> dict[str, float]:
        """
        Compute per-rule error rate: fraction of proposals using that rule
        that had at least one correction on a GL-level field.
        """
        # Count how many proposals used each rule (from shadow_proposals)
        rule_usage: dict[str, set] = {}   # rule_id → set of proposal_ids
        rule_errors: dict[str, set] = {}  # rule_id → set of proposal_ids with corrections

        all_proposals = self._gsm.list_all_proposals()
        for prop in all_proposals:
            for lp in prop.get("line_proposals", []):
                # We don't store applied_rule on LineProposal directly
                # Use category_hint → rule matching as proxy
                pass

        # Simpler approach: for each correction on a GL field, find the rule
        # that produced proposed_value for the affected line's category
        for r in records:
            if r.field not in ("gl_account", "treatment", "base_expense_account"):
                continue
            rule_id = self._find_rule_id(r.field, r.proposed_value, [r], rules_config)
            if rule_id:
                rule_errors.setdefault(rule_id, set()).add(r.proposal_id)

        # Usage: count distinct proposals that have a line_classification that matched each rule
        # (approximation: count all proposals as using a rule if their category_hint matches)
        for prop in all_proposals:
            for lp in prop.get("line_proposals", []):
                hint = lp.get("category_hint", "")
                gl   = lp.get("gl_account", "")
                tmt  = lp.get("treatment", "")
                for rule in rules_config.get("rules", []):
                    if not rule.get("enabled", True):
                        continue
                    cond = rule.get("condition", {})
                    cat_eq = cond.get("category_hint", {}).get("eq", "")
                    if cat_eq == hint:
                        rid = rule.get("id")
                        rule_usage.setdefault(rid, set()).add(prop["proposal_id"])

        error_rates: dict[str, float] = {}
        for rid, usage_set in rule_usage.items():
            errors = len(rule_errors.get(rid, set()))
            usage  = len(usage_set)
            if usage > 0:
                error_rates[rid] = errors / usage

        return error_rates
