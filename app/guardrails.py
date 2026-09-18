"""Deterministic guardrails (Problem Statement §05.1, §08).

1. check_item(): structural check of one raw LLM item (untrusted). Raises GuardrailError.
2. to_directive(): converts a checked item into the exact response shape, doing all
   arithmetic (window -> hours, percent -> factor, % capacity -> kWh) in code.
3. validate_directive(): final check of the canonical entry before it reaches the optimizer.
"""
from __future__ import annotations

import math

from app.schemas import DIRECTIVE_TYPES

HOURLY = {"no_charge_window", "no_discharge_window"}
UNITS = {"kwh", "percent_remaining", "percent_reduction", "none"}
ADJ_KEYS = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}


class GuardrailError(ValueError):
    pass


def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise GuardrailError("value must be a finite number")
    return float(v)


def _hour(v, lo: int, hi: int) -> int:
    if isinstance(v, bool):
        raise GuardrailError("hour must be an integer")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int) or not lo <= v <= hi:
        raise GuardrailError(f"hour {v!r} outside {lo}..{hi}")
    return v


def expand_windows(windows) -> list[int]:
    """[start, end) on a 24h clock; end 24 = midnight; start > end wraps past midnight."""
    if not isinstance(windows, list):
        raise GuardrailError("windows must be a list")
    hours: set[int] = set()
    for w in windows:
        if not isinstance(w, dict):
            raise GuardrailError("window must be an object")
        s, e = _hour(w.get("start_hour"), 0, 23), _hour(w.get("end_hour"), 0, 24)
        if s == e:
            hours.add(s)  # degenerate "at 7 PM" window -> that single hour
        elif s < e:
            hours.update(range(s, e))
        else:
            hours.update(range(s, 24))
            hours.update(range(0, e))
    return sorted(hours)


def _ratio(value: float, unit: str) -> float:
    """Solar factor (usable fraction remaining) from a percentage."""
    if unit not in ("percent_remaining", "percent_reduction"):
        raise GuardrailError(f"solar_reduction needs a percentage, got {unit}")
    if value < 0 or value > 100:
        raise GuardrailError("percentage outside 0..100")
    frac = value if 0 < value < 1 else value / 100.0  # tolerate 0.2 meaning 20%
    return frac if unit == "percent_remaining" else 1.0 - frac


def check_item(item) -> None:
    """Structural validation of a raw LLM item; used to trigger model fallback."""
    if not isinstance(item, dict):
        raise GuardrailError("item must be an object")
    t = item.get("directive_type")
    if t not in DIRECTIVE_TYPES:
        raise GuardrailError(f"unsupported directive_type {t!r}")
    if t == "no_op":
        return
    if not expand_windows(item.get("windows")):
        raise GuardrailError("applicable directive without hours")
    unit = item.get("value_unit")
    if unit not in UNITS:
        raise GuardrailError(f"unsupported unit {unit!r}")
    value = _num(item.get("value", 0))
    if t == "solar_reduction":
        _ratio(value, unit)
    elif t == "minimum_battery_reserve":
        if unit not in ("kwh", "percent_remaining") or value < 0:
            raise GuardrailError("reserve needs non-negative kWh or % of capacity")
    elif t == "max_grid_window":
        if unit != "kwh" or value < 0:
            raise GuardrailError("grid cap needs non-negative kWh")


def no_op(note_index: int, explanation: str) -> dict:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation,
    }


def to_directive(note_index: int, item: dict, capacity_kwh: float) -> dict:
    check_item(item)
    t = item["directive_type"]
    explanation = str(item.get("explanation") or "").strip()[:300]
    if t == "no_op":
        return no_op(note_index, explanation or "This note does not affect the 24-hour energy schedule.")

    hours = expand_windows(item["windows"])
    value, unit = _num(item.get("value", 0)), item.get("value_unit")
    if t == "solar_reduction":
        adj = {"hours": hours, "factor": round(_ratio(value, unit), 4)}
    elif t == "minimum_battery_reserve":
        kwh = value if unit == "kwh" else capacity_kwh * (value if 0 < value < 1 else value / 100.0)
        adj = {"hours": hours, "minimum_energy_kwh": round(min(kwh, capacity_kwh), 4)}
    elif t == "max_grid_window":
        adj = {"hours": hours, "max_grid_kwh": round(value, 4)}
    else:
        adj = {"hours": hours}
    d = {
        "note_index": note_index,
        "applies": True,
        "directive_type": t,
        "structured_adjustment": adj,
        "explanation": explanation or f"Interpreted as {t}.",
    }
    validate_directive(d, capacity_kwh)
    return d


def validate_directive(d: dict, capacity_kwh: float) -> None:
    """Exact contract check for one canonical directive_interpretation entry."""
    t = d.get("directive_type")
    if t not in DIRECTIVE_TYPES:
        raise GuardrailError("unsupported directive_type")
    adj = d.get("structured_adjustment")
    if t == "no_op":
        if d.get("applies") is not False or adj is not None:
            raise GuardrailError("no_op requires applies=false and null adjustment")
        return
    if d.get("applies") is not True or not isinstance(adj, dict):
        raise GuardrailError("directive requires applies=true and an adjustment object")
    if set(adj) != ADJ_KEYS[t]:
        raise GuardrailError(f"{t} adjustment must have keys {sorted(ADJ_KEYS[t])}")
    hours = adj["hours"]
    if (
        not isinstance(hours, list)
        or not hours
        or any(isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23 for h in hours)
        or hours != sorted(set(hours))
    ):
        raise GuardrailError("hours must be unique ascending integers 0..23")
    if t == "solar_reduction" and not 0 <= _num(adj["factor"]) <= 1:
        raise GuardrailError("factor must be within 0..1")
    if t == "minimum_battery_reserve" and not 0 <= _num(adj["minimum_energy_kwh"]) <= capacity_kwh:
        raise GuardrailError("reserve must be within 0..capacity")
    if t == "max_grid_window" and _num(adj["max_grid_kwh"]) < 0:
        raise GuardrailError("max_grid_kwh must be non-negative")
