"""
RuleReviewAgent — Sonnet LLM validation of proposed rule changes.

Single-turn Sonnet call: given current rules, thresholds, error patterns,
reviewer corrections, and proposed changes → approve / reject / modify each change.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic

from evals.feedback.analyzer import ErrorPatternReport
from evals.feedback.collector import FeedbackRecord
from evals.feedback.refiner import RuleChange
from rules_engine.rules_tools import get_rules
from approval_routing.threshold_tools import get_thresholds


@dataclass
class ReviewVerdict:
    change_id:       str
    verdict:         str            # "APPROVE" | "REJECT" | "MODIFY"
    reasoning:       str
    modified_change: RuleChange | None   # set when verdict == "MODIFY"
    confidence:      float


class RuleReviewAgent:
    """
    Uses Claude Sonnet to review proposed rule changes in the context of
    the full rule set, error patterns, and reviewer corrections.
    """

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self._model  = model
        self._client = anthropic.Anthropic()

    def review(
        self,
        proposed_changes: list[RuleChange],
        error_report: ErrorPatternReport,
        current_rules: dict,
        current_thresholds: dict,
        raw_corrections: list[FeedbackRecord],
    ) -> list[ReviewVerdict]:
        """
        Single-turn Sonnet call that evaluates each proposed change.
        Returns one ReviewVerdict per proposed change.
        """
        if not proposed_changes:
            return []

        prompt = self._build_prompt(
            proposed_changes,
            error_report,
            current_rules,
            current_thresholds,
            raw_corrections,
        )

        message = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text
        verdicts = self._parse_response(response_text, proposed_changes)
        return verdicts

    # -----------------------------------------------------------------------
    # Prompt construction
    # -----------------------------------------------------------------------

    def _build_prompt(
        self,
        changes: list[RuleChange],
        report: ErrorPatternReport,
        rules: dict,
        thresholds: dict,
        corrections: list[FeedbackRecord],
    ) -> str:
        rules_json      = json.dumps(rules, indent=2, default=str)
        thresholds_json = json.dumps(thresholds, indent=2, default=str)

        # Error pattern summary
        pattern_lines = []
        for p in report.systematic_errors:
            pattern_lines.append(
                f"  - {p.field}: {p.proposed_value!r}→{p.corrected_value!r} "
                f"(freq={p.frequency}, invoices: {', '.join(p.affected_invoices)}"
                + (f", likely rule: {p.affected_rule_id}" if p.affected_rule_id else "")
                + ")"
            )
        pattern_summary = "\n".join(pattern_lines) or "  (none)"

        # Reviewer corrections
        correction_lines = []
        for r in corrections:
            line = (
                f"  - {r.invoice_id} | {r.field}: {r.proposed_value!r}→{r.corrected_value!r}"
            )
            if r.correction_reason:
                line += f"  reason: {r.correction_reason!r}"
            correction_lines.append(line)
        corrections_str = "\n".join(correction_lines) or "  (none)"

        # Proposed changes
        change_lines = []
        for c in changes:
            change_lines.append(
                f"  change_id: {c.change_id}\n"
                f"  change_type: {c.change_type}\n"
                f"  rule_system: {c.rule_system}\n"
                f"  rule_id: {c.rule_id}\n"
                f"  field: {c.field}\n"
                f"  old_value: {c.old_value!r}\n"
                f"  new_value: {c.new_value!r}\n"
                f"  api_call: {c.api_call}\n"
                f"  rationale: {c.rationale}\n"
                f"  frequency: {c.frequency}\n"
            )
        changes_str = "\n---\n".join(change_lines)

        return f"""You are a GL accounting rules auditor reviewing proposed changes to an AP automation rule engine.

CURRENT RULES (rules.json):
{rules_json}

CURRENT APPROVAL THRESHOLDS (thresholds.json):
{thresholds_json}

ERROR PATTERNS DETECTED (systematic errors appearing ≥2 times):
{pattern_summary}

REVIEWER CORRECTIONS (with reasons):
{corrections_str}

PROPOSED CHANGES:
{changes_str}

For each proposed change, evaluate:
1. Does this change correctly fix the observed error pattern?
2. Does it conflict with any other rule (check priorities and conditions)?
3. Is the correction evidence strong enough (freq >= 2, reviewer reasons make sense)?
4. Could this change break any currently-correct classification?

For each change respond EXACTLY in this format (one block per change):
change_id: <id>
verdict: APPROVE | REJECT | MODIFY
reasoning: <1-2 sentences explaining your decision>
modified_value: <the value you recommend instead, only if verdict is MODIFY, else omit>
confidence: <0.0-1.0>

Important notes:
- Be skeptical of approval threshold changes: they represent business policy, not classification errors
- Check for rule priority conflicts before approving GL changes
- MODIFY means you agree with the direction but suggest a different specific value"""

    # -----------------------------------------------------------------------
    # Response parsing
    # -----------------------------------------------------------------------

    def _parse_response(
        self,
        text: str,
        changes: list[RuleChange],
    ) -> list[ReviewVerdict]:
        """Parse the LLM response into ReviewVerdict objects."""
        # Build a map for quick lookup
        change_map = {c.change_id: c for c in changes}

        # Split into per-change blocks
        blocks: list[str] = re.split(r'\n(?=change_id:)', text.strip())

        verdicts: list[ReviewVerdict] = []
        found_ids: set[str] = set()

        for block in blocks:
            lines = block.strip().splitlines()
            parsed: dict[str, str] = {}
            for line in lines:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    parsed[k.strip()] = v.strip()

            change_id = parsed.get("change_id", "")
            if not change_id:
                continue

            # Match against known change IDs (prefix match)
            matched_id = None
            for cid in change_map:
                if cid.startswith(change_id) or change_id.startswith(cid):
                    matched_id = cid
                    break
            if matched_id is None:
                continue

            verdict_str    = parsed.get("verdict", "REJECT").upper()
            reasoning      = parsed.get("reasoning", "")
            modified_value = parsed.get("modified_value")
            try:
                confidence = float(parsed.get("confidence", "0.5"))
            except ValueError:
                confidence = 0.5

            # Build modified_change if MODIFY
            modified_change = None
            if verdict_str == "MODIFY" and modified_value and matched_id in change_map:
                orig = change_map[matched_id]
                modified_change = RuleChange(
                    change_id=orig.change_id,
                    rule_system=orig.rule_system,
                    change_type=orig.change_type,
                    rule_id=orig.rule_id,
                    field=orig.field,
                    old_value=orig.old_value,
                    new_value=modified_value,
                    rationale=orig.rationale + f" [LLM modified: {modified_value!r}]",
                    based_on=orig.based_on,
                    frequency=orig.frequency,
                    api_call=orig.api_call.replace(repr(orig.new_value), repr(modified_value)),
                )

            verdicts.append(ReviewVerdict(
                change_id=matched_id,
                verdict=verdict_str if verdict_str in ("APPROVE", "REJECT", "MODIFY") else "REJECT",
                reasoning=reasoning,
                modified_change=modified_change,
                confidence=confidence,
            ))
            found_ids.add(matched_id)

        # Any changes not mentioned by the LLM → REJECT (safe default)
        for change in changes:
            if change.change_id not in found_ids:
                verdicts.append(ReviewVerdict(
                    change_id=change.change_id,
                    verdict="REJECT",
                    reasoning="LLM did not respond for this change — defaulting to REJECT.",
                    modified_change=None,
                    confidence=0.0,
                ))

        return verdicts
