import pytest

from app.guardrails import GuardrailError, expand_windows, to_directive, validate_directive


def item(t, windows, value=0, unit="none"):
    return {"directive_type": t, "windows": [{"start_hour": s, "end_hour": e} for s, e in windows],
            "value": value, "value_unit": unit, "explanation": "x"}


@pytest.mark.parametrize("windows,hours", [
    ([(13, 15)], [13, 14]),
    ([(22, 2)], [0, 1, 22, 23]),
    ([(20, 24)], [20, 21, 22, 23]),
    ([(0, 24)], list(range(24))),
    ([(19, 19)], [19]),
    ([(9, 11), (10, 12)], [9, 10, 11]),
])
def test_window_expansion(windows, hours):
    assert expand_windows([{"start_hour": s, "end_hour": e} for s, e in windows]) == hours


@pytest.mark.parametrize("value,unit,factor", [
    (20, "percent_remaining", 0.2), (80, "percent_reduction", 0.2),
    (25, "percent_remaining", 0.25), (0.5, "percent_remaining", 0.5), (0, "percent_remaining", 0.0),
])
def test_solar_factor(value, unit, factor):
    d = to_directive(0, item("solar_reduction", [(13, 15)], value, unit), 200)
    assert d["structured_adjustment"] == {"hours": [13, 14], "factor": factor}
    assert d["applies"] is True


def test_reserve_percent_of_capacity():
    d = to_directive(1, item("minimum_battery_reserve", [(18, 21)], 50, "percent_remaining"), 200)
    assert d["structured_adjustment"] == {"hours": [18, 19, 20], "minimum_energy_kwh": 100}
    assert d["note_index"] == 1


def test_reserve_clamped_to_capacity():
    d = to_directive(0, item("minimum_battery_reserve", [(18, 21)], 999, "kwh"), 200)
    assert d["structured_adjustment"]["minimum_energy_kwh"] == 200


def test_no_op_shape():
    d = to_directive(2, item("no_op", []), 200)
    assert d == {"note_index": 2, "applies": False, "directive_type": "no_op",
                 "structured_adjustment": None, "explanation": "x"}


def test_hour_only_directives_have_exact_keys():
    for t in ("no_charge_window", "no_discharge_window"):
        assert to_directive(0, item(t, [(14, 16)]), 200)["structured_adjustment"] == {"hours": [14, 15]}


@pytest.mark.parametrize("bad", [
    item("bogus", [(1, 2)]),
    item("solar_reduction", [(1, 2)], 150, "percent_remaining"),
    item("solar_reduction", [(1, 2)], 20, "kwh"),
    item("max_grid_window", [(1, 2)], 100, "percent_remaining"),
    item("max_grid_window", [(1, 2)], float("nan"), "kwh"),
    item("no_charge_window", []),
    item("no_charge_window", [(25, 26)]),
])
def test_rejects_bad_items(bad):
    with pytest.raises(GuardrailError):
        to_directive(0, bad, 200)


@pytest.mark.parametrize("d", [
    {"applies": True, "directive_type": "no_op", "structured_adjustment": None},
    {"applies": False, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [1]}},
    {"applies": True, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [2, 1]}},
    {"applies": True, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [1, 1]}},
    {"applies": True, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [1], "x": 1}},
    {"applies": True, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [1], "factor": 1.5}},
])
def test_validate_directive_rejects(d):
    with pytest.raises(GuardrailError):
        validate_directive(d, 200)
