from __future__ import annotations

from enum import Enum


class IngestionStage(str, Enum):
    """Internal stage lifecycle for the invoice ingestion pipeline."""
    INIT          = "INIT"
    LOADED        = "LOADED"
    HEADER_PARSED = "HEADER_PARSED"
    LINES_PARSED  = "LINES_PARSED"
    VALIDATED     = "VALIDATED"
    COMPLETE      = "COMPLETE"
    FAILED        = "FAILED"
