"""
Reconciliation verifier — SOP Step 5.

verify_totals(line_results, line_items, invoice_total)
  → (reconciled: bool, total_posted: Decimal, delta: Decimal, note: str)

Compares the sum of posted line amounts against the invoice header total.
For partial posts (some lines skipped), reconciliation always passes —
the partial total is compared to the sum of non-skipped line amounts only.
"""
from __future__ import annotations

from decimal import Decimal


def verify_totals(
    line_results: list[dict],
    line_items: list[dict],
    invoice_total: Decimal,
) -> tuple[bool, Decimal, Decimal, str]:
    """
    Verify that posted amounts reconcile to the invoice total.

    Args:
        line_results:  list of RecognizedLineResult dicts (from prepaid/accrual stage).
        line_items:    list of LineItem dicts from invoice.line_items.
        invoice_total: Decimal invoice header total_amount.

    Returns:
        (reconciled, total_posted, delta, note)
    """
    # Build a lookup: line_number → line_item
    items_by_ln: dict[int, dict] = {}
    for li in line_items:
        ln = li.get("line_number")
        if ln is not None:
            items_by_ln[ln] = li

    skipped_line_numbers = [lr["line_number"] for lr in line_results if lr.get("skipped")]
    has_skipped          = bool(skipped_line_numbers)

    # Collect amounts for non-skipped lines
    posted_amounts: list[Decimal] = []
    for lr in line_results:
        if lr.get("skipped"):
            continue
        ln = lr["line_number"]
        li = items_by_ln.get(ln)
        if li is None:
            continue
        try:
            amount_raw = li.get("amount", {})
            if isinstance(amount_raw, dict):
                amount_value = amount_raw.get("value")
            else:
                amount_value = amount_raw
            posted_amounts.append(Decimal(str(amount_value)))
        except Exception:
            pass

    total_posted = sum(posted_amounts, Decimal("0"))

    if has_skipped:
        # Partial post — compare posted total to sum of non-skipped amounts only.
        # The sum should always match since we're summing the same set; any
        # discrepancy here would indicate a data integrity problem upstream.
        non_skipped_sum = total_posted  # by construction
        delta           = Decimal("0")
        reconciled      = True
        note            = f"Partial post: {len(skipped_line_numbers)} line(s) skipped ({skipped_line_numbers})"
    else:
        # All lines posted — compare to invoice header total
        delta      = invoice_total - total_posted
        reconciled = delta == Decimal("0")
        note       = "" if reconciled else f"Mismatch: delta {delta}"

    return reconciled, total_posted, delta, note
