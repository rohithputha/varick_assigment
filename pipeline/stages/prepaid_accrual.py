"""
PrepaidAccrualRunner — wraps run_prepaid_accrual_agent for use in the pipeline.
"""
from __future__ import annotations

from pipeline.stages.base import StageRunner


class PrepaidAccrualRunner(StageRunner):
    """
    Wraps run_prepaid_accrual_agent.

    input_payload: GL Classification output dict (ClassifiedInvoice dict).
    output_payload on success: RecognizedInvoice dict with {"halted": False}.
    Prepaid/Accrual Recognition never halts.
    """

    def run(self, run_id: str, input_payload: dict) -> dict:
        try:
            from prepaid_accrual.agent import run_prepaid_accrual_agent

            result = run_prepaid_accrual_agent(input_payload)

            if "halted" not in result:
                result["halted"] = False

            return result

        except Exception as e:
            return {"halted": False, "success": False, "error": str(e)}
