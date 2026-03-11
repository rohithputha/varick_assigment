"""
Journal entry builder — SOP Step 5.

build_entries_for_line(line_result, line_item, invoice_date, run_id, invoice_id)
  → list[JournalEntry]

Pure function — one invoice line in, N double-entry journal entries out.
Never raises; errors return [].

Entry IDs are deterministic:
  sha256(f"{run_id}|{invoice_id}|{line_number}|{entry_type}|{period_label}")[:16]
  Same run + same invoice always produces the same entry_id (idempotent on resume).
"""
from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation

from models import JournalEntry

# Fixed accounts
_ACCOUNTS_PAYABLE = "2000"
_FIXED_ASSETS     = "1500"


def _make_entry_id(run_id: str, invoice_id: str, line_number: int,
                   entry_type: str, period_label: str) -> str:
    """
    Deterministic entry ID — sha256 of the combination of all identifying fields.
    First 16 hex characters (64-bit prefix) — collision probability negligible
    for the invoice volumes expected.
    """
    key = f"{run_id}|{invoice_id}|{line_number}|{entry_type}|{period_label}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _invoice_period(invoice_date: date) -> str:
    return f"{invoice_date.year}-{invoice_date.month:02d}"


def _derive_period_from_iso(iso_date_str: str | None, fallback: str) -> str:
    """
    Convert an ISO date string (YYYY-MM-DD) to a period label (YYYY-MM).
    Falls back to `fallback` period if the date is None, 'unknown', or unparseable.
    """
    if not iso_date_str or iso_date_str.lower() == "unknown":
        return fallback
    try:
        parts = iso_date_str.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"
    except Exception:
        pass
    return fallback


def build_entries_for_line(
    line_result: dict,
    line_item: dict,
    invoice_date: date,
    run_id: str,
    invoice_id: str,
) -> list[JournalEntry]:
    """
    Build double-entry journal entries for one invoice line.

    Args:
        line_result:  RecognizedLineResult dict from the prepaid/accrual stage.
        line_item:    LineItem dict from invoice.line_items.
        invoice_date: Date of the invoice — used for current-period entries.
        run_id:       Pipeline run ID — used for deterministic entry_id hashing.
        invoice_id:   Invoice ID — used for deterministic entry_id hashing.

    Returns:
        list of JournalEntry dicts (may be empty for skipped / zero-amount lines).
    """
    # Skipped lines → no entries
    if line_result.get("skipped"):
        return []

    # Parse line amount
    try:
        amount_raw = line_item.get("amount", {})
        if isinstance(amount_raw, dict):
            amount_value = amount_raw.get("value")
        else:
            amount_value = amount_raw
        if amount_value is None:
            return []
        line_amount = Decimal(str(amount_value))
        if line_amount == Decimal("0"):
            return []
    except (InvalidOperation, TypeError):
        return []

    line_number     = line_result["line_number"]
    final_treatment = line_result.get("final_treatment", "")
    final_gl        = line_result.get("final_gl_account") or ""
    inv_period      = _invoice_period(invoice_date)

    # Build a raw description from the line item (safe fallback if missing)
    raw_desc = line_item.get("raw_description", f"line {line_number}")

    def _entry(entry_type: str, debit: str, credit: str,
               amount: Decimal, period: str, description: str) -> JournalEntry:
        eid = _make_entry_id(run_id, invoice_id, line_number, entry_type, period)
        return JournalEntry(
            entry_id       = eid,
            line_number    = line_number,
            entry_type     = entry_type,
            debit_account  = debit,
            credit_account = credit,
            amount         = amount,
            period_label   = period,
            description    = description,
        )

    try:
        # ── EXPENSE ──────────────────────────────────────────────────────────
        if final_treatment == "EXPENSE":
            return [_entry(
                "expense", final_gl, _ACCOUNTS_PAYABLE,
                line_amount, inv_period,
                f"Expense — {raw_desc}",
            )]

        # ── CAPITALIZE ───────────────────────────────────────────────────────
        elif final_treatment == "CAPITALIZE":
            return [_entry(
                "capitalize", _FIXED_ASSETS, _ACCOUNTS_PAYABLE,
                line_amount, inv_period,
                f"Fixed asset — {raw_desc}",
            )]

        # ── PREPAID ──────────────────────────────────────────────────────────
        elif final_treatment == "PREPAID":
            prepaid = line_result.get("prepaid_result")
            if prepaid is None:
                return []

            prepaid_account = prepaid.get("prepaid_account", "1310")
            expense_account = prepaid.get("expense_account", "5010")
            total_amount    = Decimal(str(prepaid.get("total_amount", line_amount)))

            # Initial booking entry
            initial = _entry(
                "prepaid_initial", prepaid_account, _ACCOUNTS_PAYABLE,
                total_amount, inv_period,
                f"Prepaid asset — {raw_desc}",
            )

            # Monthly amortization entries
            amort_entries = []
            for ae in prepaid.get("amortization_entries", []):
                period_label = ae.get("period_label", "unknown")
                ae_amount    = Decimal(str(ae.get("amount", "0")))
                amort_entries.append(_entry(
                    "prepaid_amortization", expense_account, prepaid_account,
                    ae_amount, period_label,
                    f"Amortization — {raw_desc} ({period_label})",
                ))

            return [initial] + amort_entries

        # ── ACCRUAL ──────────────────────────────────────────────────────────
        elif final_treatment == "ACCRUAL":
            accrual = line_result.get("accrual_result")
            if accrual is None:
                return []

            accrual_account = accrual.get("accrual_account", "2110")
            expense_account = accrual.get("expense_account", final_gl)
            accrual_amount  = Decimal(str(accrual.get("amount", line_amount)))
            service_end     = accrual.get("service_period_end")
            accrual_period  = _derive_period_from_iso(service_end, inv_period)

            accrual_entry = _entry(
                "accrual", expense_account, accrual_account,
                accrual_amount, accrual_period,
                f"Accrual — {raw_desc} (service ended {service_end or 'unknown'})",
            )
            reversal_entry = _entry(
                "accrual_reversal", accrual_account, _ACCOUNTS_PAYABLE,
                accrual_amount, "on_payment",
                f"Accrual reversal — {raw_desc} (reverses on payment)",
            )
            return [accrual_entry, reversal_entry]

        # ── Unknown treatment (safety net) ────────────────────────────────────
        else:
            return []

    except Exception:
        return []
