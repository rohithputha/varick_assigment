"""
pipeline/cli.py — Command-line interface for the AP automation pipeline.

Runs the full pipeline end-to-end and handles all HALT interactions:
  - Approval prompts (DEPT_MANAGER / VP_FINANCE)
  - Ingestion HITL corrections (future — scaffolded here)

Usage:
    python3 pipeline/cli.py <invoice_file_or_json_path>

    # or from Python:
    from pipeline.cli import run_pipeline_cli
    result = run_pipeline_cli("/path/to/invoice.json")
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal


def _print_divider() -> None:
    print("─" * 54)


def _approval_prompt(halt_result: dict) -> bool | None:
    """
    Display the approval prompt and return:
      True  → user approved
      False → user rejected
      (loops until valid input)
    """
    routing = halt_result.get("routing", {})
    reason       = halt_result.get("reason", "pending_approval")
    invoice_id   = halt_result.get("invoice_id", "unknown")
    total_amount = routing.get("total_amount", "unknown")
    department   = routing.get("department") or "N/A"
    applied_rule = routing.get("applied_rule", "unknown")

    _print_divider()
    print(f"  APPROVAL REQUIRED — {reason}")
    print(f"  Invoice:    {invoice_id}")
    print(f"  Total:      ${total_amount}")
    print(f"  Department: {department}")
    print(f"  Rule:       {applied_rule}")
    _print_divider()

    while True:
        raw = input("  Approve? [yes/no]: ").strip().lower()
        if raw == "yes":
            return True
        elif raw == "no":
            return False
        else:
            print("  Please enter yes or no.")


def _print_result(result: dict) -> None:
    """Pretty-print the final pipeline result."""
    _print_divider()
    status = result.get("status", "UNKNOWN")
    print(f"  Pipeline status: {status}")

    if status == "COMPLETE":
        output = result.get("output", {})
        posting_status = output.get("posting_status", "unknown")
        total_posted   = output.get("total_posted", "0")
        entries_count  = len(output.get("journal_entries", []))
        skipped        = output.get("skipped_lines", [])
        reconciled     = output.get("amounts_reconciled", False)

        print(f"  Posting status:  {posting_status}")
        print(f"  Journal entries: {entries_count}")
        print(f"  Total posted:    ${total_posted}")
        print(f"  Reconciled:      {reconciled}")
        if skipped:
            print(f"  Skipped lines:   {skipped}")
        notes = output.get("notes", [])
        if notes:
            print("  Notes:")
            for n in notes:
                print(f"    • {n}")

    elif status == "DRY_RUN":
        routing = result.get("routing", {})
        print(f"  Outcome:      {result.get('outcome', 'unknown')}")
        print(f"  Applied rule: {result.get('applied_rule', 'unknown')}")
        print(f"  Total:        ${routing.get('total_amount', 'unknown')}")
        print(f"  Department:   {routing.get('department', 'N/A')}")
        print(f"  Reasoning:    {result.get('reasoning', '')}")

    elif status == "HALTED":
        print(f"  Stage:  {result.get('stage', '')}")
        print(f"  Reason: {result.get('reason', '')}")

    elif status == "FAILED":
        print(f"  Stage: {result.get('stage', '')}")
        print(f"  Error: {result.get('error', '')}")

    _print_divider()


def run_pipeline_cli(invoice_input: str | dict, dry_run: bool = False) -> dict:
    """
    Run the full pipeline and handle all HALT interactions interactively.

    Args:
        invoice_input: Path to invoice JSON file (str) or raw invoice dict.
        dry_run:       If True, stop after Approval Routing (skip Posting).

    Returns:
        Final orchestrator result dict.
    """
    from pipeline.db import SQLiteDB
    from pipeline.orchestrator import Orchestrator

    db = SQLiteDB()
    db.create_tables()
    orch = Orchestrator(db)

    print(f"\n  Starting pipeline run{'  [DRY RUN]' if dry_run else ''}…")
    result = orch.run(invoice_input, dry_run=dry_run)

    # Loop to handle sequential HALTs (e.g., approval then ingestion correction)
    while result.get("status") == "HALTED":
        run_id = result["run_id"]
        reason = result.get("reason", "")

        if reason in ("pending_dept_manager_approval", "pending_vp_finance_approval"):
            # Inject invoice_id from the run status for display
            try:
                status_info = orch.get_status(run_id)
                result["invoice_id"] = status_info.get("invoice_id") or "unknown"
            except Exception:
                pass

            approved = _approval_prompt(result)
            if approved:
                result = orch.resume(run_id, corrected_input={"approved": True})
            else:
                print("  Invoice rejected.")
                result = orch.resume(run_id, corrected_input={"approved": False})

        else:
            # Ingestion HITL or other halt — not handled interactively in v1
            print(f"\n  Pipeline halted at stage '{result.get('stage')}': {reason}")
            print("  Manual intervention required. Use orch.resume() to continue.")
            break

    _print_result(result)
    return result


def main() -> None:
    # Support both:
    #   python3 pipeline/cli.py <path>
    #   python -m pipeline.cli run --input <path>
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if args and args[0] == "run":
        try:
            idx = args.index("--input")
            invoice_path = args[idx + 1]
        except (ValueError, IndexError):
            print("Usage: python -m pipeline.cli run --input <invoice_json_path> [--dry-run]", file=sys.stderr)
            sys.exit(1)
    elif args:
        invoice_path = args[0]
    else:
        print("Usage: python3 pipeline/cli.py <invoice_json_path> [--dry-run]", file=sys.stderr)
        sys.exit(1)

    if invoice_path.endswith(".json"):
        try:
            with open(invoice_path) as f:
                invoice_input = json.load(f)
        except FileNotFoundError:
            print(f"File not found: {invoice_path}", file=sys.stderr)
            sys.exit(1)
    else:
        invoice_input = invoice_path   # let orchestrator handle file path

    if isinstance(invoice_input, list):
        print(f"\n  Found {len(invoice_input)} invoice(s) — running in batch mode.")
        for i, item in enumerate(invoice_input, 1):
            # Support labeled_invoices.json format: {"raw_input": {...}, "ground_truth": {...}}
            invoice = item.get("raw_input", item) if isinstance(item, dict) else item
            invoice_id = invoice.get("invoice_id", f"#{i}") if isinstance(invoice, dict) else f"#{i}"
            print(f"\n  [{i}/{len(invoice_input)}] Invoice: {invoice_id}")
            run_pipeline_cli(invoice, dry_run=dry_run)
    else:
        run_pipeline_cli(invoice_input, dry_run=dry_run)


if __name__ == "__main__":
    main()
