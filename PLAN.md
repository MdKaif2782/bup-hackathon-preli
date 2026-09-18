# GridWise LLM: Execution Plan (BUP CSE Fest 2026 Preliminary)

Round window: **7:00 PM – 11:00 PM (4h)**. Target: a working deployed endpoint by **8:45 PM**. Use the rest of the time to harden it and finish the submission.

---

## 1. What we are building (one paragraph)

One HTTP service with `GET /health` returning `{"status":"ok"}` and `POST /optimize-energy`.
It takes a 24-hour scenario (demand, solar, tariff, battery) plus 1–3 operator notes.
**An LLM** turns each note into one of 6 directive types. **Deterministic guardrails** validate that output. An **LP optimizer** then builds the cheapest valid 24-hour grid/solar/battery schedule under those directives.
The judge replays our `hourly_plan` against the *ground-truth* directives, so wrong extraction means an invalid case and zero optimization credit.

### Where the 100 points are (drives priority)
| Category | Pts | What wins it |
|---|---|---|
| LLM interpretation | 25 | right type / hours / numbers under paraphrase |
| Directive application + constraints | 25 | LP with hard constraints + replay validator |
| Optimization quality | 10 | true optimum (LP gives it) |
| API contract & schema | 10 | exact fields, order, 400 on bad input |
| Performance & reliability | 10 | p95 ≤ 5s, never 5xx on valid input, no leaked secrets |
| Deployment & Docker | 10 | public URL + pullable image with exact tag |
| Docs & local repro | 10 | copy-paste README quickstart + sample test command |

The video carries no base points. It only breaks ties, but it is still required.

---

## 2. Tech choices

| Concern | Choice | Why |
|---|---|---|
| Language / API | **Python 3.11 + FastAPI + Uvicorn** | fastest to write, pydantic validation |
| Optimizer | **LP via `scipy.optimize.linprog` (HiGHS)** | exact optimum in ms; hard constraints are linear |
| LLM | provider-agnostic client, env `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY`. Default: a fast model with JSON / tool output (e.g. Claude Haiku 4.5 `claude-haiku-4-5`, or gpt-4o-mini / Gemini Flash, depending on which key the team has) | p95 ≤ 5s needs a small, fast model |
| Container | Docker (`python:3.11-slim`), port `8000`, bind `0.0.0.0` | fallback requirement |
| Hosting | Railway / Render (paid, no sleep) / Fly.io / Cloud Run with min-instances=1 | health must be ready ≤ 60s, so **avoid free tiers that sleep** |
| Registry | Docker Hub or GHCR, exact tag e.g. `gridwise:v1.0.0` + digest | "pullable exact tag/digest" |

---

## 3. Repo layout

```
app/
  main.py            # FastAPI app, routes, error handlers (400/500, no stack traces)
  schemas.py         # pydantic request/response models
  llm/
    client.py        # provider wrapper, timeout, retry, LRU cache by note text
    prompt.py        # system prompt + few-shot examples (paraphrases, NOT sample wording)
    interpret.py     # LLM → intermediate JSON → canonical directive
  guardrails.py      # deterministic validation + normalization of directives
  optimizer.py       # LP build/solve, slack fallback, post-processing + rounding
  validator.py       # judge-style replay of hourly_plan (used in-service AND in tests)
  summary.py         # deterministic plan_summary text
tests/
  test_optimizer.py      # 10 samples with GROUND-TRUTH directives → cost == expected
  test_guardrails.py
  test_validator.py
  paraphrases.json       # ~40 self-written paraphrased notes + expected directives
scripts/
  run_samples.py     # POST every public sample to a base URL, replay + score it
  eval_llm.py        # run paraphrases.json through the interpreter and report accuracy
Dockerfile  .dockerignore  requirements.txt  .env.example  README.md
```

---

## 4. Key design decisions

### 4.1 LLM interpretation: the LLM does language, code does arithmetic
Send all notes to the LLM in **one call** (temperature 0, JSON / tool-call output). Ask it for an **intermediate schema**, not the final one:

```json
{"note_index":0, "directive_type":"minimum_battery_reserve",
 "start_hour":18, "end_hour":21,            // end exclusive; 24 = midnight
 "value":50, "value_unit":"percent_of_capacity", // kwh | percent_of_capacity | factor_remaining | percent_reduction | percent_remaining
 "explanation":"..."}
```
Code then derives the final directive:
- hours = expand `[start, end)`, wrapping past midnight (`22→2` gives `[0,1,22,23]`), then sort and dedupe.
- `percent_reduction 80` → factor 0.2; `percent_remaining 25` / "one-fifth" → 0.25 / 0.2.
- `percent_of_capacity 50` → `0.5 * capacity_kwh` (SAMPLE-03 needs this).
- `max_grid_window` value is kWh.

