"""Exact 24-hour LP scheduler (Problem Statement §05, §09).

Variables per hour h: g (grid), s (solar used), c (charge), d (discharge), E (energy after hour).
    min  sum tariff[h]*g[h]  (+ tiny cycling penalty)
    s.t. g + s + d - c = demand                   energy balance
         E[h] - E[h-1] - c + d = 0, E[-1] = init  battery transition
         E[23] = init                             end-of-day neutrality
         bounds encode solar/rate/capacity/reserve and every operator directive.
If the directive set is infeasible, re-solve with heavily penalised slack so the
service still returns a physically consistent plan instead of an error.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

from app.schemas import OptimizeRequest

H = 24
NV = 5  # g, s, c, d, E
CYCLE_PENALTY = 1e-6
SLACK_PENALTY = 1e6
DECIMALS = 4


@dataclass
class Constraints:
    eff_solar: list[float]
    min_energy: list[float]
    max_charge: list[float]
    max_discharge: list[float]
    max_grid: list[float | None]
    notes: list[str] = field(default_factory=list)


def build_constraints(req: OptimizeRequest, directives: list[dict]) -> Constraints:
    b = req.battery
    eff_solar = [h.solar_kwh for h in req.hours]
    min_energy = [b.minimum_energy_kwh] * H
    max_charge = [b.max_charge_kwh_per_hour] * H
    max_discharge = [b.max_discharge_kwh_per_hour] * H
    max_grid: list[float | None] = [None] * H

    for d in directives:
        t, adj = d["directive_type"], d.get("structured_adjustment")
        if t == "no_op" or not adj:
            continue
        for h in adj["hours"]:
            if t == "solar_reduction":
                eff_solar[h] = eff_solar[h] * adj["factor"]
            elif t == "minimum_battery_reserve":
                min_energy[h] = max(min_energy[h], adj["minimum_energy_kwh"])
            elif t == "no_charge_window":
                max_charge[h] = 0.0
            elif t == "no_discharge_window":
                max_discharge[h] = 0.0
            elif t == "max_grid_window":
                cap = adj["max_grid_kwh"]
                max_grid[h] = cap if max_grid[h] is None else min(max_grid[h], cap)
    return Constraints(eff_solar, min_energy, max_charge, max_discharge, max_grid)


def _idx(h: int, k: int) -> int:
    return h * NV + k


def _solve(req: OptimizeRequest, cons: Constraints, with_slack: bool):
    b = req.battery
    n_base = H * NV
    # slack vars (only in relaxed mode): reserve shortfall r[h], grid-cap excess u[h]
    n = n_base + (2 * H if with_slack else 0)
    cost = np.zeros(n)
    bounds: list[tuple[float, float | None]] = []
    for h in range(H):
        hr = req.hours[h]
        cost[_idx(h, 0)] = hr.tariff_bdt_per_kwh
        cost[_idx(h, 2)] = CYCLE_PENALTY
        cost[_idx(h, 3)] = CYCLE_PENALTY
        gmax = cons.max_grid[h]
        emin = cons.min_energy[h]
        if with_slack:
            # hard bounds become soft; physical bounds (capacity, base min) stay hard
            emin_hard = min(b.minimum_energy_kwh, b.capacity_kwh)
            bounds += [
                (0, None),
                (0, cons.eff_solar[h]),
                (0, cons.max_charge[h]),
                (0, cons.max_discharge[h]),
                (emin_hard, b.capacity_kwh),
            ]
        else:
            bounds += [
                (0, gmax),
                (0, cons.eff_solar[h]),
                (0, cons.max_charge[h]),
                (0, cons.max_discharge[h]),
                (emin, b.capacity_kwh),
            ]
    if with_slack:
        for h in range(H):
            cost[n_base + h] = SLACK_PENALTY
            cost[n_base + H + h] = SLACK_PENALTY
        bounds += [(0, None)] * (2 * H)

    a_eq, b_eq = [], []
    for h in range(H):
        row = np.zeros(n)  # balance
        row[_idx(h, 0)] = 1
        row[_idx(h, 1)] = 1
        row[_idx(h, 3)] = 1
        row[_idx(h, 2)] = -1
        a_eq.append(row)
        b_eq.append(req.hours[h].demand_kwh)

        row = np.zeros(n)  # transition
        row[_idx(h, 4)] = 1
        row[_idx(h, 2)] = -1
        row[_idx(h, 3)] = 1
        rhs = 0.0
        if h == 0:
            rhs = b.initial_energy_kwh
        else:
            row[_idx(h - 1, 4)] = -1
        a_eq.append(row)
        b_eq.append(rhs)

    row = np.zeros(n)  # neutrality
    row[_idx(H - 1, 4)] = 1
    a_eq.append(row)
    b_eq.append(b.initial_energy_kwh)

    a_ub, b_ub = [], []
    if with_slack:
        for h in range(H):
            if cons.min_energy[h] > b.minimum_energy_kwh:
                row = np.zeros(n)  # -E - r <= -emin
                row[_idx(h, 4)] = -1
                row[n_base + h] = -1
                a_ub.append(row)
                b_ub.append(-cons.min_energy[h])
            if cons.max_grid[h] is not None:
                row = np.zeros(n)  # g - u <= cap
                row[_idx(h, 0)] = 1
                row[n_base + H + h] = -1
                a_ub.append(row)
                b_ub.append(cons.max_grid[h])

    res = linprog(
        cost,
        A_ub=np.array(a_ub) if a_ub else None,
        b_ub=np.array(b_ub) if b_ub else None,
        A_eq=np.array(a_eq),
        b_eq=np.array(b_eq),
        bounds=bounds,
        method="highs",
    )
    return res


class ScheduleInfeasible(Exception):
    pass


def optimize(req: OptimizeRequest, directives: list[dict]) -> tuple[list[dict], Constraints, bool]:
    """Returns (hourly_plan, constraints, relaxed). relaxed=True means directives had to be softened."""
    cons = build_constraints(req, directives)
    res = _solve(req, cons, with_slack=False)
    relaxed = False
    if res.status != 0:
        res = _solve(req, cons, with_slack=True)
        relaxed = True
        if res.status != 0:
            raise ScheduleInfeasible("scenario is infeasible under the base battery rules")
    x = res.x
    plan = _to_plan(req, cons, x)
    return plan, cons, relaxed


def _r(v: float) -> float:
    v = round(float(v), DECIMALS)
    return 0.0 if v == 0 else v  # no "-0.0"


def _to_plan(req: OptimizeRequest, cons: Constraints, x: np.ndarray) -> list[dict]:
    """Net charge/discharge into one action per hour, round, and re-derive state so that
    energy balance, transitions and neutrality hold exactly on the rounded numbers."""
    init = req.battery.initial_energy_kwh
    nets = [_r(x[_idx(h, 2)] - x[_idx(h, 3)]) for h in range(H)]
    # push accumulated rounding drift into the largest-magnitude move so sum(nets) == 0
    drift = _r(sum(nets))
    if drift != 0:
        k = max(range(H), key=lambda h: abs(nets[h]))
        nets[k] = _r(nets[k] - drift)

    plan = []
    energy = init
    for h in range(H):
        demand = req.hours[h].demand_kwh
        net = nets[h]
        solar = min(_r(x[_idx(h, 1)]), cons.eff_solar[h])
        solar = max(solar, 0.0)
        grid = demand + net - solar
        if grid < 0:  # rounding can make this -1e-4: use less solar instead
            solar = max(0.0, solar + grid)
            grid = 0.0
        energy = _r(energy + net)
        action = "charge" if net > 0 else "discharge" if net < 0 else "idle"
        plan.append(
            {
                "hour": h,
                "grid_kwh": _r(grid),
                "solar_used_kwh": _r(solar),
                "battery_action": action,
                "battery_kwh": _r(abs(net)),
                "battery_energy_after_kwh": energy,
            }
        )
    return plan


def totals(req: OptimizeRequest, plan: list[dict]) -> dict:
    grid = [p["grid_kwh"] for p in plan]
    cost = sum(g * req.hours[p["hour"]].tariff_bdt_per_kwh for g, p in zip(grid, plan))
    return {
        "total_grid_kwh": _r(sum(grid)),
        "total_cost_bdt": _r(cost),
        "peak_grid_kwh": _r(max(grid)),
    }
