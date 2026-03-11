"""
End-to-end pipeline runner on labeled_invoices.json.

Stages: Invoice Ingestion → PO Matching → GL Classification

PO matching is forced to MATCHED for all invoices (FORCE_MATCH=True)
so that GL Classification runs on every invoice, including INV-006 (no PO).

Ground truth comparison covers GL Classification (SOP Step 2) only.
  - INV-004 ACCRUAL entries are Step 3 work; Step 2 expected outputs
    are 5040 EXPENSE (consulting) and 5060 EXPENSE (travel).
  - INV-006 has no PO; with FORCE_MATCH the classifier runs — note added.

Usage:
    python3 run_pipeline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ── Force PO matching to always return MATCHED ───────────────────────────────
import po_matching.matchers.po_validator as _pov
_pov.FORCE_MATCH = True

from pipeline.db import SQLiteDB
from pipeline.orchestrator import Orchestrator

# ── Ground truth for GL Classification (Step 2 only) ────────────────────────
# INV-004: Step 3 will assign ACCRUAL; Step 2 assigns EXPENSE.
# We record Step-2-only expected values here.
_GL_EXPECTED: dict[str, dict[int, dict]] = {
    "INV-001": {1: {"gl": "1310", "treatment": "PREPAID",    "base": "5010"}},
    "INV-002": {
        1: {"gl": "5030", "treatment": "EXPENSE",    "base": None},
        2: {"gl": "5040", "treatment": "EXPENSE",    "base": None},
        3: {"gl": "5030", "treatment": "EXPENSE",    "base": None},
    },
    "INV-003": {
        1: {"gl": "5110", "treatment": "EXPENSE",    "base": None},
        2: {"gl": "1500", "treatment": "CAPITALIZE", "base": None},
        3: {"gl": "1300", "treatment": "PREPAID",    "base": "5020"},
    },
    "INV-004": {
        # Step 2 assigns EXPENSE; Step 3 upgrades to ACCRUAL (not tested here)
        1: {"gl": "5040", "treatment": "EXPENSE",    "base": None},
        2: {"gl": "5060", "treatment": "EXPENSE",    "base": None},
    },
    "INV-005": {
        1: {"gl": "5050", "treatment": "EXPENSE",    "base": None},
        2: {"gl": "5000", "treatment": "EXPENSE",    "base": None},
        3: {"gl": "5050", "treatment": "EXPENSE",    "base": None},
        4: {"gl": "5000", "treatment": "EXPENSE",    "base": None},
    },
    # INV-006: no PO → would normally be blocked; FORCE_MATCH bypasses that.
    # No GL expected — just run and report what the classifier assigns.
    "INV-006": {},
}

_PASS = "\033[92m✓\033[0m"
_FAIL = "\033[91m✗\033[0m"
_SKIP = "\033[93m~\033[0m"


def _check(got, expected, label: str) -> tuple[bool, str]:
    if expected is None:
        return True, f"{_SKIP} {label}: {got} (no expectation)"
    match = str(got) == str(expected)
    icon  = _PASS if match else _FAIL
    return match, f"{icon} {label}: got={got!r}  expected={expected!r}"


def run_all() -> None:
    data_path = Path(__file__).parent / "data" / "labeled_invoices.json"
    invoices  = json.loads(data_path.read_text())

    db = SQLiteDB(":memory:")
    db.create_tables()
    orch = Orchestrator(db)

    total_lines  = 0
    passed_lines = 0
    invoice_pass = 0

    for entry in invoices:
        raw = entry["raw_input"]
        inv_id = raw.get("invoice_id", "?")

        print(f"\n{'═'*60}")
        print(f"  {inv_id}  —  {raw.get('vendor_name')}  |  {raw.get('total_amount')} USD")
        print(f"{'═'*60}")

        result = orch.run(raw)

        if result["status"] == "FAILED":
            print(f"  [PIPELINE FAILED] {result.get('error', '')}")
            continue

        if result["status"] == "HALTED":
            print(f"  [HALTED at {result['stage']}] reason={result['reason']}")
            continue

        # Unwrap nested stub payloads to find the prepaid_accrual output.
        output = result.get("output", {})
        depth = 0
        while "line_results" not in output and "payload" in output and depth < 10:
            output = output["payload"]
            depth += 1

        # If prepaid_accrual layer found, use it; else fall back to GL layer
        if "line_results" in output:
            gl_output    = output.get("classified_invoice", output)
            pa_output    = output
        else:
            # Fallback: no prepaid_accrual layer — find GL layer
            gl_output = output
            depth2 = 0
            while "line_classifications" not in gl_output and "payload" in gl_output and depth2 < 10:
                gl_output = gl_output["payload"]
                depth2 += 1
            pa_output = None

        line_classifications = gl_output.get("line_classifications", [])
        line_results         = pa_output.get("line_results", []) if pa_output else []
        notes                = pa_output.get("notes", []) if pa_output else gl_output.get("notes", [])

        # Build lookup: line_number → prepaid_accrual result
        pa_by_ln = {r["line_number"]: r for r in line_results}

        if notes:
            print("  Agent notes:")
            for n in notes:
                print(f"    • {n}")

        expected_lines = _GL_EXPECTED.get(inv_id, {})
        inv_all_pass   = True

        if not line_classifications:
            print("  [no line_classifications in output]")
            continue

        print()
        for lc in line_classifications:
            ln        = lc["line_number"]
            gl        = lc.get("gl_account")
            treatment = lc.get("treatment")
            base      = lc.get("base_expense_account")
            conf      = lc.get("confidence", 0.0)
            rule      = lc.get("applied_rule", "?")
            flagged   = lc.get("flagged", False)
            flag_why  = lc.get("flag_reason", "")

            # Step 3 enrichment for this line
            pa        = pa_by_ln.get(ln, {})
            final_tr  = pa.get("final_treatment", treatment)
            final_gl  = pa.get("final_gl_account", gl)
            prepaid   = pa.get("prepaid_result")
            accrual   = pa.get("accrual_result")
            skipped   = pa.get("skipped", False)
            ln_notes  = pa.get("notes", [])

            exp = expected_lines.get(ln)

            print(f"  Line {ln}:")
            print(f"    Step2  GL={gl or '—'}  treatment={treatment or '—'}  conf={conf:.2f}  rule={rule}")
            if pa:
                print(f"    Step3  GL={final_gl or '—'}  treatment={final_tr or '—'}", end="")
                if accrual:
                    print(f"  accrual→{accrual['accrual_account']}  expense={accrual['expense_account']}  reversal={accrual['reversal_trigger']}", end="")
                if prepaid:
                    print(f"  prepaid→{prepaid['prepaid_account']}  expense={prepaid['expense_account']}  months={prepaid['amortization_months']}", end="")
                print()
            if ln_notes:
                for n in ln_notes:
                    print(f"    note: {n}")

            if flagged:
                print(f"    {_SKIP} FLAGGED: {flag_why}")
                if exp:
                    inv_all_pass = False
                continue

            line_ok = True
            if exp:
                gl_ok, gl_msg = _check(gl,        exp["gl"],        "step2 gl_account")
                tr_ok, tr_msg = _check(treatment,  exp["treatment"], "step2 treatment")
                ba_ok, ba_msg = _check(base,       exp["base"],      "base_expense")
                print(f"    {gl_msg}")
                print(f"    {tr_msg}")
                print(f"    {ba_msg}")
                line_ok = gl_ok and tr_ok and ba_ok
                total_lines += 1
                if line_ok:
                    passed_lines += 1
                else:
                    inv_all_pass = False
            else:
                print(f"    {_SKIP} no ground-truth expectation for this line")

        if expected_lines:
            invoice_pass += int(inv_all_pass)
            status = f"{_PASS} ALL PASS" if inv_all_pass else f"{_FAIL} SOME FAILURES"
            print(f"\n  Invoice result: {status}")

    print(f"\n{'═'*60}")
    print(f"  SUMMARY")
    print(f"{'═'*60}")
    print(f"  Lines:    {passed_lines}/{total_lines} passed")
    invoices_with_gt = sum(1 for inv in invoices if _GL_EXPECTED.get(inv["raw_input"]["invoice_id"]))
    print(f"  Invoices: {invoice_pass}/{invoices_with_gt} fully correct")
    print()


if __name__ == "__main__":
    run_all()
