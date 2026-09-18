"""LLM client: strict JSON-schema call over a multi-provider model chain (Groq + Cerebras),
with per-model rate-limit cooldown, timeouts and a note cache."""
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

PROVIDERS = {
    "groq": {"key_env": "GROQ_API_KEY"},
    "cerebras": {"key_env": "CEREBRAS_API_KEY", "base_url": "https://api.cerebras.ai/v1"},
}


class LLMUnavailable(Exception):
    """Every configured model failed (network, rate limit, bad output)."""


def _split_env(name: str, default: str = "") -> list[str]:
    return [m.strip() for m in os.getenv(name, default).split(",") if m.strip()]


def _models() -> list[tuple[str, str]]:
    """Ordered (provider, model) chain; providers without an API key are skipped.

    Default order: Groq primary -> Cerebras models (high rate limits) -> Groq fallbacks.
    LLM_CHAIN="provider:model,..." overrides the order explicitly.
    """
    if os.getenv("LLM_CHAIN"):
        chain = [tuple(e.split(":", 1)) for e in _split_env("LLM_CHAIN") if ":" in e]
    else:
        chain = [("groq", os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip())]
        chain += [("cerebras", m) for m in _split_env("CEREBRAS_MODELS", "qwen-3.8-27b,gpt-oss-120b")]
        chain += [("groq", m) for m in _split_env("GROQ_FALLBACK_MODELS")]
    out: list[tuple[str, str]] = []
    for provider, model in chain:
        cfg = PROVIDERS.get(provider)
        if cfg and os.getenv(cfg["key_env"]) and (provider, model) not in out:
            out.append((provider, model))
    return out


_clients: dict[str, object] = {}
_client_lock = threading.Lock()


def _get_client(provider: str):
    if provider not in _clients:
        with _client_lock:
            if provider not in _clients:
                cfg = PROVIDERS[provider]
                key = os.getenv(cfg["key_env"])
                if not key:
                    raise LLMUnavailable(f"{cfg['key_env']} not configured")
                # retries are handled here by walking the chain, not inside the SDKs
                if provider == "groq":
                    from groq import Groq

                    _clients[provider] = Groq(api_key=key, timeout=PER_CALL_TIMEOUT_S, max_retries=0)
                else:
                    from openai import OpenAI

                    _clients[provider] = OpenAI(api_key=key, base_url=cfg["base_url"],
                                                timeout=PER_CALL_TIMEOUT_S, max_retries=0)
    return _clients[provider]


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

# (provider, model) -> monotonic time until which it is skipped after a 429
_cooldown: dict[tuple[str, str], float] = {}
DEFAULT_COOLDOWN_S = 20.0


def _retry_after(exc: Exception) -> float:
    try:
        return min(60.0, float(exc.response.headers.get("retry-after")))  # type: ignore[attr-defined]
    except Exception:
        return DEFAULT_COOLDOWN_S


def _cache_key(note: str) -> str:
    return " ".join(note.split()).lower()


def _reasoning_kwargs(provider: str, model: str) -> dict:
    if "gpt-oss" in model:
        return {"reasoning_effort": "low"}
    if provider == "cerebras" and "qwen" in model:
        # Cerebras qwen reasons by default and the thinking tokens would exhaust the output cap
        return {"reasoning_effort": "none"}
    return {}


def _call_model(provider: str, model: str, notes: list[str], timeout: float) -> list[dict]:
    resp = _get_client(provider).chat.completions.create(
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
        **_reasoning_kwargs(provider, model),
    )
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

    models = _models()
    if not models:
        raise LLMUnavailable("no LLM provider key configured")
    deadline = time.monotonic() + TOTAL_BUDGET_S
    now = time.monotonic()
    ready = [m for m in models if _cooldown.get(m, 0) <= now]
    last_err = "no model available"
    # if everything is cooling down, still try them all rather than fail outright
    for provider, model in ready or models:
        remaining = deadline - time.monotonic()
        if remaining < 1.5:
            break
        label = f"{provider}:{model}"
        try:
            t0 = time.monotonic()
            items = _call_model(provider, model, [notes[i] for i in missing], min(PER_CALL_TIMEOUT_S, remaining))
            log.info("llm ok model=%s notes=%d %.2fs", label, len(missing), time.monotonic() - t0)
            for i, it in zip(missing, items):
                it = {k: v for k, v in it.items() if k != "note_index"}
                _cache.put(_cache_key(notes[i]), it)
                results[i] = it
            return [dict(r) for r in results], label
        except LLMUnavailable:
            raise
        except Exception as exc:  # rate limit, timeout, bad JSON, schema error -> next model
            last_err = type(exc).__name__
            if last_err == "RateLimitError":
                _cooldown[(provider, model)] = time.monotonic() + _retry_after(exc)
            log.warning("llm failed model=%s err=%s", label, last_err)
    raise LLMUnavailable(last_err)
