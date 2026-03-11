"""
Condition evaluator for the GL rule engine.

eval_op(op, field_value, target) -> bool
matches(condition, signals_dict) -> bool

Condition language: ANDed field constraints.
  { "category_hint": { "eq": "software" }, "unit_cost": { "gte": 5000 } }

OR logic is not supported — write two separate rules instead.
"""
from __future__ import annotations


def eval_op(op: str, field_value, target) -> bool:
    """
    Evaluate a single operator against a field value.

    For numeric comparisons (gte, lte, gt, lt), returns False when
    field_value is None so rules fall through to fallback rules gracefully.

    Raises ValueError for unknown operators.
    """
    if op == "eq":      return field_value == target
    if op == "ne":      return field_value != target
    if op == "gte":     return field_value is not None and field_value >= target
    if op == "lte":     return field_value is not None and field_value <= target
    if op == "gt":      return field_value is not None and field_value > target
    if op == "lt":      return field_value is not None and field_value < target
    if op == "in":      return field_value in target
    if op == "not_in":  return field_value not in target
    if op == "is_null":  return (field_value is None) == target
    if op == "not_null": return (field_value is not None) == target
    raise ValueError(f"Unknown operator: {op!r}")


def matches(condition: dict, signals: dict) -> bool:
    """
    Return True iff all field constraints in condition match the signals dict.

    condition: { field_name: { operator: target_value }, ... }
    signals:   flat dict of signal field names → values (e.g. dataclasses.asdict(LineSignals))
    """
    for field, constraints in condition.items():
        value = signals.get(field)
        for op, target in constraints.items():
            if not eval_op(op, value, target):
                return False
    return True
