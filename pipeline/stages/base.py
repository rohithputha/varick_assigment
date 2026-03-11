"""
StageRunner ABC — contract for all pipeline stage runners.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class StageRunner(ABC):
    """
    Abstract base for all stage runners.

    Contract:
      - run() accepts (run_id, input_payload) and returns a dict.
      - If the stage signals HALT, the returned dict MUST contain {"halted": True}.
      - run() NEVER raises — all exceptions are caught and returned as
        {"halted": False, "success": False, "error": "<message>"}.
      - The orchestrator inspects the returned dict; it does not catch exceptions
        from run().

    run_id is provided so runners can write auxiliary state if needed.
    Most v1 runners ignore run_id entirely.
    """

    @abstractmethod
    def run(self, run_id: str, input_payload: dict) -> dict:
        """
        Execute the stage.

        Args:
            run_id:        The orchestrator's run ID (for traceability).
            input_payload: Output of the previous stage (or raw input for INGESTION).

        Returns:
            dict with at minimum {"halted": bool}

            On success:
                {"halted": False, "success": True, ...stage-specific fields...}

            On halt:
                {"halted": True, "state_id": str, "reason": str}   # INGESTION halt
                {"halted": True, "reason": str}                     # future stage halts

            On unexpected error:
                {"halted": False, "success": False, "error": str}
        """
        ...
