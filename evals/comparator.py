"""
Comparator — field-by-field comparison of pipeline outputs against ground truth.

Produces:
  LineRunResult  — per-line pass/fail for one pipeline run
  RunResult      — per-invoice pass/fail for one pipeline run
  InvoiceEvalResult — aggregated across N runs
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from evals.ground_truth import InvoiceGT, LineGT


# ---------------------------------------------------------------------------
# Per-field check
# ---------------------------------------------------------------------------

@dataclass
class FieldCheck:
    name: str
    passed: bool            # True = match, False = mismatch
    got: object             # actual value
    expected: object        # expected value
    skipped: bool = False   # True = no expectation for this field (not counted)


def _check(name: str, got, expected) -> FieldCheck:
    """Compare got vs expected. If expected is None and the sentinel is None, skip."""
    # Distinguish "expected to be None" from "no assertion" by checking the sentinel.
    # In our GT, None always means "no assertion expected" EXCEPT for base_expense_account
    # where it means "we expect None". We encode this distinction via a separate sentinel.
    got_str      = str(got) if got is not None else None
    expected_str = str(expected) if expected is not None else None
    passed       = got_str == expected_str
    return FieldCheck(name=name, passed=passed, got=got, expected=expected)


# Sentinel to mark fields where we explicitly expect None
_EXPECT_NONE = object()

def _check_nullable(name: str, got, expected_is_none: bool) -> FieldCheck:
    """Use when expected value is None and that's intentional."""
    if expected_is_none:
        passed = got is None
        return FieldCheck(name=name, passed=passed, got=got, expected=None)
    return FieldCheck(name=name, passed=True, got=got, expected=None, skipped=True)


def _check_decimal(name: str, got_str: str | None, expected: Decimal | None) -> FieldCheck:
    """Compare a decimal amount string from the pipeline against a Decimal GT value."""
    if expected is None:
        return FieldCheck(name=name, passed=True, got=got_str, expected=None, skipped=True)
    if got_str is None:
        return FieldCheck(name=name, passed=False, got=None, expected=expected)
    try:
        got_dec = Decimal(str(got_str)).quantize(Decimal("0.01"))
        exp_dec = expected.quantize(Decimal("0.01"))
        passed  = got_dec == exp_dec
    except InvalidOperation:
        passed = False
    return FieldCheck(name=name, passed=passed, got=got_str, expected=str(expected))


# ---------------------------------------------------------------------------
# Per-line result for one run
# ---------------------------------------------------------------------------

@dataclass
class LineRunResult:
    line_number: int
    checks: list[FieldCheck] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return all(c.passed for c in self.checks if not c.skipped)

    @property
    def stage3_checks(self) -> list[FieldCheck]:
        return [c for c in self.checks if c.name.startswith("stage3_")]

    @property
    def stage4_checks(self) -> list[FieldCheck]:
        return [c for c in self.checks if c.name.startswith("final_")
                or c.name in ("amortization_months", "monthly_amount",
                               "accrual_account", "expense_account")]


# ---------------------------------------------------------------------------
# Per-invoice result for one run
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    run_index: int
    invoice_id: str
    blocked_check: FieldCheck | None    # INV-006: was NO_PO correctly returned?
    approval_check: FieldCheck | None   # None if blocked or no expectation
    line_results: list[LineRunResult] = field(default_factory=list)

    @property
    def stage3_pass(self) -> bool:
        return all(
            c.passed
            for lr in self.line_results
            for c in lr.stage3_checks
            if not c.skipped
        )

    @property
    def stage4_pass(self) -> bool:
        return all(
            c.passed
            for lr in self.line_results
            for c in lr.stage4_checks
            if not c.skipped
        )

    @property
    def approval_pass(self) -> bool:
        return self.approval_check.passed if self.approval_check else True

    @property
    def blocked_pass(self) -> bool:
        return self.blocked_check.passed if self.blocked_check else True

    @property
    def all_pass(self) -> bool:
        return self.stage3_pass and self.stage4_pass and self.approval_pass and self.blocked_pass


# ---------------------------------------------------------------------------
# Aggregated across N runs
# ---------------------------------------------------------------------------

