"""
Routing tools — thin wrapper over the rule engine for the agent.

route(recognized_invoice_dict) → dict

Never raises; exceptions are caught and returned as a DENY/fail_closed_deny result.
"""
from __future__ import annotations

from decimal import Decimal

from approval_routing.router.rules import route_invoice
from models import ApprovalOutcome


def route(recognized_invoice_dict: dict) -> dict:
    """
    Call the routing rule engine and return a serialised routing result dict.

    Returns:
        {
          "success":              bool,
          "outcome":              str,       # ApprovalOutcome value
          "applied_rule":         str,
          "total_amount":         str,       # Decimal serialised as string
          "department":           str | None,
          "has_capitalize":       bool,
          "all_lines_classified": bool,
          "reasoning":            str,
        }
    """
    try:
        result = route_invoice(recognized_invoice_dict)
        return {
            "success":              True,
            "outcome":              result.outcome.value,
            "applied_rule":         result.applied_rule,
            "total_amount":         str(result.total_amount),
            "department":           result.department,
            "has_capitalize":       result.has_capitalize,
            "all_lines_classified": result.all_lines_classified,
            "reasoning":            result.reasoning,
        }
    except Exception as exc:
        return {
            "success":              False,
            "outcome":              ApprovalOutcome.DENY.value,
            "applied_rule":         "fail_closed_deny",
            "total_amount":         str(Decimal("0")),
            "department":           None,
            "has_capitalize":       False,
            "all_lines_classified": False,
            "reasoning":            f"routing_error: {exc}",
        }
