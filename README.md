# GridWise LLM: Smart Campus Energy Optimizer

BUP CSE Fest 2026 Hackathon, Online Preliminary. This is one HTTP service. It reads natural-language **operator notes** with an LLM, checks the output with **deterministic guardrails**, and produces the **cost-optimal 24-hour grid / solar / battery schedule** with an exact linear-programming solver.

| | |
|---|---|
| Live endpoint | **`https://bup-preli-la-team.inovate.it.com`** (plain `http://` works too, with no redirect) |
| Endpoints | `GET /health` · `POST /optimize-energy` |
| Docker fallback image | **`damegami2782/gridwise:v1.1.0`** (linux/amd64 + linux/arm64) |
| Service port | `8000` (binds `0.0.0.0`) |
| LLM providers | Groq and Cerebras. Chain: `groq:openai/gpt-oss-120b` → `cerebras:qwen-3.8-27b` → `cerebras:gpt-oss-120b` → `groq:openai/gpt-oss-20b` → `groq:qwen/qwen3.8-27b` |
| Optimizer | Linear program solved with HiGHS via `scipy.optimize.linprog` |

---

## 1. Quickstart: Docker (fastest)

```bash
docker pull damegami2782/gridwise:v1.1.0

docker run -d --name gridwise -p 8000:8000 \
  -e GROQ_API_KEY=<your-groq-api-key> \
  -e GROQ_MODEL=openai/gpt-oss-120b \
  -e GROQ_FALLBACK_MODELS=openai/gpt-oss-20b,qwen/qwen3.8-27b \
  -e CEREBRAS_API_KEY=<optional-cerebras-api-key> \
  damegami2782/gridwise:v1.1.0

curl http://localhost:8000/health
# {"status":"ok"}   (ready in ~2 s after start)
```

The image contains **no secrets**. API keys are passed only at runtime (`-e` or `--env-file .env`).

## 2. Quickstart: from source

