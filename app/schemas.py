"""Request/response models for the GridWise API (Problem Statement §07 and §10)."""
from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)
DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]
BatteryAction = Literal["charge", "discharge", "idle"]


def _finite_non_negative(v: float, name: str) -> float:
    if isinstance(v, bool) or not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    if v < 0:
        raise ValueError(f"{name} must be non-negative")
    return float(v)


class HourEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: StrictInt
    demand_kwh: float
    solar_kwh: float
    tariff_bdt_per_kwh: float

    @field_validator("hour")
    @classmethod
    def _hour_range(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("hour must be an integer from 0 to 23")
        return v

    @field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")
    @classmethod
    def _non_negative(cls, v: float, info) -> float:
        return _finite_non_negative(v, info.field_name)


class Battery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float

    @field_validator("*")
    @classmethod
    def _non_negative(cls, v: float, info) -> float:
        return _finite_non_negative(v, info.field_name)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourEntry] = Field(min_length=24, max_length=24)
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, v: list[str]) -> list[str]:
        if any(not n.strip() for n in v):
            raise ValueError("operator_notes must be non-empty strings")
        return v

    @model_validator(mode="after")
    def _hours_unique(self) -> "OptimizeRequest":
        if sorted(h.hour for h in self.hours) != list(range(24)):
            raise ValueError("hours must contain each hour 0..23 exactly once")
        self.hours = sorted(self.hours, key=lambda h: h.hour)
        return self


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[dict[str, Any]]
    explanation: str


class HourPlan(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: BatteryAction
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
