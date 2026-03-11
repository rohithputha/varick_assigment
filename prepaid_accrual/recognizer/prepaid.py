"""
Prepaid recognizer — pure functions, no I/O.

detect_prepaid(line_classification, line_item) -> PrepaidLineResult | None
compute_amortization_schedule(...)             -> list[AmortizationEntry]
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from models import AmortizationEntry, PrepaidLineResult


# ---------------------------------------------------------------------------
# Amortization schedule
# ---------------------------------------------------------------------------

def compute_amortization_schedule(
    total_amount:    Decimal,
    start_date:      date,
    end_date:        date,
    prepaid_account: str,
    expense_account: str,
) -> list[AmortizationEntry]:
    """
    Build one AmortizationEntry per calendar month overlapping the service period.

    Equal monthly split; remainder assigned to the last month so that
    sum(entry.amount) == total_amount exactly.
    """
    if start_date > end_date or total_amount <= Decimal("0"):
        return []

    # Collect all (year, month) pairs that overlap [start_date, end_date]
    months: list[tuple[int, int]] = []
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    if not months:
        return []

    n = len(months)
    monthly = (total_amount / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    entries: list[AmortizationEntry] = []
    running = Decimal("0.00")

    for i, (yr, mo) in enumerate(months):
        _, last_day = monthrange(yr, mo)
        period_start = date(yr, mo, 1)
        period_end   = date(yr, mo, last_day)
        period_label = f"{yr}-{mo:02d}"

        # Last month gets remainder to ensure exact total
        amount = (total_amount - running) if i == n - 1 else monthly
        running += amount

        entries.append(AmortizationEntry(
            period_label=period_label,
            period_start=period_start,
            period_end=period_end,
            amount=amount,
            prepaid_account=prepaid_account,
            expense_account=expense_account,
        ))

    return entries


# ---------------------------------------------------------------------------
# detect_prepaid
# ---------------------------------------------------------------------------

def detect_prepaid(
    line_classification: dict,
    line_item: dict,
) -> PrepaidLineResult | None:
    """
    Apply prepaid recognition rules (Rules 3 & 4 from SOP Step 3).

    Returns PrepaidLineResult if this line should be recognised as prepaid,
    None otherwise.

    Called only after accrual check (Rule 2) has already returned None.

    CASE A — existing PREPAID from GL Classification (annual software / cloud):
        treatment == "PREPAID" → accounts already correct, compute schedule.

    CASE B — insurance reclassification:
        category == "insurance"
        AND (billing_type == "annual" OR service_period_days > 31)
        AND NOT service_precedes_invoice
        → override GL's EXPENSE 5100 → PREPAID 1320, compute schedule.
    """
    desc   = (line_item.get("parsed_description") or {})
    amount_field = (line_item.get("amount") or {})
    amount_raw   = amount_field.get("value")

    if amount_raw is None:
        return None
    try:
        total_amount = Decimal(str(amount_raw))
    except Exception:
        return None
    if total_amount <= Decimal("0"):
        return None

    treatment      = line_classification.get("treatment")
    category_hint  = desc.get("category_hint", "")
    billing_type   = desc.get("billing_type", "unknown")
    svc_days       = desc.get("service_period_days")

    # ── CASE A: existing PREPAID ─────────────────────────────────────────────
    if treatment == "PREPAID":
        prepaid_account = line_classification.get("gl_account") or ""
        expense_account = line_classification.get("base_expense_account") or ""
        return _build_prepaid_result(
            line_number=line_classification["line_number"],
            prepaid_account=prepaid_account,
            expense_account=expense_account,
            total_amount=total_amount,
            desc=desc,
            is_reclassified=False,
            reclassification_reason=None,
        )

    # ── CASE B: insurance reclassification ───────────────────────────────────
    if (
        category_hint == "insurance"
        and (billing_type == "annual" or (svc_days is not None and svc_days > 31))
        and not desc.get("service_precedes_invoice", False)
    ):
        return _build_prepaid_result(
            line_number=line_classification["line_number"],
            prepaid_account="1320",
            expense_account="5100",
            total_amount=total_amount,
            desc=desc,
            is_reclassified=True,
            reclassification_reason="insurance_annual_prepaid_reclassification",
        )

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_prepaid_result(
    line_number:             int,
    prepaid_account:         str,
    expense_account:         str,
    total_amount:            Decimal,
    desc:                    dict,
    is_reclassified:         bool,
    reclassification_reason: str | None,
) -> PrepaidLineResult:
    start_str = desc.get("service_period_start")
    end_str   = desc.get("service_period_end")

    if start_str and end_str:
        try:
            start_date = date.fromisoformat(start_str)
            end_date   = date.fromisoformat(end_str)
            entries    = compute_amortization_schedule(
                total_amount, start_date, end_date, prepaid_account, expense_account
            )
        except (ValueError, TypeError):
            start_date = date.min
            end_date   = date.min
            entries    = []
    else:
        # Dates missing — schedule not computable; caller will note this
        start_date = date.min
        end_date   = date.min
        entries    = []

    return PrepaidLineResult(
        line_number=line_number,
        prepaid_account=prepaid_account,
        expense_account=expense_account,
        total_amount=total_amount,
        service_period_start=start_date,
        service_period_end=end_date,
        amortization_months=len(entries),
        amortization_entries=entries,
        is_reclassified=is_reclassified,
        reclassification_reason=reclassification_reason,
    )
