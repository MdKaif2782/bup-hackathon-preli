"""Groq client: strict JSON-schema call with model fallback chain, timeouts and a note cache."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict

from app.guardrails import check_item
from app.llm.prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, user_message

log = logging.getLogger("gridwise.llm")

PER_CALL_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "8"))
TOTAL_BUDGET_S = float(os.getenv("LLM_TOTAL_BUDGET_S", "18"))
CACHE_SIZE = 2048
# qwen on Groq enforces 1000 output tokens/min; the request cap counts against it
MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "600"))


class LLMUnavailable(Exception):
    """Every configured model failed (network, rate limit, bad output)."""


def _models() -> list[str]:
    primary = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    fallbacks = [m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "").split(",") if m.strip()]
    seen, out = set(), []
    for m in [primary, *fallbacks]:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from groq import Groq

                key = os.getenv("GROQ_API_KEY")
                if not key:
                    raise LLMUnavailable("GROQ_API_KEY not configured")
                # retries handled here (model fallback), not inside the SDK
                _client = Groq(api_key=key, timeout=PER_CALL_TIMEOUT_S, max_retries=0)
    return _client


class _LRU:
    def __init__(self, size: int):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, k):
        with self.lock:
            if k in self.data:
                self.data.move_to_end(k)
                return self.data[k]
        return None

    def put(self, k, v):
        with self.lock:
            self.data[k] = v
            self.data.move_to_end(k)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


_cache = _LRU(CACHE_SIZE)

# model -> monotonic time until which it is skipped after a 429
_cooldown: dict[str, float] = {}
DEFAULT_COOLDOWN_S = 20.0


def _retry_after(exc: Exception) -> float:
    try:
        return min(60.0, float(exc.response.headers.get("retry-after")))  # type: ignore[attr-defined]
    except Exception:
        return DEFAULT_COOLDOWN_S


def _cache_key(note: str) -> str:
    return " ".join(note.split()).lower()


def _call_model(model: str, notes: list[str], timeout: float) -> list[dict]:
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message(notes)},
        ],
        temperature=0,
        max_completion_tokens=MAX_OUTPUT_TOKENS,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "note_interpretations", "schema": RESPONSE_SCHEMA, "strict": True},
        },
        timeout=timeout,
    )
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"
    resp = _get_client().chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    items = json.loads(content)["interpretations"]
    if not isinstance(items, list) or len(items) != len(notes):
        raise ValueError("wrong number of interpretations")
    by_idx = {}
    for it in items:
        idx = it.get("note_index")
        if not isinstance(idx, int) or not 0 <= idx < len(notes) or idx in by_idx:
            raise ValueError("bad note_index mapping")
        check_item(it)  # guardrail failure -> caller falls through to the next model
        by_idx[idx] = it
    return [by_idx[i] for i in range(len(notes))]


def interpret_notes(notes: list[str]) -> tuple[list[dict], str]:
    """Returns (raw LLM items aligned to notes, model label). Raises LLMUnavailable."""
    results: list[dict | None] = [_cache.get(_cache_key(n)) for n in notes]
    missing = [i for i, r in enumerate(results) if r is None]
    if not missing:
        return [dict(r) for r in results], "cache"

    deadline = time.monotonic() + TOTAL_BUDGET_S
    last_err = "no model configured"
    models = _models()
    now = time.monotonic()
    ready = [m for m in models if _cooldown.get(m, 0) <= now]
    # if everything is cooling down, still try them all rather than fail outright
    for model in ready or models:
        remaining = deadline - time.monotonic()
        if remaining < 1.5:
            break
        try:
            t0 = time.monotonic()
            items = _call_model(model, [notes[i] for i in missing], min(PER_CALL_TIMEOUT_S, remaining))
            log.info("llm ok model=%s notes=%d %.2fs", model, len(missing), time.monotonic() - t0)
            for i, it in zip(missing, items):
                it = {k: v for k, v in it.items() if k != "note_index"}
                _cache.put(_cache_key(notes[i]), it)
                results[i] = it
            return [dict(r) for r in results], model
        except LLMUnavailable:
            raise
        except Exception as exc:  # rate limit, timeout, bad JSON, schema error -> next model
            last_err = type(exc).__name__
            if last_err == "RateLimitError":
                _cooldown[model] = time.monotonic() + _retry_after(exc)
            log.warning("llm failed model=%s err=%s", model, last_err)
    raise LLMUnavailable(last_err)