@dataclass
class InvoiceEvalResult:
    invoice_id: str
    gt: InvoiceGT
    runs: list[RunResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def pass_at_1(self) -> bool:
        return self.runs[0].all_pass if self.runs else False

    @property
    def pass_at_n(self) -> bool:
        return any(r.all_pass for r in self.runs)

    @property
    def mean_pass_rate(self) -> float:
        if not self.runs:
            return 0.0
        return sum(r.all_pass for r in self.runs) / self.n

    def _check_rate(self, check_name: str) -> tuple[int, int]:
        """Return (passed, total) for a specific check name across all runs."""
        passed = total = 0
        for run in self.runs:
            for lr in run.line_results:
                for c in lr.checks:
                    if c.name == check_name and not c.skipped:
                        total += 1
                        if c.passed:
                            passed += 1
        return passed, total

    def stage3_gl_rate(self) -> tuple[int, int]:
        return self._check_rate("stage3_gl")

    def stage3_treatment_rate(self) -> tuple[int, int]:
        return self._check_rate("stage3_treatment")

    def stage3_base_rate(self) -> tuple[int, int]:
        return self._check_rate("stage3_base")

    def final_gl_rate(self) -> tuple[int, int]:
        return self._check_rate("final_gl")

    def final_treatment_rate(self) -> tuple[int, int]:
        return self._check_rate("final_treatment")

    def amort_months_rate(self) -> tuple[int, int]:
        return self._check_rate("amortization_months")

    def monthly_amount_rate(self) -> tuple[int, int]:
        return self._check_rate("monthly_amount")

    def accrual_account_rate(self) -> tuple[int, int]:
        return self._check_rate("accrual_account")

    def expense_account_rate(self) -> tuple[int, int]:
        return self._check_rate("expense_account")

    def approval_rate(self) -> tuple[int, int]:
        passed = total = 0
        for run in self.runs:
            if run.approval_check and not run.approval_check.skipped:
                total += 1
                if run.approval_check.passed:
                    passed += 1
        return passed, total


# ---------------------------------------------------------------------------
# Compare one run bundle against ground truth
# ---------------------------------------------------------------------------

def compare_run(bundle: dict, gt: InvoiceGT, run_index: int) -> RunResult:
    """
    Compare a single run_invoice_once() bundle against the invoice ground truth.
    """
    result        = bundle["result"]
    stage_outputs = bundle["stage_outputs"]

    # ── Blocked invoice check (INV-006) ──────────────────────────────────────
    # Check the ingestion output's invoice.status — more reliable than PO matching
    # output (which can fail independently when po_number is None).
    blocked_check: FieldCheck | None = None
    if gt.expected_blocked:
        ingestion_out = stage_outputs.get("INGESTION") or {}
        invoice_dict  = ingestion_out.get("invoice") or {}
        raw_status    = str(invoice_dict.get("status", ""))
        # status may be "InvoiceStatus.FLAGGED_NO_PO" or just "FLAGGED_NO_PO"
        is_flagged_no_po = "FLAGGED_NO_PO" in raw_status
        blocked_check = FieldCheck(
            name="po_blocked",
            passed=is_flagged_no_po,
            got=raw_status,
            expected="FLAGGED_NO_PO",
        )

    # ── Approval routing check ────────────────────────────────────────────────
    approval_check: FieldCheck | None = None
    if gt.expected_approval is not None:
        # Outcome is on the top-level orchestrator result for DRY_RUN
        actual_outcome = result.get("outcome")
        approval_check = _check("approval_outcome", actual_outcome, gt.expected_approval)

    # ── Per-line checks ───────────────────────────────────────────────────────
    gl_out = stage_outputs.get("GL_CLASSIFICATION") or {}
    pa_out = stage_outputs.get("PREPAID_ACCRUAL") or {}

    lc_by_ln: dict[int, dict] = {
        lc["line_number"]: lc
        for lc in gl_out.get("line_classifications", [])
    }
    lr_by_ln: dict[int, dict] = {
        lr["line_number"]: lr
        for lr in pa_out.get("line_results", [])
    }

    line_results: list[LineRunResult] = []

    for line_gt in gt.lines:
        ln     = line_gt.line_number
        lc     = lc_by_ln.get(ln, {})
        lr     = lr_by_ln.get(ln, {})
        checks: list[FieldCheck] = []

        # Dimension 1 — Stage 3 GL Classification
        checks.append(_check("stage3_gl",        lc.get("gl_account"),            line_gt.stage3_gl))
        checks.append(_check("stage3_treatment",  lc.get("treatment"),             line_gt.stage3_treatment))

        # base_expense_account: None is an explicit expected value for EXPENSE lines
        got_base = lc.get("base_expense_account")
        exp_base = line_gt.stage3_base
        # Both None → pass. One None → fail. Both non-None → string compare.
        checks.append(_check("stage3_base", got_base, exp_base))

        # Dimension 2 — Stage 4 Final Treatment
        checks.append(_check("final_gl",        lr.get("final_gl_account"),  line_gt.final_gl))
        checks.append(_check("final_treatment",  lr.get("final_treatment"),   line_gt.final_treatment))

        # PREPAID-specific
        if line_gt.amortization_months is not None:
            pr = lr.get("prepaid_result") or {}
            checks.append(_check("amortization_months",
                                 pr.get("amortization_months"),
                                 line_gt.amortization_months))
            # Monthly amount: first amortization entry's "amount"
            entries  = pr.get("amortization_entries") or []
            got_amt  = entries[0]["amount"] if entries else None
            checks.append(_check_decimal("monthly_amount", got_amt, line_gt.monthly_amount))

        # ACCRUAL-specific
        if line_gt.accrual_account is not None:
            ar = lr.get("accrual_result") or {}
            checks.append(_check("accrual_account",  ar.get("accrual_account"),  line_gt.accrual_account))
            checks.append(_check("expense_account",  ar.get("expense_account"),  line_gt.expense_account))

        line_results.append(LineRunResult(line_number=ln, checks=checks))

    return RunResult(
        run_index=run_index,
        invoice_id=gt.invoice_id,
        blocked_check=blocked_check,
        approval_check=approval_check,
        line_results=line_results,
    )


def compare_invoice(bundles: list[dict], gt: InvoiceGT) -> InvoiceEvalResult:
    """Aggregate N run comparisons into an InvoiceEvalResult."""
    result = InvoiceEvalResult(invoice_id=gt.invoice_id, gt=gt)
    for i, bundle in enumerate(bundles):
        result.runs.append(compare_run(bundle, gt, run_index=i + 1))
    return result
