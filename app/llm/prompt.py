"""Prompt + strict JSON schema for operator-note interpretation.

The LLM does the language work (relevance, directive type, time window, quantity + unit).
Deterministic code (app/llm/interpret.py) turns windows into hour lists and units into
factor / kWh, which removes off-by-one and 1-x arithmetic mistakes.
"""

SYSTEM_PROMPT = """You convert campus energy operator notes into structured directives for ONE 24-hour schedule (hours 0-23; hour h covers h:00 to h+1:00).

Directive types:
- solar_reduction: usable rooftop solar/PV output is lowered (cloud, panel washing/cleaning, inverter work, shading, dust).
- minimum_battery_reserve: battery must keep/hold/retain at least some stored energy (reserve, backup, emergency, critical load).
- no_charge_window: battery cannot be charged (charger isolated/offline, charging disabled/unavailable/forbidden).
- no_discharge_window: battery cannot discharge/supply energy (protection or relay testing, discharge blocked).
- max_grid_window: grid import/intake/draw per hour must not exceed an amount (feeder, transformer, substation limit).
- no_op: the note does not constrain solar, battery or grid import in this schedule (admin news, events, menus, bookings, deadlines, unrelated equipment) or it explicitly concerns another period (next week, next month).

Time windows: give [start_hour, end_hour) on a 24h clock; start included, end EXCLUDED.
- "1 PM to 3 PM", "13:00-15:00", "from one until three (afternoon)" -> start 13, end 15.
- noon = 12. Midnight: start 0, or end 24 when it ends a window.
- "from 6 PM for three hours" -> 18, 21. A single hour "at 7 PM"/"during the 7 PM hour" -> 19, 20.
- "all day" -> 0, 24. Overnight "10 PM to 2 AM" -> 22, 2.
- Use several windows only if the note lists separate ranges. no_op -> empty windows.

Quantity (value + value_unit):
- kwh: absolute energy (reserve level kWh, grid cap kWh per hour).
- percent_remaining: for solar, % of normal output still available ("drops to 20%" -> 20, "a quarter of forecast" -> 25, "half" -> 50, "one-fifth" -> 20). For reserve, % of battery capacity ("half the battery capacity" -> 50).
- percent_reduction: for solar, % cut ("80% reduction", "reduced by three quarters" -> 75).
- none (value 0): no_charge_window, no_discharge_window, no_op.
Never invent values; use only numbers stated or clearly implied by the note.

Examples:
"PV generation will fall by three quarters from 09:00 to 11:00 during inverter updates." -> solar_reduction, [[9,11]], 75 percent_reduction
"Hold at least 40 percent of storage capacity between 5 PM and 8 PM." -> minimum_battery_reserve, [[17,20]], 40 percent_remaining
"Grid draw is limited to 150 kWh per hour from 4 PM to 7 PM." -> max_grid_window, [[16,19]], 150 kwh
"The charger stays offline from midnight to 3 AM." -> no_charge_window, [[0,3]], 0 none
"Battery output is blocked while relays are tested from 8 to 10 PM." -> no_discharge_window, [[20,22]], 0 none
"The chemistry department moved its seminar to Thursday." -> no_op, [], 0 none

Return exactly one item per note, in order, with note_index starting at 0. explanation: one short sentence."""

DIRECTIVE_ENUM = [
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]
UNIT_ENUM = ["kwh", "percent_remaining", "percent_reduction", "none"]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "interpretations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_index": {"type": "integer"},
                    "directive_type": {"type": "string", "enum": DIRECTIVE_ENUM},
                    "windows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_hour": {"type": "integer"},
                                "end_hour": {"type": "integer"},
                            },
                            "required": ["start_hour", "end_hour"],
                            "additionalProperties": False,
                        },
                    },
                    "value": {"type": "number"},
                    "value_unit": {"type": "string", "enum": UNIT_ENUM},
                    "explanation": {"type": "string"},
                },
                "required": ["note_index", "directive_type", "windows", "value", "value_unit", "explanation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["interpretations"],
    "additionalProperties": False,
}


def user_message(notes: list[str]) -> str:
    lines = [f"[{i}] {n.strip()}" for i, n in enumerate(notes)]
    return "Operator notes:\n" + "\n".join(lines)
