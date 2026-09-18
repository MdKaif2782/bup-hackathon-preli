"""Judge-style replay of a returned plan (Problem Statement §09, §11.2, §11.3).

Used both inside the service (self-check before responding) and by tests/scripts.
Returns a list of human-readable violations; empty list == valid.
"""
from __future__ import annotations

import math

TOL = 0.01
ACTIONS = {"charge", "discharge", "idle"}


def replay(request: dict, directives: list[dict], response: dict) -> list[str]:
    errs: list[str] = []
    hours = sorted(request["hours"], key=lambda h: h["hour"])
    bat = request["battery"]
    plan = response.get("hourly_plan", [])

    if [p.get("hour") for p in plan] != list(range(24)):
        return ["hourly_plan must contain hours 0..23 exactly once, in order"]

    eff_solar = [h["solar_kwh"] for h in hours]
    min_e = [bat["minimum_energy_kwh"]] * 24
    no_charge, no_discharge, grid_cap = set(), set(), {}
    for d in directives:
        adj = d.get("structured_adjustment")
        if d["directive_type"] == "no_op" or not adj:
            continue
        for h in adj["hours"]:
            t = d["directive_type"]
            if t == "solar_reduction":
                eff_solar[h] *= adj["factor"]
            elif t == "minimum_battery_reserve":
                min_e[h] = max(min_e[h], adj["minimum_energy_kwh"])
            elif t == "no_charge_window":
                no_charge.add(h)
            elif t == "no_discharge_window":
                no_discharge.add(h)
            elif t == "max_grid_window":
                grid_cap[h] = min(grid_cap.get(h, math.inf), adj["max_grid_kwh"])

    e_prev = bat["initial_energy_kwh"]
    tot_grid = tot_cost = 0.0
    peak = 0.0
    for p, hr in zip(plan, hours):
        h = p["hour"]
        vals = [p.get(k) for k in ("grid_kwh", "solar_used_kwh", "battery_kwh", "battery_energy_after_kwh")]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in vals):
            errs.append(f"h{h}: non-finite or missing numeric value")
            continue
        g, s, b, e = vals
        a = p.get("battery_action")
        if a not in ACTIONS:
            errs.append(f"h{h}: invalid battery_action {a!r}")
            continue
        if min(g, s, b, e) < -TOL:
            errs.append(f"h{h}: negative value")
        if a == "idle" and abs(b) > TOL:
            errs.append(f"h{h}: idle with battery_kwh={b}")
        c = b if a == "charge" else 0.0
        dch = b if a == "discharge" else 0.0
        if c > bat["max_charge_kwh_per_hour"] + TOL:
            errs.append(f"h{h}: charge {c} > rate limit")
        if dch > bat["max_discharge_kwh_per_hour"] + TOL:
            errs.append(f"h{h}: discharge {dch} > rate limit")
        if abs(e - (e_prev + c - dch)) > TOL:
            errs.append(f"h{h}: battery transition mismatch ({e_prev} -> {e})")
        if e > bat["capacity_kwh"] + TOL:
            errs.append(f"h{h}: energy {e} > capacity")
        if e < min_e[h] - TOL:
            errs.append(f"h{h}: energy {e} < minimum {min_e[h]}")
        if s > eff_solar[h] + TOL:
            errs.append(f"h{h}: solar_used {s} > effective solar {eff_solar[h]}")
        if abs(g + s + dch - (hr["demand_kwh"] + c)) > TOL:
            errs.append(f"h{h}: energy balance violated")
        if h in no_charge and c > TOL:
            errs.append(f"h{h}: charging during no_charge_window")
        if h in no_discharge and dch > TOL:
            errs.append(f"h{h}: discharging during no_discharge_window")
        if h in grid_cap and g > grid_cap[h] + TOL:
            errs.append(f"h{h}: grid {g} > cap {grid_cap[h]}")
        e_prev = e
        tot_grid += g
        tot_cost += g * hr["tariff_bdt_per_kwh"]
        peak = max(peak, g)

    if abs(e_prev - bat["initial_energy_kwh"]) > TOL:
        errs.append(f"final energy {e_prev} != initial {bat['initial_energy_kwh']}")
    for key, val in (("total_grid_kwh", tot_grid), ("total_cost_bdt", tot_cost), ("peak_grid_kwh", peak)):
        if key in response and abs(response[key] - val) > TOL:
            errs.append(f"{key} {response[key]} != recalculated {round(val, 4)}")
    return errs