Requires Python ≥ 3.11 and a Groq API key (free at <https://console.groq.com/keys>). A Cerebras key (free at <https://cloud.cerebras.ai>) is optional and adds high-rate-limit fallback models.

```bash
git clone https://github.com/MdKaif2782/bup-hackathon-preli.git
cd bup-hackathon-preli

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # runtime deps + pytest/httpx for tests

cp .env.example .env                 # then put your key in GROQ_API_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`.env` is loaded automatically at startup. With Docker Compose, `docker compose up --build` reads the same `.env`.

### Environment variables

| Name | Required | Default | Purpose |
|---|---|---|---|
| `GROQ_API_KEY` | **yes** | none | Groq API key (secret; never commit it) |
| `GROQ_MODEL` | no | `openai/gpt-oss-120b` | Primary interpretation model |
| `GROQ_FALLBACK_MODELS` | no | *(empty)* | Comma-separated Groq models tried last. We use `openai/gpt-oss-20b,qwen/qwen3.8-27b` |
| `CEREBRAS_API_KEY` | recommended | none | Enables the Cerebras models (secret) |
| `CEREBRAS_MODELS` | no | `qwen-3.8-27b,gpt-oss-120b` | Cerebras models, tried right after `GROQ_MODEL` |
| `LLM_CHAIN` | no | *(built from the above)* | Explicit override, e.g. `cerebras:qwen-3.8-27b,groq:openai/gpt-oss-120b` |
| `LLM_TIMEOUT_S` | no | `8` | Timeout per model call |
| `LLM_TOTAL_BUDGET_S` | no | `18` | Total LLM time budget per request (judge timeout is 30 s) |
| `LLM_MAX_OUTPUT_TOKENS` | no | `600` | Output token cap per call |
| `PORT` | no | `8000` | Container listen port |
| `WEB_CONCURRENCY` | no | `1` | Uvicorn workers (1 keeps the note cache shared) |

Providers without a key are skipped. With no key at all the service still starts and answers. Note interpretation then falls back to the degraded rule parser (see §5.4). Always configure the key for real evaluation.

---

## 3. Try it

```bash
BASE=http://localhost:8000     # or https://bup-preli-la-team.inovate.it.com

curl -s $BASE/health

# Run public sample SAMPLE-06 (3 notes, one distractor)
python3 -c "import json;print(json.dumps(json.load(open('BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json'))['cases'][5]['input']))" > /tmp/sample06.json
curl -s -X POST $BASE/optimize-energy -H 'Content-Type: application/json' -d @/tmp/sample06.json
```

Abbreviated response:

```json
{
  "scenario_id": "SAMPLE-06",
  "directive_interpretation": [
    {"note_index": 0, "applies": true,  "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [10, 11], "factor": 0.5}, "explanation": "..."},
    {"note_index": 1, "applies": true,  "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]}, "explanation": "..."},
    {"note_index": 2, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null, "explanation": "..."}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 105.0, "solar_used_kwh": 0.0, "battery_action": "charge",
     "battery_kwh": 20.0, "battery_energy_after_kwh": 120.0},
    "... 23 more hours ..."
  ],
  "total_grid_kwh": 2395.0,
  "total_cost_bdt": 34090.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Applied 2 operator directive(s) (solar_reduction, no_charge_window); 1 note(s) ignored as no_op. ..."
}
```

Individual hourly actions may differ between runs; equivalent optimal schedules have the same cost.

Error behaviour: malformed JSON or a structurally invalid body returns **400** `{"error": ..., "details": [...]}`. A scenario that is impossible under the base battery rules (e.g. initial energy below the minimum) returns **422**. Unexpected failures return **500** `{"error": "internal error"}`, with no stack traces or secrets.

---

## 4. Testing

### 4.1 Public samples, end to end (expected: 10/10)
With the service running:
```bash
python scripts/run_samples.py --base-url http://localhost:8000
```
For each public case the script checks that:
1. every directive interpretation matches the reference (type, hours, values ±0.01);
2. the returned plan passes a judge-style **replay against the reference directives**: energy balance, effective solar, battery transitions, bounds, rate limits, directive windows, end-of-day neutrality and the reported totals;
3. `total_cost_bdt` equals the reference optimum (±0.01).

Expected output:
```
PASS SAMPLE-01  cost=38365.0 ref=38365  1.19s
...
PASS SAMPLE-10  cost=41620.0 ref=41620  0.83s

10/10 passed   p95 latency 1.91s   max 1.91s
```

### 4.2 Unit and API tests (offline, no API key needed)
```bash
pytest -q          # 76 passed
```
The tests cover:
- the optimizer reaching the reference optimum on all 10 samples;
- the validator accepting the reference plans;
- guardrail conversion and rejection rules;
- the full response schema;
- 400 cases;
- LLM outage (fallback), invalid LLM output (safe `no_op`), contradictory directives, 422 and controlled 500.

### 4.3 Paraphrase robustness of the LLM (needs `GROQ_API_KEY`)
```bash
python scripts/eval_llm.py                            # full fallback chain
python scripts/eval_llm.py --model groq:openai/gpt-oss-20b --sleep 13   # one model at a time
python scripts/eval_llm.py --model cerebras:qwen-3.8-27b
```
This runs 31 self-written paraphrased notes (`tests/paraphrases.json`). They cover 12-hour/24-hour times, noon/midnight, overnight windows, single hours, "halve / one-fifth / three quarters", "% reduction" vs "% remaining", % of battery capacity, and distractors that mention times. Result: **31/31** for each of the five models on their own. A burst of 30 back-to-back uncached requests to `cerebras:qwen-3.8-27b` had 0 failures (p95 1.1 s).

---

## 5. Architecture

```
 request ──► schema validation (pydantic, 400 on failure)
               │
               ▼
        ┌──────────────┐   strict JSON schema    ┌───────────────────────┐
        │ LLM          │ ──────────────────────► │ Guardrails            │
        │ interpreter  │  type, time windows,    │ (deterministic code)  │
        │(Groq/Cerebras)│ value + unit per note  │ windows → hours       │
        └──────────────┘                         │ % → factor / kWh      │
          ▲ fallback chain + cache               │ range and shape checks│
          │ rule parser if all LLMs fail         └──────────┬────────────┘
                                                            ▼
                                             ┌────────────────────────────┐
                                             │ LP optimizer (HiGHS)       │
                                             │ min Σ tariff·grid          │
                                             │ + every directive as a     │
                                             │   hard constraint          │
                                             └──────────┬─────────────────┘
                                                        ▼
                                  replay validator (self-check) ──► JSON response
```

### 5.1 LLM role
The LLM is **the** interpreter of `operator_notes`, and its output directly defines the optimizer's constraints.
- All notes in a request are sent in one call with `temperature=0` and **strict `json_schema` structured output** (Groq and Cerebras) (`app/llm/prompt.py`).
- For each note the model returns:
  - `directive_type`, one of the 6 allowed types;
  - time `windows` as `[start_hour, end_hour)`;
  - a quantity with its unit: `kwh`, `percent_remaining`, `percent_reduction` or `none`;
  - a short explanation.
- The LLM handles the language: relevance, paraphrases, "noon until 2 PM", "one-fifth of normal output", "% of capacity". Code does the arithmetic, which removes off-by-one hour lists and 1−x mistakes.

### 5.2 Guardrails (`app/guardrails.py`)
All LLM output is treated as untrusted until it passes these checks:
- **Type:** `directive_type` must be one of the 6 supported types. Anything else is rejected; the service never invents a type.
- **Hours:** windows expand to unique ascending integers 0–23. The end hour is excluded, `24` means midnight, and a window that crosses midnight wraps.
- **Solar factor:** `factor = remaining %` or `1 − reduction %`, and must be in [0, 1].
- **Reserve:** `% of capacity × capacity_kwh`, finite, ≥ 0 and ≤ capacity.
- **Grid cap:** `max_grid_kwh` must be finite and ≥ 0.
- **Shape:** exactly one entry per note, in `note_index` order. `no_op` gets `applies=false` and `structured_adjustment=null`; every other type gets `applies=true` with exactly the required keys.
- **When a check fails:** the next model in the chain is asked. If output is still invalid, that note becomes a `no_op` with an explanation, so no constraint is ever invented.
- **Base data:** demand, tariff and battery parameters from the request are never modified.

### 5.3 Optimizer (`app/optimizer.py`)
A 24-hour linear program with 5 variables per hour: grid, solar used, charge, discharge and energy after the hour.
- **Objective:** minimise `Σ tariff[h]·grid[h]`, plus a 1e-6 penalty on battery throughput to avoid pointless cycling.
- **Constraints:**
  - energy balance every hour;
  - battery transitions;
  - `min(reserve) ≤ E ≤ capacity`;
  - charge and discharge rate limits;
  - `solar_used ≤ effective solar`;
  - end-of-day neutrality.
- **Directive effects:**
  - `solar_reduction` scales effective solar;
  - `minimum_battery_reserve` raises the lower bound on energy;
  - `no_charge_window` and `no_discharge_window` set the rate to 0;
  - `max_grid_window` caps grid import.
- **Post-processing:** charge and discharge are netted into a single action per hour (lossless, so equivalent). Values are rounded to 4 decimals, then battery state and grid import are **recomputed from the rounded values** so the checks hold exactly. Totals are calculated from the final plan.
- **Infeasible directives** (only possible if a note was misread): the LP is re-solved with heavily penalised slack, so the response is still a valid, physically consistent schedule, and `plan_summary` flags it.
- **Speed:** about 3 ms per solve. The result is the true optimum, and matches the reference cost on all public samples.

### 5.4 Reliability
- **Multi-provider model chain.** Every Groq and Cerebras model has its own rate-limit bucket. On a 429 the model is skipped for its `retry-after` period and the next model is used immediately. Cerebras `qwen-3.8-27b` (450 requests/min) is the high-throughput backstop; it runs with reasoning disabled, which keeps it under the output-token cap.
- **Timeouts:** 8 s per call and 18 s per request, inside the judge's 30 s.
- **LRU cache** of interpretations per note text, so repeated notes are answered instantly.
- **Degraded rule parser** (`app/llm/fallback.py`). It is used **only** if every LLM call fails, is flagged in `explanation`, and passes through the same guardrails.
- **Self-check.** Every response is replayed by `app/validator.py` before it is returned.
- **Logs** record the model, latency and error class names only, never keys, prompts or stack traces.

### 5.5 Repository layout
```
app/main.py            FastAPI routes + 400/422/500 handlers
app/schemas.py         request/response models
app/llm/prompt.py      system prompt + strict JSON schema
app/llm/client.py      Groq + Cerebras clients, model chain, cooldown, cache
app/llm/fallback.py    degraded rule parser (LLM outage only)
app/guardrails.py      deterministic validation + conversion
app/optimizer.py       LP model, relaxation, rounding, totals
app/validator.py       judge-style replay of a plan
app/pipeline.py        LLM → guardrails → optimizer → self-check
scripts/run_samples.py public-sample end-to-end checker
scripts/eval_llm.py    paraphrase accuracy evaluation
scripts/deploy_vps.sh  release to Docker Hub + redeploy on the team VPS
tests/                 pytest suite + paraphrases.json
```

---

## 6. Deployment
- **Live service:** Ubuntu VPS → Docker container (`docker-compose.yml`) on `127.0.0.1:8090` → nginx reverse proxy with a Let's Encrypt certificate at `bup-preli-la-team.inovate.it.com`. The container uses `restart: unless-stopped`.
- **Release + redeploy:** `scripts/deploy_vps.sh v1.0.x` builds a multi-arch image, pushes it to Docker Hub, then pulls and restarts that exact tag on the VPS.
- **Fallback image:** `damegami2782/gridwise:v1.0.0` on Docker Hub. It is multi-arch, runs as a non-root user and has a built-in `HEALTHCHECK`.

## 7. Security and secret handling
- `.env` is git-ignored and docker-ignored. Only `.env.example`, with empty values, is committed.
- API keys are injected at runtime and are never logged, echoed or baked into the image.
- Error responses carry only generic messages and field locations. Input values and stack traces are never returned.

## 8. Known limitations
- **Free-tier limits.** Groq allows roughly 5–7 fresh calls per minute per model, and Cerebras `gpt-oss-120b` allows 5 requests/min. Cerebras `qwen-3.8-27b` (450 requests/min, 450K tokens/min) carries the load in bursts. Only if every provider is exhausted do requests fall back to the rule parser.
- Notes are expected to express whole-hour windows, as the Problem Statement specifies. Sub-hour times are interpreted by the LLM at hour granularity.
- **Directive conflicts:** if hard directives truly contradict each other (organizers guarantee they won't), the service returns the minimum-violation schedule and says so in `plan_summary`, instead of failing.
- **Cache:** it is in-memory and per process, and is lost on restart.

## 9. Dependencies and credits
- [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [Pydantic](https://docs.pydantic.dev/): API and validation
- [SciPy](https://scipy.org/) `linprog` with the [HiGHS](https://highs.dev/) solver, and NumPy: optimization
- [Groq Python SDK](https://github.com/groq/groq-python) and [OpenAI Python SDK](https://github.com/openai/openai-python) (for Cerebras's OpenAI-compatible API): LLM access; the models are OpenAI gpt-oss-120b / gpt-oss-20b and Qwen 3.8 27B, served by Groq and Cerebras
- python-dotenv, pytest, httpx
- Built with help from Claude Code (Anthropic) as an AI coding assistant. The architecture and design decisions are the team's.

All scenario data is synthetic, taken from the organizer's public sample pack.
