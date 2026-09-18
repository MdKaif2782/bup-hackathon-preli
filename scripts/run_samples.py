"""POST every public sample to a running service, replay the plan and score it.

Usage: python scripts/run_samples.py [--base-url http://localhost:8000] [--file cases.json]
Exit code 0 only if every case passes interpretation, validity and optimal cost.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.validator import replay  # noqa: E402

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


def post(url: str, body: dict, timeout: float = 30) -> tuple[dict, float]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), time.monotonic() - t0


def same_directive(a: dict, b: dict) -> bool:
    if (a["applies"], a["directive_type"]) != (b["applies"], b["directive_type"]):
        return False
    x, y = a["structured_adjustment"], b["structured_adjustment"]
    if x is None or y is None:
        return x is y
    if set(x) != set(y) or x["hours"] != y["hours"]:
        return False
    return all(abs(x[k] - y[k]) <= 0.01 for k in x if k != "hours")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    args = ap.parse_args()
    cases = json.loads(Path(args.file).read_text())["cases"]
    passed, latencies = 0, []
    for c in cases:
        exp = c["expected_output"]
        try:
            resp, dt = post(args.base_url.rstrip("/") + "/optimize-energy", c["input"])
        except Exception as exc:
            print(f"FAIL {c['id']}: request error {exc}")
            continue
        latencies.append(dt)
        problems = []
        got = resp.get("directive_interpretation", [])
        want = exp["directive_interpretation"]
        if len(got) != len(want) or [d["note_index"] for d in got] != list(range(len(want))):
            problems.append("interpretation count/order mismatch")
        else:
            for g, w in zip(got, want):
                if not same_directive(g, w):
                    problems.append(f"note {w['note_index']}: got {g['directive_type']} {g['structured_adjustment']} want {w['directive_type']} {w['structured_adjustment']}")
        # validity is judged against the ground-truth directives, as the organizer judge does
        problems += replay(c["input"], want, resp)
        if resp.get("scenario_id") != c["input"]["scenario_id"]:
            problems.append("scenario_id not echoed")
        cost, best = resp.get("total_cost_bdt"), exp["total_cost_bdt"]
        if not problems and cost is not None and cost > best + 0.01:
            problems.append(f"suboptimal cost {cost} > {best}")
        status = "PASS" if not problems else "FAIL"
        passed += not problems
        print(f"{status} {c['id']}  cost={cost} ref={best}  {dt:.2f}s")
        for p in problems[:5]:
            print("     -", p)
    if latencies:
        s = sorted(latencies)
        p95 = s[min(len(s) - 1, int(round(0.95 * len(s))) - 1)]
        print(f"\n{passed}/{len(cases)} passed   p95 latency {p95:.2f}s   max {s[-1]:.2f}s")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
