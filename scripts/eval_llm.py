"""Measure LLM interpretation accuracy on tests/paraphrases.json.

Usage: python scripts/eval_llm.py [--model openai/gpt-oss-20b] [--batch 3] [--sleep 0]
Notes are sent in batches (like real 1-3 note scenarios) through the real
interpretation path: Groq -> guardrails -> canonical directive.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="force a single model (disables fallback chain)")
    ap.add_argument("--batch", type=int, default=3)
    ap.add_argument("--sleep", type=float, default=0.0, help="pause between batches (rate limits)")
    args = ap.parse_args()
    if args.model:
        os.environ["GROQ_MODEL"] = args.model
        os.environ["GROQ_FALLBACK_MODELS"] = ""

    from app.pipeline import interpret
    from app.schemas import OptimizeRequest

    data = json.loads((ROOT / "tests" / "paraphrases.json").read_text())
    cap = data["capacity_kwh"]
    notes = data["notes"]
    hours = [{"hour": h, "demand_kwh": 100, "solar_kwh": 50, "tariff_bdt_per_kwh": 10} for h in range(24)]
    battery = {"capacity_kwh": cap, "initial_energy_kwh": 100, "minimum_energy_kwh": 20,
               "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50}
    ok = 0
    sources = {}
    for i in range(0, len(notes), args.batch):
        chunk = notes[i:i + args.batch]
        req = OptimizeRequest.model_validate({"scenario_id": f"EVAL-{i}", "operator_notes": [n["note"] for n in chunk],
                                              "hours": hours, "battery": battery})
        directives, source = interpret(req)
        sources[source] = sources.get(source, 0) + 1
        for exp, got in zip(chunk, directives):
            adj = got["structured_adjustment"] or {}
            good = got["directive_type"] == exp["type"]
            if good and exp["type"] != "no_op":
                good = adj.get("hours") == exp["hours"]
                for k in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
                    if k in exp:
                        good = good and abs(adj.get(k, -1) - exp[k]) <= 0.01
            ok += good
            if not good:
                print(f"MISS [{source}] {exp['note']}\n     got  {got['directive_type']} {adj}\n     want {exp['type']} "
                      f"{ {k: v for k, v in exp.items() if k not in ('note', 'type')} }")
        if args.sleep:
            time.sleep(args.sleep)
    print(f"\n{ok}/{len(notes)} correct  ({100 * ok / len(notes):.1f}%)   sources={sources}")
    return 0 if ok == len(notes) else 1


if __name__ == "__main__":
    sys.exit(main())
