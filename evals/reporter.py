"""
Reporter — formats eval results to stdout.

Output sections:
  1. Per-invoice detail: line-level checks for each run
  2. Summary accuracy table: 3 dimensions × field-level pass rates
"""
from __future__ import annotations

from evals.comparator import FieldCheck, InvoiceEvalResult, LineRunResult, RunResult

_PASS  = "\033[92m✓\033[0m"
_FAIL  = "\033[91m✗\033[0m"
_SKIP  = "\033[93m~\033[0m"
_W     = 62


def _icon(check: FieldCheck | None) -> str:
    if check is None or check.skipped:
        return _SKIP
    return _PASS if check.passed else _FAIL


def _pct(passed: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{passed}/{total}  ({100 * passed / total:.1f}%)"


def _bar(label: str, passed: int, total: int, width: int = 28) -> str:
    pct_str = _pct(passed, total)
    icon    = _PASS if passed == total else (_SKIP if total == 0 else _FAIL)
    return f"  {icon}  {label:<{width}} {pct_str}"


# ---------------------------------------------------------------------------
# Per-invoice detail
# ---------------------------------------------------------------------------

def print_invoice_header(inv_id: str, vendor: str, amount: str, n: int) -> None:
    print(f"\n{'═' * _W}")
    print(f"  {inv_id}  —  {vendor}  |  ${amount}  (N={n} runs)")
    print(f"{'═' * _W}")


def _fmt_field(check: FieldCheck) -> str:
    icon = _icon(check)
    if check.skipped:
        return f"{icon} {check.name}=~"
    if check.passed:
        return f"{icon} {check.name}={check.got!r}"
    return f"{icon} {check.name}: got={check.got!r} exp={check.expected!r}"


def _print_line_result(lr: LineRunResult) -> None:
    stage3 = [c for c in lr.checks if c.name.startswith("stage3_")]
    stage4 = [c for c in lr.checks if not c.name.startswith("stage3_")]

    s3_parts = "  ".join(_fmt_field(c) for c in stage3)
    s4_parts = "  ".join(_fmt_field(c) for c in stage4) if stage4 else None

    all_ok = lr.all_pass
    icon   = _PASS if all_ok else _FAIL
    print(f"    {icon} Line {lr.line_number}:")
    print(f"       Stage3  {s3_parts}")
    if s4_parts:
        print(f"       Stage4  {s4_parts}")


def print_run_result(run: RunResult) -> None:
    overall = _PASS if run.all_pass else _FAIL
    print(f"\n  ─── Run {run.run_index} {overall} ───────────────────────────")

    if run.blocked_check:
        icon = _icon(run.blocked_check)
        print(f"    {icon} Blocked check: po_match.status={run.blocked_check.got!r}  "
              f"(expected {run.blocked_check.expected!r})")

    for lr in run.line_results:
        _print_line_result(lr)

    if run.approval_check:
        icon = _icon(run.approval_check)
        print(f"    {icon} Approval: got={run.approval_check.got!r}  "
              f"expected={run.approval_check.expected!r}")


def print_invoice_result(result: InvoiceEvalResult, raw: dict) -> None:
    vendor = raw.get("vendor_name", "?")
    amount = raw.get("total_amount", "?")
    print_invoice_header(result.invoice_id, vendor, amount, result.n)

    for run in result.runs:
        print_run_result(run)

    # Summary line
    print(f"\n  pass@1={_PASS if result.pass_at_1 else _FAIL}  "
          f"pass@{result.n}={_PASS if result.pass_at_n else _FAIL}  "
          f"mean={result.mean_pass_rate * 100:.0f}%")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(results: list[InvoiceEvalResult]) -> None:
    print(f"\n{'═' * _W}")
    print("  EVAL SUMMARY")
    print(f"{'═' * _W}")

    total_n = sum(r.n for r in results)
    print(f"  Invoices: {len(results)}  |  Total runs: {total_n}")

    # ── Dimension 1 — GL Classification ──────────────────────────────────────
    print("\n  Dimension 1 — GL Classification (Stage 3)")
    gl_p, gl_t     = _sum_rates(results, InvoiceEvalResult.stage3_gl_rate)
    tr_p, tr_t     = _sum_rates(results, InvoiceEvalResult.stage3_treatment_rate)
    ba_p, ba_t     = _sum_rates(results, InvoiceEvalResult.stage3_base_rate)
    print(_bar("gl_account",         gl_p, gl_t))
    print(_bar("treatment",          tr_p, tr_t))
    print(_bar("base_expense_acct",  ba_p, ba_t))

    # ── Dimension 2 — Final Treatment ────────────────────────────────────────
    print("\n  Dimension 2 — Final Treatment (Stage 4)")
    fgl_p, fgl_t   = _sum_rates(results, InvoiceEvalResult.final_gl_rate)
    ftr_p, ftr_t   = _sum_rates(results, InvoiceEvalResult.final_treatment_rate)
    am_p,  am_t    = _sum_rates(results, InvoiceEvalResult.amort_months_rate)
    ma_p,  ma_t    = _sum_rates(results, InvoiceEvalResult.monthly_amount_rate)
    ac_p,  ac_t    = _sum_rates(results, InvoiceEvalResult.accrual_account_rate)
    ex_p,  ex_t    = _sum_rates(results, InvoiceEvalResult.expense_account_rate)
    print(_bar("final_gl_account",   fgl_p, fgl_t))
    print(_bar("final_treatment",    ftr_p, ftr_t))
    if am_t:
        print(_bar("amort_months",   am_p,  am_t))
    if ma_t:
        print(_bar("monthly_amount", ma_p,  ma_t))
    if ac_t:
        print(_bar("accrual_account",ac_p,  ac_t))
    if ex_t:
        print(_bar("expense_account",ex_p,  ex_t))

    # ── Dimension 3 — Approval Routing ───────────────────────────────────────
    print("\n  Dimension 3 — Approval Routing (Stage 5)")
    ap_p, ap_t = _sum_rates(results, InvoiceEvalResult.approval_rate)
    print(_bar("outcome", ap_p, ap_t))

    # ── Blocked invoice check ─────────────────────────────────────────────────
    blocked_results = [r for r in results if r.gt.expected_blocked]
    if blocked_results:
        print("\n  Blocked Invoice Check (INV-006)")
        bl_passed = sum(
            run.blocked_pass
            for r in blocked_results
            for run in r.runs
        )
        bl_total = sum(r.n for r in blocked_results)
        print(_bar("po_blocked_correctly", bl_passed, bl_total))

    # ── Invoice-level pass rates ──────────────────────────────────────────────
    non_blocked = [r for r in results if not r.gt.expected_blocked]
    p1 = sum(r.pass_at_1 for r in non_blocked)
    pn = sum(r.pass_at_n for r in non_blocked)
    nb = len(non_blocked)
    print(f"\n  Invoice pass@1:  {p1}/{nb}")
    print(f"  Invoice pass@N:  {pn}/{nb}")
    print(f"{'═' * _W}\n")


def _sum_rates(
    results: list[InvoiceEvalResult],
    rate_fn,
) -> tuple[int, int]:
    total_p = total_t = 0
    for r in results:
        p, t = rate_fn(r)
        total_p += p
        total_t += t
    return total_p, total_t
