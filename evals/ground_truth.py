"""
Hardcoded ground truth for all 6 labeled invoices.

Covers three eval dimensions:
  - Stage 3 (GL Classification): gl_account, treatment, base_expense_account
  - Stage 4 (Prepaid/Accrual):   final_gl_account, final_treatment, amortization, accrual
  - Stage 5 (Approval Routing):  outcome

INV-004 note: stage3 GL is EXPENSE (5040/5060); stage4 upgrades to ACCRUAL (2110/2100).
              The JSON ground truth only has the final stage4 values — stage3 intermediates
              are hardcoded here.

INV-006 note: no PO → po_match returns NO_PO. The pipeline does not halt (ingestion only
              flags it); this eval asserts po_match.status == NO_PO as the "blocked" check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class LineGT:
    line_number: int

    # Dimension 1 — GL Classification (Stage 3 output)
    stage3_gl: str | None
    stage3_treatment: str | None
    stage3_base: str | None             # base_expense_account; None is a valid expected value

    # Dimension 2 — Final Treatment (Stage 4 output)
    final_gl: str | None
    final_treatment: str | None

    # PREPAID-specific (only set when final_treatment == PREPAID)
    amortization_months: int | None = None
    monthly_amount: Decimal | None = None   # first amortization entry amount

    # ACCRUAL-specific (only set when final_treatment == ACCRUAL)
    accrual_account: str | None = None      # = final_gl
    expense_account: str | None = None      # base expense reversed on payment


@dataclass
class InvoiceGT:
    invoice_id: str
    expected_approval: str | None   # VP_FINANCE / DEPT_MANAGER / AUTO_APPROVE
                                    # None means blocked — routing not reached
    expected_blocked: bool          # True for INV-006 (no PO)
    block_reason: str | None        # "NO_PO"
    lines: list[LineGT] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Full ground truth — all 6 invoices
# ---------------------------------------------------------------------------

GROUND_TRUTH: dict[str, InvoiceGT] = {

    # ── INV-001: Cloudware Solutions ─────────────────────────────────────────
    # Annual software license → Prepaid Software 1310, amortize 12×$2K to 5010
    # Approval: VP_FINANCE (Rule 6: $24K > $10K)
    "INV-001": InvoiceGT(
        invoice_id="INV-001",
        expected_approval="VP_FINANCE",
        expected_blocked=False,
        block_reason=None,
        lines=[
            LineGT(
                line_number=1,
                stage3_gl="1310",       stage3_treatment="PREPAID",  stage3_base="5010",
                final_gl="1310",        final_treatment="PREPAID",
                amortization_months=12, monthly_amount=Decimal("2000.00"),
            ),
        ],
    ),

    # ── INV-002: Morrison & Burke LLP ────────────────────────────────────────
    # 3-line legal invoice; Line 2 "advisory" overrides legal framing → 5040
    # Approval: DEPT_MANAGER (Rule 5: $9.5K in $1K–$10K band)
    "INV-002": InvoiceGT(
        invoice_id="INV-002",
        expected_approval="DEPT_MANAGER",
        expected_blocked=False,
        block_reason=None,
        lines=[
            LineGT(line_number=1,
                   stage3_gl="5030", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="5030",  final_treatment="EXPENSE"),
            LineGT(line_number=2,
                   stage3_gl="5040", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="5040",  final_treatment="EXPENSE"),
            LineGT(line_number=3,
                   stage3_gl="5030", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="5030",  final_treatment="EXPENSE"),
        ],
    ),

    # ── INV-003: TechDirect Inc. ─────────────────────────────────────────────
    # Mixed: equipment <$5K (5110 expense), ≥$5K (1500 capitalize),
    #        AWS annual cloud (1300 prepaid → 5020, 12×$3K)
    # Approval: VP_FINANCE (Rule 1: has CAPITALIZE line)
    "INV-003": InvoiceGT(
        invoice_id="INV-003",
        expected_approval="VP_FINANCE",
        expected_blocked=False,
        block_reason=None,
        lines=[
            LineGT(line_number=1,
                   stage3_gl="5110", stage3_treatment="EXPENSE",    stage3_base=None,
                   final_gl="5110",  final_treatment="EXPENSE"),
            LineGT(line_number=2,
                   stage3_gl="1500", stage3_treatment="CAPITALIZE", stage3_base=None,
                   final_gl="1500",  final_treatment="CAPITALIZE"),
            LineGT(line_number=3,
                   stage3_gl="1300",       stage3_treatment="PREPAID", stage3_base="5020",
                   final_gl="1300",        final_treatment="PREPAID",
                   amortization_months=12, monthly_amount=Decimal("3000.00")),
        ],
    ),

    # ── INV-004: Apex Strategy Group ─────────────────────────────────────────
    # Service delivered Dec 2025, invoiced Jan 2026 → service_precedes_invoice = True
    # Stage 3 assigns EXPENSE (5040 consulting, 5060 travel)
    # Stage 4 upgrades both to ACCRUAL (2110, 2100)
    # Approval: DEPT_MANAGER (Rule 5: $8.7K in $1K–$10K band)
    "INV-004": InvoiceGT(
        invoice_id="INV-004",
        expected_approval="DEPT_MANAGER",
        expected_blocked=False,
        block_reason=None,
        lines=[
            LineGT(line_number=1,
                   stage3_gl="5040", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="2110",  final_treatment="ACCRUAL",
                   accrual_account="2110", expense_account="5040"),
            LineGT(line_number=2,
                   stage3_gl="5060", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="2100",  final_treatment="ACCRUAL",
                   accrual_account="2100", expense_account="5060"),
        ],
    ),

    # ── INV-005: BrightSpark Agency ──────────────────────────────────────────
    # Marketing invoice; physical_goods rule overrides dept for Lines 2 & 4
    # Line 1: 5050 marketing  Line 2: 5000 physical_goods (branded merch)
    # Line 3: 5050 marketing  Line 4: 5000 physical_goods (branded gift bags)
    # Approval: VP_FINANCE (Rule 6: $23.5K > $10K)
    "INV-005": InvoiceGT(
        invoice_id="INV-005",
        expected_approval="VP_FINANCE",
        expected_blocked=False,
        block_reason=None,
        lines=[
            LineGT(line_number=1,
                   stage3_gl="5050", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="5050",  final_treatment="EXPENSE"),
            LineGT(line_number=2,
                   stage3_gl="5000", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="5000",  final_treatment="EXPENSE"),
            LineGT(line_number=3,
                   stage3_gl="5050", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="5050",  final_treatment="EXPENSE"),
            LineGT(line_number=4,
                   stage3_gl="5000", stage3_treatment="EXPENSE", stage3_base=None,
                   final_gl="5000",  final_treatment="EXPENSE"),
        ],
    ),

    # ── INV-006: QuickPrint Co. ──────────────────────────────────────────────
    # No PO number → po_match returns NO_PO.
    # Ingestion flags it as FLAGGED_NO_PO but does not halt.
    # The eval asserts po_match.status == NO_PO as the "blocked correctly" check.
    "INV-006": InvoiceGT(
        invoice_id="INV-006",
        expected_approval=None,     # routing not expected to be asserted
        expected_blocked=True,
        block_reason="NO_PO",
        lines=[],                   # no line-level assertions for blocked invoice
    ),
}


def get_ground_truth() -> dict[str, InvoiceGT]:
    return GROUND_TRUTH
