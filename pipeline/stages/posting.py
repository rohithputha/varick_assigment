"""
PostingRunner — wraps run_posting_agent for use in the pipeline.
"""
from __future__ import annotations

from pipeline.stages.base import StageRunner


class PostingRunner(StageRunner):
    """
    Wraps run_posting_agent.

    input_payload: Approval Routing output dict (RoutedInvoice dict).
    May also contain {"approved": True/False} merged in by the orchestrator on resume
    to signal that human approval has been granted or denied.

    output_payload on success: PostingResult dict with {"halted": False}.
    HALTs if routing outcome is DEPT_MANAGER or VP_FINANCE and approved != True.
    """

    def run(self, run_id: str, input_payload: dict) -> dict:
        try:
            from posting.agent import run_posting_agent
            result = run_posting_agent(input_payload, run_id=run_id)
            if "halted" not in result:
                result["halted"] = False
            return result
        except Exception as e:
            return {"halted": False, "success": False, "error": str(e)}
