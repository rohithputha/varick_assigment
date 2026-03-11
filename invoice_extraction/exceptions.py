class StructurizerError(Exception):
    """Raised when line_item_structurizer cannot parse raw invoice text into line items."""
    pass


class IngestionError(Exception):
    """Raised for unrecoverable ingestion failures (non-LLM errors)."""
    pass
