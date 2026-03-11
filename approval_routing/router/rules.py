"""
Approval Routing rule engine — SOP Step 4.

route_invoice(recognized_invoice_dict) → ApprovalRoutingResult

Pure function — no I/O, no side effects, no LLM calls.
Uses Decimal throughout; never raises (errors → DENY/fail_closed_deny).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from models import ApprovalOutcome, ApprovalRoutingResult
from approval_routing.threshold_tools import get_thresholds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_thresholds() -> tuple[Decimal, Decimal, Decimal, Decimal, set]:
    """Load threshold values fresh from thresholds.json on each call."""
    try:
        config = get_thresholds()
        t = config.get("thresholds", {})
        auto_approve_max = Decimal(str(t.get("auto_approve_max", "1000.00")))
        dept_manager_max = Decimal(str(t.get("dept_manager_max", "10000.00")))
        marketing_max    = Decimal(str(t.get("marketing_max", "2500.00")))
        engineering_max  = Decimal(str(t.get("engineering_max", "5000.00")))
        cloud_accounts   = set(config.get("cloud_software_accounts", ["5010", "5020"]))
    except Exception:
        # Fall back to hardcoded defaults if thresholds.json is unavailable
        auto_approve_max = Decimal("1000.00")
        dept_manager_max = Decimal("10000.00")
        marketing_max    = Decimal("2500.00")
        engineering_max  = Decimal("5000.00")
        cloud_accounts   = {"5010", "5020"}
    return auto_approve_max, dept_manager_max, marketing_max, engineering_max, cloud_accounts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_expense_account(line: dict) -> str | None:
    """
    Determine the underlying expense GL account for a single line result.

    Resolution order (per plan §3 "All lines cloud or software" check):
      EXPENSE  (no_action_required=True) → final_gl_account
      PREPAID  (prepaid_result not None) → prepaid_result.expense_account
      ACCRUAL  (accrual_result not None) → accrual_result.expense_account
      SKIPPED                            → None  (fails the check conservatively)
    """
    if line.get("skipped"):
        return None

    prepaid = line.get("prepaid_result")
    if prepaid is not None:
        return prepaid.get("expense_account")

    accrual = line.get("accrual_result")
    if accrual is not None:
        return accrual.get("expense_account")

    # EXPENSE / no_action_required path
    return line.get("final_gl_account")


def _all_lines_cloud_or_software(line_results: list[dict], cloud_accounts: set) -> bool:
    """
    Return True iff every non-skipped line's underlying expense account is
    in the cloud/software accounts set (default: {5010, 5020}).

    If all lines are skipped the check fails (conservative).
    """
    non_skipped = [lr for lr in line_results if not lr.get("skipped")]
    if not non_skipped:
        return False  # no non-skipped lines → fail conservatively

    for line in non_skipped:
        account = _resolve_expense_account(line)
        if account not in cloud_accounts:
            return False
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def route_invoice(recognized_invoice_dict: dict) -> ApprovalRoutingResult:
    """
    Evaluate the SOP approval routing rules and return an ApprovalRoutingResult.

    Args:
        recognized_invoice_dict:
            RecognizedInvoice dict from the Prepaid/Accrual Recognition stage.
            Expected structure:
              {
                "classified_invoice": {
                  "invoice": { "header": { "total_amount": {"value": ...},
                                           "department":   {"value": ...}, ... },
                               ... },
                  "all_classified": bool,
                  ...
                },
                "line_results": [ {...}, ... ],
                ...
              }

    Returns:
        ApprovalRoutingResult — never raises; errors produce DENY/fail_closed_deny.
    """

    # ── Load thresholds from JSON ─────────────────────────────────────────────
    (
        _AUTO_APPROVE_MAX,
        _DEPT_MANAGER_MAX,
        _MARKETING_MAX,
        _ENGINEERING_MAX,
        _CLOUD_SOFTWARE_ACCOUNTS,
    ) = _load_thresholds()

    # ── Extract fields ────────────────────────────────────────────────────────
    try:
        classified_invoice = recognized_invoice_dict["classified_invoice"]
        invoice            = classified_invoice["invoice"]
        header             = invoice["header"]

        # total_amount — must convert via Decimal(str(...)) to avoid float precision issues
        raw_amount   = header.get("total_amount", {})
        amount_value = raw_amount.get("value") if isinstance(raw_amount, dict) else raw_amount
        if amount_value is None:
            raise ValueError("total_amount.value is None")
        total_amount = Decimal(str(amount_value))

        # department — None if missing/blank; lowercased for comparisons
        dept_field   = header.get("department")
        if isinstance(dept_field, dict):
            dept_raw = dept_field.get("value") or ""
        else:
            dept_raw = dept_field or ""
        department  = dept_raw.strip() or None   # keep original case for result
        dept_lower  = department.lower() if department else ""

        # line results & classification flag
        line_results   = recognized_invoice_dict.get("line_results", [])
        all_classified = classified_invoice.get("all_classified", True)

    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
        # Cannot evaluate routing — fail closed
        return ApprovalRoutingResult(
            outcome              = ApprovalOutcome.DENY,
            applied_rule         = "fail_closed_deny",
            total_amount         = Decimal("0"),
            department           = None,
            has_capitalize       = False,
            all_lines_classified = False,
            reasoning            = f"fail_closed_deny: could not parse invoice fields — {exc}",
        )

    # ── Step 1: Evaluate constraints ─────────────────────────────────────────
    has_capitalize        = any(
        lr.get("original_treatment") == "CAPITALIZE" and not lr.get("skipped")
        for lr in line_results
    )
    gl_flagged_constraint = not all_classified

    # ── Step 2: Evaluate Rules 1–7 (first match wins) ────────────────────────

    # RULE 1 — CAPITALIZE override
    if has_capitalize:
        outcome      = ApprovalOutcome.VP_FINANCE
        applied_rule = "capitalize_override"
        reasoning    = (
            f"Fixed asset line(s) present (CAPITALIZE treatment) — "
            f"VP Finance approval required regardless of amount (${total_amount}) or department."
        )

    # RULE 2 — Engineering department override
    elif (
        dept_lower == "engineering"
        and total_amount <= _ENGINEERING_MAX
        and _all_lines_cloud_or_software(line_results, _CLOUD_SOFTWARE_ACCOUNTS)
    ):
        outcome      = ApprovalOutcome.AUTO_APPROVE
        applied_rule = "engineering_override"
        reasoning    = (
            f"Engineering department, total ${total_amount} ≤ $5,000, "
            f"all lines are cloud (5020) or software (5010) — routine SaaS/cloud spend auto-approved."
        )

    # RULE 3 — Marketing department override
    elif dept_lower == "marketing" and total_amount <= _MARKETING_MAX:
        outcome      = ApprovalOutcome.AUTO_APPROVE
        applied_rule = "marketing_override"
        reasoning    = (
            f"Marketing department, total ${total_amount} ≤ $2,500 — "
            f"small campaign/vendor spend auto-approved."
        )

    # RULE 4 — Base: auto-approve
    elif total_amount <= _AUTO_APPROVE_MAX:
        outcome      = ApprovalOutcome.AUTO_APPROVE
        applied_rule = "base_auto"
        reasoning    = (
            f"Total ${total_amount} ≤ $1,000 — within base auto-approve threshold."
        )

    # RULE 5 — Base: department manager
    elif total_amount <= _DEPT_MANAGER_MAX:
        outcome      = ApprovalOutcome.DEPT_MANAGER
        applied_rule = "dept_manager_base"
        reasoning    = (
            f"Total ${total_amount} is between $1,000 and $10,000 — "
            f"department manager approval required."
        )

    # RULE 6 — Base: VP Finance
    elif total_amount > _DEPT_MANAGER_MAX:
        outcome      = ApprovalOutcome.VP_FINANCE
        applied_rule = "vp_finance_base"
        reasoning    = (
            f"Total ${total_amount} exceeds $10,000 — VP Finance approval required."
        )

    # RULE 7 — Fail closed (safety net — should only reach here if total_amount is somehow NaN/inf)
    else:
        outcome      = ApprovalOutcome.DENY
        applied_rule = "fail_closed_deny"
        reasoning    = "fail_closed_deny: amount did not match any routing rule."

    # ── Step 3: Apply Rule 0 constraint (gl_flagged) ─────────────────────────
    if gl_flagged_constraint and outcome == ApprovalOutcome.AUTO_APPROVE:
        outcome      = ApprovalOutcome.DEPT_MANAGER
        applied_rule = "dept_manager_gl_flagged_override"
        reasoning   += (
            " One or more lines are unclassified (all_classified=False) — "
            "auto-approve downgraded to DEPT_MANAGER."
        )

    # ── Step 4: Return result ─────────────────────────────────────────────────
    return ApprovalRoutingResult(
        outcome              = outcome,
        applied_rule         = applied_rule,
        total_amount         = total_amount,
        department           = department,
        has_capitalize       = has_capitalize,
        all_lines_classified = all_classified,
        reasoning            = reasoning,
    )
