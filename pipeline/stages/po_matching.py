"""
POMatchingRunner — wraps run_po_matching_agent for use in the pipeline.
"""
from __future__ import annotations

from pipeline.stages.base import StageRunner


class POMatchingRunner(StageRunner):
    """
    Wraps run_po_matching_agent.

    input_payload: Full INGESTION output dict.
    The invoice dict may be nested under input_payload["invoice"].
    This runner unwraps it before passing to run_po_matching_agent.

    PO Matching v1 never halts — mismatches are flagged and passed through.
    """

    def run(self, run_id: str, input_payload: dict) -> dict:
        try:
            from po_matching.agent import run_po_matching_agent

            # Ingestion wraps the invoice dict under "invoice" key.
            # Support both flat (invoice_id at top level) and nested shapes.
            invoice_dict = input_payload.get("invoice", input_payload)

            result = run_po_matching_agent(invoice_dict)

            # Normalize: ensure halted key is always present
            if "halted" not in result:
                result["halted"] = False

            return result

        except Exception as e:
            return {"halted": False, "success": False, "error": str(e)}
