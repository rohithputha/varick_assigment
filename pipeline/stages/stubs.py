"""
Pass-through stubs for unimplemented pipeline stages.
GL Classification, Prepaid/Accrual, Approval Routing, Posting.
"""
from __future__ import annotations

from pipeline.stages.base import StageRunner


class _PassThroughStub(StageRunner):
    """
    Pass-through stub for unimplemented stages.
    Returns the input_payload unchanged with a stub marker.
    Never halts. Never fails.
    """

    def __init__(self, stage_name: str) -> None:
        self._stage_name = stage_name

    def run(self, run_id: str, input_payload: dict) -> dict:
        return {
            "halted":  False,
            "success": True,
            "stub":    True,
            "stage":   self._stage_name,
            "note":    f"{self._stage_name} not yet implemented — passing through",
            "payload": input_payload,
        }


class GLClassificationRunner(_PassThroughStub):
    def __init__(self) -> None:
        super().__init__("GL_CLASSIFICATION")


class PrepaidAccrualRunner(_PassThroughStub):
    def __init__(self) -> None:
        super().__init__("PREPAID_ACCRUAL")


class ApprovalRoutingRunner(_PassThroughStub):
    def __init__(self) -> None:
        super().__init__("APPROVAL_ROUTING")


class PostingRunner(_PassThroughStub):
    def __init__(self) -> None:
        super().__init__("POSTING")