Why: LLMs slip on off-by-one hour lists and 1−x arithmetic. This removes both failure modes and stays compliant, because the LLM still produces the interpretation.

Prompt contents:
- the 6 types with their semantic cues: "charger isolated / charging circuit unavailable" = no_charge; "relay / protection testing, must not discharge" = no_discharge; "feeder / transformer / substation limit, grid intake" = max_grid; "keep / hold / reserve at least" = reserve; "cloud / panel washing / inverter work / PV drop" = solar_reduction.
- the end-exclusive rule and the 12-hour to 24-hour conversion ("noon" = 12, "midnight" = 0/24).
- the rule that anything not about *today's* energy schedule (next week, next month, admin news, menus) is no_op.
- ~8 few-shot examples written **as our own paraphrases** (never copy sample wording).

Reliability:
- 10s timeout, 1 retry, then a **degraded fallback**: a regex/rule parser that is used *only* when the LLM errors, flagged in `explanation`. The LLM stays the primary path, which keeps us compliant.
- In-memory LRU cache keyed on the note text. Repeated judge calls then become instant.

### 4.2 Guardrails (`guardrails.py`)
Every rule from PS §08:
- type must be in the enum; unknown types become no_op with an explanation, never a new type.
- exactly one entry per note, in index order; fill missing notes with no_op.
- hours are unique ints 0–23, ascending, non-empty; an empty window becomes no_op.
- factor ∈ [0,1]; reserve is finite, ≥ 0 and ≤ capacity (clamp); max_grid is finite and ≥ 0.
- no_op ⇒ `applies=false, structured_adjustment=null`; otherwise `applies=true` and the exact key set for that type (no extra keys).

### 4.3 Optimizer (`optimizer.py`): LP, 24 hours × 5 variables
Variables per hour: `g ≥ 0` (grid), `s ∈ [0, eff_solar]`, `c ∈ [0, max_charge]`, `d ∈ [0, max_discharge]`, `E ∈ [minE_h, cap]`.

Constraints:
- `g + s + d = demand + c`
- `E_h = E_{h-1} + c − d`, with `E_{-1} = initial`, and `E_23 = initial`
- `minE_h = max(base_min, reserve directive)`
- no_charge ⇒ `c_h ub = 0`; no_discharge ⇒ `d_h ub = 0`; max_grid ⇒ `g_h ub = cap`
- solar_reduction ⇒ `eff_solar = solar × factor`; multiple reductions on the same hour multiply

Objective: `min Σ tariff·g` + a tiny `ε·Σ(c+d)` to discourage pointless cycling.

Post-process:
- net `c − d` into one action per hour (there are no losses, so this is equivalent).
- round to 3 decimals, then **recompute `E` and `g` from the rounded values** so balance and neutrality hold exactly.
- `battery_kwh = 0` when idle.
- totals and peak are computed from the final rounded plan.

If the LP is infeasible (usually a misread directive), re-solve with **penalized slack** on directive constraints (big-M). This still returns a physically valid plan and never a 500.

### 4.4 Self-check before responding
Run `validator.py` (a judge replay) on every response. If it fails, log a safe message and fall back to the slack solve. Tests use the same validator.

### 4.5 API errors
- Invalid JSON or schema (wrong types, ≠24 hours, duplicate hours, 0 or >3 notes, empty note) → **400** with `{"error": "..."}`. FastAPI's default is 422, so override it.
- Anything unexpected → **500** `{"error":"internal error"}`. Never return a stack trace, and never log the key.

---

## 5. Timeline and commits (each commit ends with the co-author trailer)

Commit convention: small, frequent, conventional messages. Every commit ends with:
```
Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
```
Add one `Co-Authored-By: Name <email>` line per teammate who paired on that commit.
Push to the **private** repo after each phase.

