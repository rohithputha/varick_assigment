"""
Eval entry point — runs all 6 labeled invoices through the pipeline N times
and reports accuracy across GL Classification, Final Treatment, and Approval Routing.

Usage:
    python3 evals/run_eval.py           # N=5 (default)
    python3 evals/run_eval.py --n 1     # single run (fast, no averaging)
    python3 evals/run_eval.py --n 10    # more runs for variance analysis

Exit code:
    0  — all invoices pass@1
    1  — at least one invoice failed on the first run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure the project root is on sys.path whether this file is run directly
# (python3 evals/run_eval.py) or as a module (python3 -m evals.run_eval).
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.comparator import compare_invoice
from evals.ground_truth import get_ground_truth
from evals.reporter import print_invoice_result, print_summary
from evals.runner import run_invoice_n_times


DATA_PATH = Path(__file__).parent.parent / "data" / "labeled_invoices.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="AP Pipeline eval harness")
    parser.add_argument("--n", type=int, default=5,
                        help="Number of runs per invoice (default: 5)")
    args = parser.parse_args()

    n  = max(1, args.n)
    gt = get_ground_truth()

    invoices = json.loads(DATA_PATH.read_text())

    all_results = []

    for entry in invoices:
        raw       = entry["raw_input"]
        inv_id    = raw.get("invoice_id", "?")
        inv_gt    = gt.get(inv_id)

        if inv_gt is None:
            print(f"\n[WARN] No ground truth for {inv_id} — skipping")
            continue

        # INV-006 has no PO — run without FORCE_MATCH to test real blocked behavior
        force_match = not inv_gt.expected_blocked

        print(f"\nRunning {inv_id} × {n}  (force_match={force_match})  ...", flush=True)
        bundles = run_invoice_n_times(raw, force_match=force_match, n=n)

        inv_result = compare_invoice(bundles, inv_gt)
        all_results.append(inv_result)

        print_invoice_result(inv_result, raw)

    print_summary(all_results)

    # Exit 1 if any non-blocked invoice failed on the first run
    non_blocked = [r for r in all_results if not r.gt.expected_blocked]
    all_pass_at_1 = all(r.pass_at_1 for r in non_blocked)
    return 0 if all_pass_at_1 else 1


if __name__ == "__main__":
    sys.exit(main())
