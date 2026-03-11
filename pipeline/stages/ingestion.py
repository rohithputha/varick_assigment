"""
IngestionRunner — wraps run_ingestion_agent for use in the pipeline.
"""
from __future__ import annotations

from pipeline.stages.base import StageRunner


class IngestionRunner(StageRunner):
    """
    Wraps run_ingestion_agent.

    input_payload format:
        {"input": str | dict}                          # new run
        {"input": dict, "resume_state_id": str}        # HITL resume

    output_payload on success:
        The full result dict from run_ingestion_agent (contains Invoice under "invoice"),
        with {"halted": False} normalized in.

    output_payload on halt:
        {"halted": True, "state_id": "<uuid>", "reason": "AMOUNT_MISMATCH"}
    """

    def run(self, run_id: str, input_payload: dict) -> dict:
        try:
            from invoice_extraction.agent import run_ingestion_agent

            raw_input       = input_payload["input"]
            resume_state_id = input_payload.get("resume_state_id")

            result = run_ingestion_agent(raw_input, resume_state_id=resume_state_id)

            # Normalize: ensure halted key is always present
            if "halted" not in result:
                result["halted"] = False

            return result

        except Exception as e:
            return {"halted": False, "success": False, "error": str(e)}