| Time | Phase | Tasks | Commit(s) |
|---|---|---|---|
| **7:00–7:15** | 0. Bootstrap | read spec, freeze this plan, venv, `requirements.txt`, `.gitignore` (`.env`!), `.env.example` | `docs: add execution plan` · `chore: scaffold python project and gitignore` |
| **7:15–7:40** | 1. Contract | pydantic request/response schemas, `/health`, `/optimize-energy` stub, 400/500 handlers | `feat: request/response schemas` · `feat: health endpoint and error handlers` |
| **7:40–8:20** | 2. Optimizer (core) | LP build, directives applied, netting and rounding, `validator.py` replay; test all 10 samples **with ground-truth directives** and match expected `total_cost_bdt` ±0.01 | `feat: LP optimizer with battery constraints` · `feat: apply directives in optimizer` · `feat: judge-style plan validator` · `test: public samples reproduce optimal cost` |
| **8:20–8:50** | 3. LLM path | client + prompt + intermediate schema → canonical conversion, cache, timeout/retry | `feat: LLM client with timeout, retry, cache` · `feat: note interpretation prompt and parser` |
| **8:50–9:05** | 4. Guardrails + wiring | `guardrails.py`, end-to-end pipeline, deterministic `plan_summary`, slack fallback | `feat: deterministic directive guardrails` · `feat: wire LLM → guardrails → optimizer pipeline` |
| **9:05–9:25** | 5. Deploy (early!) | Dockerfile, build, run locally, push image with tag, deploy to host with secret env var, test from outside with curl | `build: dockerfile and dockerignore` · `ci: deployment config` |
| **9:25–10:00** | 6. Harden LLM | write `paraphrases.json` (~40 notes: 12h/24h times, "noon/midnight", "one-fifth", "by 80%", "half", % of capacity, overnight windows, distractors), run `eval_llm.py`, fix prompt until 100% | `test: paraphrase robustness set` · `fix: prompt handling for <case>` (1 per fix) |
| **10:00–10:20** | 7. Reliability | malformed-input tests, LLM-down fallback test, latency check (20 sequential requests, p95), no secrets in logs | `test: malformed input and provider failure` · `perf: ...` if needed |
| **10:20–10:40** | 8. Docs | README: overview, architecture diagram, env var names, model/provider, guardrails, optimizer, quickstart (venv + Docker), curl examples, `scripts/run_samples.py` usage + expected output, limitations, credits | `docs: README with quickstart and architecture` |
| **10:40–10:50** | 9. Release | final image tag `v1.0.0` + digest in README, redeploy, run `run_samples.py` against the **public URL** | `release: v1.0.0` |
| **10:50–11:00** | 10. Submit | submit URL + repo + image + video link; **make repo public only after the deadline** | — |
| after 11:00 | Video | 3-minute screen recording: problem → pipeline diagram → code tour (prompt, guardrails, LP) → live curl + sample script | — |

**Time buffer:** if phase 2 or 3 overruns, cut from phase 6 first. Never skip phase 5 (deploy by ~9:25), because a late deployment surprise is the biggest risk.

---

## 6. Definition of done (pre-submit checklist)

- [ ] `curl $URL/health` → `{"status":"ok"}` from outside our network
- [ ] `python scripts/run_samples.py --base-url $URL`: 10/10 interpretations match, 10/10 plans valid on replay, 10/10 costs equal to expected ±0.01
- [ ] `eval_llm.py` shows ≥ 95% on the paraphrase set
- [ ] p95 latency < 5s (cold cache)
- [ ] bad JSON → 400, 4 notes → 400, 23 hours → 400, LLM key missing → still 200 via fallback, never 5xx
- [ ] `git log -p | grep -i "sk-\|api_key="` returns nothing; `.env` is not tracked; the image has no secrets (`docker history`, env)
- [ ] `docker pull <image>:v1.0.0 && docker run -p 8000:8000 -e LLM_API_KEY=... <image>:v1.0.0` → health OK
- [ ] README quickstart has been followed from a fresh clone
- [ ] Repo private until 11:00 PM, then public

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| LLM off-by-one hours / wrong factor | intermediate schema + deterministic expansion (§4.1) |
| Provider outage or rate limit during judging | retry, fallback parser, cache, spare key/provider via env |
| Host sleeps / cold start > 60s | paid tier or min-instances=1 |
| LP infeasible from a misread | slack re-solve, so the response is always valid JSON |
| Float drift fails tolerance | round, then recompute state and grid from rounded values |
| 422 vs 400 mismatch | custom validation handler returning 400 |
| Simultaneous charge + discharge | netting post-process + ε cycling penalty |

---

## 8. Open decisions (need the team)
1. **Which LLM API key do we have?** The Claude, OpenAI, Gemini and Groq options are all plug-in via env; the default model gets pinned once we know.
2. **Hosting platform** and who holds the account.
3. Registry: Docker Hub or GHCR (namespace).
