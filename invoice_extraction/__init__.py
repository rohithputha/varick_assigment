"""
Invoice Extraction Module — transforms raw vendor invoice data into a clean,
validated, signal-enriched Invoice object ready for PO Matching.
"""

from invoice_extraction.agent import run_ingestion_agent
from invoice_extraction.state import StateManager

__all__ = ["run_ingestion_agent", "StateManager"]
