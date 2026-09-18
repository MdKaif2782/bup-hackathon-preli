"""API contract + robustness tests. The LLM is stubbed so these run offline."""
import copy

import pytest
from fastapi.testclient import TestClient

import app.pipeline as pipeline
from app.llm.client import LLMUnavailable
from app.main import app
from app.validator import replay
from tests.conftest import load_cases

client = TestClient(app, raise_server_exceptions=False)
CASES = load_cases()
BASE = CASES[5]["input"]  # 3 notes incl. a distractor


def _llm_from_expected(case):
    """Fake LLM returning the intermediate schema that matches the reference answer."""
    items = []
    for d in case["expected_output"]["directive_interpretation"]:
        adj = d["structured_adjustment"] or {}
        hours = adj.get("hours", [])
        item = {"directive_type": d["directive_type"], "windows": [{"start_hour": h, "end_hour": h + 1} for h in hours],
                "value": 0, "value_unit": "none", "explanation": "stub"}
        if "factor" in adj:
            item.update(value=adj["factor"] * 100, value_unit="percent_remaining")
        for k in ("minimum_energy_kwh", "max_grid_kwh"):
            if k in adj:
                item.update(value=adj[k], value_unit="kwh")
        items.append(item)
    return items


@pytest.fixture
def stub_llm(monkeypatch):
    def use(fn):
        monkeypatch.setattr(pipeline, "interpret_notes", fn)
    return use


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_full_contract_with_stubbed_llm(case, stub_llm):
    stub_llm(lambda notes: (_llm_from_expected(case), "stub"))
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200
    body = r.json()
    exp = case["expected_output"]
    assert set(body) == {"scenario_id", "directive_interpretation", "hourly_plan", "total_grid_kwh",
                         "total_cost_bdt", "peak_grid_kwh", "plan_summary"}
    assert body["scenario_id"] == case["input"]["scenario_id"]
    for got, want in zip(body["directive_interpretation"], exp["directive_interpretation"]):
        assert set(got) == {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}
        assert (got["note_index"], got["applies"], got["directive_type"]) == \
               (want["note_index"], want["applies"], want["directive_type"])
        assert got["structured_adjustment"] == pytest.approx(want["structured_adjustment"]) \
            if want["structured_adjustment"] else got["structured_adjustment"] is None
    assert replay(case["input"], exp["directive_interpretation"], body) == []
    assert body["total_cost_bdt"] == pytest.approx(exp["total_cost_bdt"], abs=0.01)


@pytest.mark.parametrize("raw", ["{bad", "[]", "null", '"text"', ""])
def test_malformed_json_is_400(raw):
    r = client.post("/optimize-energy", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "error" in r.json()


def _mut(fn):
    body = copy.deepcopy(BASE)
    fn(body)
    return body


@pytest.mark.parametrize("body", [
    _mut(lambda b: b.pop("battery")),
    _mut(lambda b: b.pop("scenario_id")),
    _mut(lambda b: b.update(operator_notes=[])),
    _mut(lambda b: b.update(operator_notes=["a", "b", "c", "d"])),
    _mut(lambda b: b.update(operator_notes=["  "])),
    _mut(lambda b: b.update(hours=b["hours"][:23])),
    _mut(lambda b: b["hours"][5].update(hour=4)),
    _mut(lambda b: b["hours"][0].update(demand_kwh=-1)),
    _mut(lambda b: b["hours"][0].update(demand_kwh="abc")),
    _mut(lambda b: b["battery"].update(capacity_kwh=None)),
], ids=["no-battery", "no-id", "0-notes", "4-notes", "blank-note", "23-hours", "dup-hour",
        "neg-demand", "str-demand", "null-capacity"])
def test_structurally_invalid_is_400(body):
    r = client.post("/optimize-energy", json=body)
    assert r.status_code == 400
    assert "Traceback" not in r.text


def test_llm_outage_uses_fallback_not_5xx(stub_llm):
    def down(notes):
        raise LLMUnavailable("RateLimitError")
    stub_llm(down)
    case = CASES[5]
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200
    body = r.json()
    assert all("fallback" in d["explanation"] for d in body["directive_interpretation"])
    assert replay(case["input"], case["expected_output"]["directive_interpretation"], body) == []


@pytest.mark.parametrize("junk", [
    {"directive_type": "turn_off_lights", "windows": [], "value": 0, "value_unit": "none"},
    {"directive_type": "solar_reduction", "windows": [{"start_hour": 30, "end_hour": 40}], "value": 50, "value_unit": "percent_remaining"},
    {"directive_type": "solar_reduction", "windows": [{"start_hour": 10, "end_hour": 12}], "value": 180, "value_unit": "percent_remaining"},
    {"directive_type": "max_grid_window", "windows": [{"start_hour": 10, "end_hour": 12}], "value": -5, "value_unit": "kwh"},
    {"directive_type": "no_charge_window", "windows": [], "value": 0, "value_unit": "none"},
    "not even an object",
])
def test_invalid_llm_output_becomes_safe_no_op(junk, stub_llm):
    stub_llm(lambda notes: ([junk] * len(notes), "stub"))
    r = client.post("/optimize-energy", json=BASE)
    assert r.status_code == 200
    for d in r.json()["directive_interpretation"]:
        assert d["directive_type"] == "no_op" and d["applies"] is False and d["structured_adjustment"] is None


def test_contradictory_directives_still_return_valid_json(stub_llm):
    # grid cap of 0 all day cannot be met: relaxed solve must still answer 200
    stub_llm(lambda notes: ([{"directive_type": "max_grid_window", "windows": [{"start_hour": 0, "end_hour": 24}],
                              "value": 0, "value_unit": "kwh", "explanation": "x"}] * len(notes), "stub"))
    r = client.post("/optimize-energy", json=BASE)
    assert r.status_code == 200
    assert "infeasible" in r.json()["plan_summary"]


def test_impossible_battery_is_422(stub_llm):
    stub_llm(lambda notes: ([{"directive_type": "no_op", "windows": [], "value": 0, "value_unit": "none",
                              "explanation": "x"}] * len(notes), "stub"))
    body = _mut(lambda b: b["battery"].update(initial_energy_kwh=10, minimum_energy_kwh=50))
    r = client.post("/optimize-energy", json=body)
    assert r.status_code == 422 and "error" in r.json()


def test_unexpected_llm_crash_degrades_to_fallback(stub_llm):
    def boom(notes):
        raise RuntimeError("unexpected")
    stub_llm(boom)
    r = client.post("/optimize-energy", json=BASE)
    assert r.status_code == 200


def test_unexpected_crash_is_controlled_500(stub_llm, monkeypatch):
    stub_llm(lambda notes: (_llm_from_expected(CASES[5]), "stub"))

    def boom(*a, **k):
        raise RuntimeError("secret-ish internal detail")
    monkeypatch.setattr(pipeline, "optimize", boom)
    r = client.post("/optimize-energy", json=BASE)
    assert r.status_code == 500
    assert r.json() == {"error": "internal error"}
