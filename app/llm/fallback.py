"""Degraded-mode interpreter, used ONLY when every Groq model is unavailable.

The LLM is the primary interpreter; this keyword/regex parser exists so a provider
outage produces a controlled, best-effort answer instead of a 5xx. Its output goes
through the same guardrails and is flagged in the explanation.
"""
from __future__ import annotations

import re

_TIME = r"(noon|midnight|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)"
_RANGE = re.compile(_TIME + r"\s*(?:to|until|till|through|and|-|–)\s*" + _TIME, re.I)
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_FRACTIONS = {"half": 50, "quarter": 25, "a third": 100 / 3, "one-third": 100 / 3,
              "one third": 100 / 3, "one-fifth": 20, "one fifth": 20, "a fifth": 20,
              "three quarters": 75, "three-quarters": 75, "two-thirds": 200 / 3}


def _parse_time(tok: str) -> tuple[int, str | None]:
    t = tok.lower().replace(".", "").strip()
    if t == "noon":
        return 12, "pm"
    if t == "midnight":
        return 0, "am"
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    h, mer = int(m.group(1)), m.group(3)
    if mer == "pm" and h < 12:
        h += 12
    elif mer == "am" and h == 12:
        h = 0
    if mer is None and ":" in t:
        mer = "24h"
    return h, mer


def _window(note: str) -> list[dict]:
    text = note
    for w, n in _WORD_NUM.items():
        text = re.sub(rf"\b{w}\b", str(n), text, flags=re.I)
    m = _RANGE.search(text)
    if not m:
        return []
    (s, sm), (e, em) = _parse_time(m.group(1)), _parse_time(m.group(2))
    if sm is None and em in ("pm",) and s < 12:
        s = s + 12 if s + 12 <= e else s  # "2 to 4 PM" -> 14..16, "11 to 2 PM" -> 11..14
    if sm is None and em is None:  # bare numbers: small ones are afternoon/evening
        s, e = (s + 12 if s < 7 else s), (e + 12 if e <= 7 else e)
    if em is None and sm == "pm" and e < 12:
        e += 12
    if m.group(2).lower() == "midnight":
        e = 24
    return [{"start_hour": s % 24, "end_hour": e if e == 24 else e % 24}]


def _percent(note: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", note, re.I)
    if m:
        return float(m.group(1))
    low = note.lower()
    for w, v in sorted(_FRACTIONS.items(), key=lambda kv: -len(kv[0])):
        if w in low:
            return v
    return None


def _kwh(note: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", note, re.I)
    return float(m.group(1)) if m else None


def interpret_fallback(note: str) -> dict:
    low = note.lower()
    windows = _window(note)
    base = {"windows": windows, "value": 0, "value_unit": "none",
            "explanation": "[fallback parser: LLM unavailable] "}
    other_period = re.search(r"next (week|month|year)|last (week|month)", low)
    if not windows or other_period:
        return {**base, "directive_type": "no_op", "windows": [],
                "explanation": base["explanation"] + "No schedulable energy constraint found."}

    if re.search(r"solar|pv|panel|inverter|photovoltaic", low):
        pct = _percent(note)
        if pct is not None:
            unit = "percent_reduction" if re.search(r"reduc|cut|by \d|lower by|decrease", low) and not re.search(r"to (about |roughly )?\d", low) else "percent_remaining"
            return {**base, "directive_type": "solar_reduction", "value": pct, "value_unit": unit,
                    "explanation": base["explanation"] + "Solar output reduced."}
    if re.search(r"discharg", low):
        return {**base, "directive_type": "no_discharge_window",
                "explanation": base["explanation"] + "Battery discharge blocked."}
    if re.search(r"charg", low):
        return {**base, "directive_type": "no_charge_window",
                "explanation": base["explanation"] + "Battery charging unavailable."}
    if re.search(r"grid|import|intake|feeder|transformer|substation", low) and _kwh(note) is not None:
        return {**base, "directive_type": "max_grid_window", "value": _kwh(note), "value_unit": "kwh",
                "explanation": base["explanation"] + "Grid import capped."}
    if re.search(r"reserve|keep|retain|hold|at least|remain", low) and re.search(r"batter|stor", low):
        if _kwh(note) is not None:
            return {**base, "directive_type": "minimum_battery_reserve", "value": _kwh(note), "value_unit": "kwh",
                    "explanation": base["explanation"] + "Battery reserve required."}
        if _percent(note) is not None:
            return {**base, "directive_type": "minimum_battery_reserve", "value": _percent(note),
                    "value_unit": "percent_remaining",
                    "explanation": base["explanation"] + "Battery reserve required."}
    return {**base, "directive_type": "no_op", "windows": [],
            "explanation": base["explanation"] + "No schedulable energy constraint found."}
