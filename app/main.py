"""FastAPI entrypoint: GET /health and POST /optimize-energy."""
from __future__ import annotations

import json
import logging

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.pipeline import InfeasibleScenario, run_pipeline  # noqa: E402
from app.schemas import OptimizeRequest  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("gridwise")

app = FastAPI(title="GridWise LLM Energy Optimizer", version="1.0.0")


def _error(status: int, message: str, details: list | None = None) -> JSONResponse:
    body: dict = {"error": message}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status, content=body)


def _clean_errors(exc: ValidationError | RequestValidationError) -> list[dict]:
    # Only location + message: never echo raw input values back.
    return [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", "invalid")}
        for e in exc.errors()
    ][:20]


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _error(400, "invalid request", _clean_errors(exc))


@app.exception_handler(Exception)
async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled error: %s", type(exc).__name__)
    return _error(500, "internal error")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/optimize-energy")
async def optimize_energy(request: Request) -> JSONResponse:
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _error(400, "malformed JSON")
    if not isinstance(payload, dict):
        return _error(400, "request body must be a JSON object")
    try:
        req = OptimizeRequest.model_validate(payload)
    except ValidationError as exc:
        return _error(400, "invalid request", _clean_errors(exc))

    try:
        # run_pipeline is blocking (LLM HTTP + LP solve); keep the event loop free.
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(run_pipeline, req)
    except InfeasibleScenario as exc:
        return _error(422, str(exc))
    except Exception as exc:  # controlled failure, no stack trace to client
        log.error("pipeline failure for %s: %s", req.scenario_id, type(exc).__name__)
        return _error(500, "internal error")
    return JSONResponse(status_code=200, content=result)
