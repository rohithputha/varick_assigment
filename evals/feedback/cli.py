"""
Feedback Loop CLI.

Usage:
    # Analyze corrections collected so far
    python -m evals.feedback.cli --analyze

    # Propose changes (programmatic refiner — no LLM)
    python -m evals.feedback.cli --propose

    # Full pipeline: analyze → refiner → LLM review → simulate → confirm → apply
    python -m evals.feedback.cli --apply

    # Benchmark snapshots
    python -m evals.feedback.cli --benchmark before
    python -m evals.feedback.cli --benchmark after
    python -m evals.feedback.cli --benchmark compare
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline.db import SQLiteDB
from evals.feedback.analyzer import FeedbackAnalyzer
from evals.feedback.refiner import RuleRefiner
from evals.feedback.reviewer import RuleReviewAgent
from evals.feedback.benchmark import FeedbackBenchmark
from rules_engine.rules_tools import get_rules
from approval_routing.threshold_tools import get_thresholds


_DB_PATH   = Path(__file__).parent.parent.parent / "pipeline.db"
_SEPARATOR = "─" * 65


def _get_db() -> SQLiteDB:
    db = SQLiteDB(_DB_PATH)
    db.create_tables()
    return db


# ---------------------------------------------------------------------------
# --analyze
# ---------------------------------------------------------------------------

def cmd_analyze() -> None:
    db       = _get_db()
    analyzer = FeedbackAnalyzer(db)
    report   = analyzer.analyze()

    print(f"\n{'═' * _SEPARATOR.__len__()}")
    print("  FEEDBACK ANALYSIS")
    print(f"{'═' * _SEPARATOR.__len__()}")
    print(f"\nTotal corrections: {report.total_corrections}")

    if not report.total_corrections:
        print("  No corrections found. Run --submit to ingest corrections first.")
        return

    print("\nBy stage:")
    for stage, count in sorted(report.by_stage.items()):
        print(f"  {stage:<25} {count}")

    print("\nBy field:")
    for field, count in sorted(report.by_field.items(), key=lambda x: -x[1]):
        print(f"  {field:<30} {count}")

    if report.systematic_errors:
        print(f"\nSystematic errors ({len(report.systematic_errors)} pattern(s)):")
        for p in report.systematic_errors:
            rule_note = f"  → likely rule: {p.affected_rule_id}" if p.affected_rule_id else ""
            print(f"  {p.field}: {p.proposed_value!r}→{p.corrected_value!r} "
                  f"(freq={p.frequency}, invoices: {', '.join(p.affected_invoices)}){rule_note}")
    else:
        print("\nNo systematic errors found (all corrections are one-offs).")

    if report.top_problem_rules:
        print(f"\nTop problem rules (error rate > 25%):")
        for rid in report.top_problem_rules:
            rate = report.rule_error_rates.get(rid, 0)
            print(f"  {rid:<40} {rate * 100:.0f}%")


# ---------------------------------------------------------------------------
# --propose
# ---------------------------------------------------------------------------

def cmd_propose() -> None:
    db       = _get_db()
    analyzer = FeedbackAnalyzer(db)
    report   = analyzer.analyze()
    refiner  = RuleRefiner()

    if not report.systematic_errors:
        print("No systematic errors to propose changes for.")
        return

    changes = refiner.propose_changes(report)
    if not changes:
        print("No actionable changes generated.")
        return

    print(f"\nGenerated {len(changes)} rule change(s):")
    print(refiner.preview(changes))


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------

def cmd_apply() -> None:
    db = _get_db()
    from pipeline.state_manager import GlobalStateManager
    gsm = GlobalStateManager(db)

    print(f"\n{_SEPARATOR}")
    print("  FEEDBACK LOOP — APPLY")
    print(_SEPARATOR)

    # Step 1: Analyze
    analyzer = FeedbackAnalyzer(db)
    report   = analyzer.analyze()

    if not report.total_corrections:
        print("No corrections found. Nothing to apply.")
        return

    print(f"\nAnalyzing {report.total_corrections} correction record(s)...")

    if not report.systematic_errors:
        print("No systematic errors detected. Nothing to propose.")
        return

    print(f"Found {len(report.systematic_errors)} systematic error(s):")
    for p in report.systematic_errors:
        rule_note = f"\n    → likely rule: {p.affected_rule_id}" if p.affected_rule_id else ""
        print(f"  {p.field}: {p.proposed_value!r}→{p.corrected_value!r} "
              f"(freq={p.frequency}, invoices: {', '.join(p.affected_invoices)}){rule_note}")

    # Step 2: Generate changes
    refiner = RuleRefiner()
    changes = refiner.propose_changes(report)

    if not changes:
        print("\nNo actionable changes generated from error patterns.")
        return

    print(f"\nGenerated {len(changes)} rule change(s):")
    for c in changes:
        print(f"  [{c.change_id}] {c.change_type}: {c.api_call}")

    # Step 3: LLM review
    print(f"\nSending to LLM reviewer (Sonnet) for validation...")
    try:
        current_rules      = get_rules()
        current_thresholds = get_thresholds()
        raw_corrections    = analyzer._collector.get_all_corrections()

        reviewer = RuleReviewAgent()
        verdicts = reviewer.review(
            proposed_changes=changes,
            error_report=report,
            current_rules=current_rules,
            current_thresholds=current_thresholds,
            raw_corrections=raw_corrections,
        )
    except Exception as e:
        print(f"\nERROR: LLM review failed: {e}")
        print("Proceeding without LLM review — all changes require manual confirmation.")
        verdicts = []

    # Display verdicts and build approved list
    verdict_map = {v.change_id: v for v in verdicts}
    approved_changes = []
    print()
    for c in changes:
        v = verdict_map.get(c.change_id)
        if v:
            icon = "✓" if v.verdict == "APPROVE" else ("~" if v.verdict == "MODIFY" else "✗")
            print(f"  [{c.change_id}] {v.verdict:<8}  (conf={v.confidence:.2f})  {v.reasoning}")
            if v.verdict == "APPROVE":
                approved_changes.append(c)
            elif v.verdict == "MODIFY" and v.modified_change:
                approved_changes.append(v.modified_change)
                print(f"           modified: {v.modified_change.field}={v.modified_change.new_value!r}")
        else:
            # No verdict → require explicit user approval
            print(f"  [{c.change_id}] NO VERDICT — skipped")

    if not approved_changes:
        print("\nAll changes rejected or no verdicts. Nothing to apply.")
        return

    # Step 4: Simulate
    print(f"\nSimulating {len(approved_changes)} change(s) (not yet applied)...")
    try:
        sim_result = refiner.simulate(approved_changes)
        _print_simulation(sim_result, approved_changes)
    except Exception as e:
        print(f"  WARNING: simulation failed: {e}")
        print("  Proceeding without simulation — accuracy delta unknown.")
        sim_result = None

    # Step 5: Human confirmation
    change_ids_str = ", ".join(c.change_id for c in approved_changes)
    print(f"\nApply changes {change_ids_str}? [y/N] ", end="", flush=True)
    choice = input().strip().lower()

    if choice != "y":
        print("Aborted. No changes applied.")
        return

    # Step 6: Apply
    print()
    all_feedback_ids = [r.feedback_id for r in analyzer._collector.get_all_corrections()
                        if not r.applied]

    for c in approved_changes:
        print(f"Calling {c.api_call}... ", end="", flush=True)
        try:
            from rules_engine import rules_tools as rrt
            from approval_routing import threshold_tools as tt
            if c.rule_system == "gl":
                if c.change_type == "DISABLE_RULE":
                    result = rrt.disable_rule(c.rule_id)
                elif c.change_type == "ADD_NORMALISATION":
                    parts = c.field.split(":")
                    result = rrt.add_normalisation(parts[0], c.new_value, parts[1] if len(parts) > 1 else "category")
                else:
                    result = rrt.update_rule(c.rule_id, c.field, c.new_value)
            elif c.rule_system == "approval_threshold":
                result = tt.update_threshold(c.rule_id, c.new_value)
            else:
                result = {"success": False, "error": "unknown rule_system"}

            if result.get("success", True):
                print("✓")
            else:
                print(f"✗  {result.get('error', 'unknown error')}")
        except Exception as e:
            print(f"✗  {e}")

    # Mark feedback records as applied
    if all_feedback_ids:
        analyzer._collector.mark_applied(all_feedback_ids)

    new_version = get_rules().get("version", "?")
    print(f"\nrules.json bumped to v{new_version}")
    print("\nRun --benchmark after to measure real improvement.")


def _print_simulation(sim, changes) -> None:
    """Print simulation result table."""
    def _fmt(b: float, a: float) -> str:
        pb = f"{b * 100:.0f}%"
        pa = f"{a * 100:.0f}%"
        diff = (a - b) * 100
        sign = "+" if diff >= 0 else ""
        tag  = "✓" if diff > 0 else ("✗" if diff < 0 else "—")
        return f"{pb} → {pa}  ({sign}{diff:.0f} pp)  {tag}"

    print(f"Simulation result (changes not yet applied):")
    print(f"  GL accuracy:       {_fmt(sim.gl_accuracy_before,       sim.gl_accuracy_after)}")
    print(f"  Treatment:         {_fmt(sim.treatment_accuracy_before, sim.treatment_accuracy_after)}")
    print(f"  Approval routing:  {_fmt(sim.approval_accuracy_before,  sim.approval_accuracy_after)}")
    if sim.improved_invoices:
        print(f"\n  Improved: {', '.join(sim.improved_invoices)}")
    if sim.regressed_invoices:
        print(f"  Regressed: {', '.join(sim.regressed_invoices)}")


# ---------------------------------------------------------------------------
# --benchmark
# ---------------------------------------------------------------------------

def cmd_benchmark(subcommand: str) -> None:
    db        = _get_db()
    benchmark = FeedbackBenchmark(db)

    if subcommand in ("before", "after"):
        print(f"\nRunning labeled invoice evals for '{subcommand}' snapshot...")
        snap = benchmark.snapshot(label=subcommand)
        print(f"\nSnapshot captured ({snap.snapshot_id[:8]}):")
        print(f"  Label:        {snap.label}")
        print(f"  GL version:   {snap.rules_gl_version}")
        print(f"  Thresholds:   {snap.threshold_version}")
        print(f"  GL accuracy:       {snap.gl_accuracy * 100:.0f}%")
        print(f"  Treatment:         {snap.treatment_accuracy * 100:.0f}%")
        print(f"  Approval routing:  {snap.approval_accuracy * 100:.0f}%")
        print(f"  Overall:           {snap.overall_accuracy * 100:.0f}%")

    elif subcommand == "compare":
        try:
            delta = benchmark.compare("before", "after")
            print("\n" + benchmark.report(delta))
        except ValueError as e:
            print(f"\nERROR: {e}")
            print("Run --benchmark before and --benchmark after first.")

    else:
        print(f"Unknown benchmark subcommand: {subcommand!r}")
        print("Use: before | after | compare")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Feedback loop — analyze corrections, propose and apply rule changes"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--analyze",   action="store_true",
                       help="Analyze corrections and show error patterns")
    group.add_argument("--propose",   action="store_true",
                       help="Propose rule changes (programmatic refiner only)")
    group.add_argument("--apply",     action="store_true",
                       help="Full pipeline: analyze → LLM review → simulate → apply")
    group.add_argument("--benchmark", metavar="SUBCOMMAND",
                       help="Snapshot or compare accuracy: before | after | compare")

    args = parser.parse_args()

    if args.analyze:
        cmd_analyze()
    elif args.propose:
        cmd_propose()
    elif args.apply:
        cmd_apply()
    elif args.benchmark:
        cmd_benchmark(args.benchmark)


if __name__ == "__main__":
    main()
