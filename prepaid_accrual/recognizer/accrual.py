"""
Accrual recognizer — pure function, no I/O.

detect_accrual(line_classification, line_item) -> AccrualLineResult | None
"""
from __future__ import annotations

from decimal import Decimal

from models import AccrualLineResult


_PROFESSIONAL_CATEGORIES = {"professional_services", "legal"}


def detect_accrual(
    line_classification: dict,
    line_item: dict,
) -> AccrualLineResult | None:
    """
    Apply accrual recognition rule (Rule 2 from SOP Step 3).

    Returns AccrualLineResult if service_precedes_invoice is True, else None.

    Account routing:
        professional_services / legal → 2110 (Accrued Professional Services)
        all others                    → 2100 (Accrued Liabilities — general)

    The expense_account is the GL account assigned by Step 2 — the Posting stage
    uses it to know which expense account to debit when reversing the liability.
    """
    desc = (line_item.get("parsed_description") or {})

    if not desc.get("service_precedes_invoice", False):
        return None

    amount_field = (line_item.get("amount") or {})
    amount_raw   = amount_field.get("value")

    if amount_raw is None:
        return None
    try:
        amount = Decimal(str(amount_raw))
    except Exception:
        return None

    category        = desc.get("category_hint", "")
    accrual_account = "2110" if category in _PROFESSIONAL_CATEGORIES else "2100"
    expense_account = line_classification.get("gl_account") or "5000"
    service_end     = desc.get("service_period_end")

    return AccrualLineResult(
        line_number=line_classification["line_number"],
        accrual_account=accrual_account,
        expense_account=expense_account,
        amount=amount,
        service_period_end=service_end or "unknown",
        reversal_trigger="on_payment",
    )
