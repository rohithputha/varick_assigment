"""
ApprovalRoutingRunner — wraps run_approval_routing_agent for use in the pipeline.
"""
from __future__ import annotations

from pipeline.stages.base import StageRunner


class ApprovalRoutingRunner(StageRunner):
    """
    Wraps run_approval_routing_agent.

    input_payload: Prepaid/Accrual output dict (RecognizedInvoice dict).
    output_payload on success: RoutedInvoice dict with {"halted": False}.
    Approval Routing never halts.
    """

    def run(self, run_id: str, input_payload: dict) -> dict:
        try:
            from approval_routing.agent import run_approval_routing_agent
            result = run_approval_routing_agent(input_payload)
            if "halted" not in result:
                result["halted"] = False
            return result
        except Exception as e:
            return {"halted": False, "success": False, "error": str(e)}
