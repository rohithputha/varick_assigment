"""
Outer pipeline orchestrator for Varick AP automation.

Usage:
    from pipeline.db import SQLiteDB
    from pipeline.orchestrator import Orchestrator

    db = SQLiteDB()           # defaults to pipeline.db in project root
    db.create_tables()
    orch = Orchestrator(db)

    result = orch.run("/path/to/invoice.json")
    result = orch.run({"vendor_name": "Acme", ...})

    # Resume a HALTED run
    result = orch.resume(run_id, corrected_input={...})

    # Query status + audit trail
    status = orch.get_status(run_id)
"""

from pipeline.db           import SQLiteDB
from pipeline.orchestrator import Orchestrator

__all__ = ["SQLiteDB", "Orchestrator"]
