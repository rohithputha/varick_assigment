"""
GLClassificationRunner — wraps run_gl_classification_agent for use in the pipeline.
"""
from __future__ import annotations

from pipeline.stages.base import StageRunner


class GLClassificationRunner(StageRunner):
    """
    Wraps run_gl_classification_agent.

    input_payload: PO Matching output dict.
        Must contain:
          - "invoice":  full Invoice dict (passed through from POMatchingRunner)
          - "matched":  bool
          - ... other PO match fields

    output_payload on success: ClassifiedInvoice dict with {"halted": False}.
    GL Classification never halts.
    """

    def run(self, run_id: str, input_payload: dict) -> dict:
        try:
            from gl_classification.agent import run_gl_classification_agent

            # PO matching passes the invoice dict through under "invoice".
            # Fall back to the full payload if "invoice" key is absent.
            invoice_dict  = input_payload.get("invoice", input_payload)
            po_match_dict = input_payload

            result = run_gl_classification_agent(invoice_dict, po_match_dict)

            if "halted" not in result:
                result["halted"] = False

            return result

        except Exception as e:
            return {"halted": False, "success": False, "error": str(e)}
