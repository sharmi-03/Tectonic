"""Model integration for the platform modules: one place that talks to Gemini.

Model roles (each overridable by environment variable):
  * reasoning  — GEMINI_MODEL (default gemini-3.6-flash): grounded answers in Agentic-RAG.
  * utility    — TECTONIC_UTILITY_MODEL (default gemini-3.5-flash-lite): fast, cheap structured
                 calls — RAG routing, groundedness critique, query reformulation, audit
                 natural-language → filter translation, PromptOps reflection.
  * embeddings — TECTONIC_EMBEDDING_MODEL (default gemini-embedding-2) at TECTONIC_EMBEDDING_DIM
                 (default 768, Matryoshka-truncated and re-normalised), with retrieval task types.

Every call has a deterministic offline fallback (TECTONIC_OFFLINE=1, no API key, or an API
error), so the platform keeps working and tests never touch the network. Token usage from every
call is reported through ``set_usage_hook`` so FinOps accounts for the platform's own LLM spend.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

_log = logging.getLogger("tectonic_runtime.models")

UsageHook = Callable[[dict[str, Any]], Awaitable[None] | None]
_usage_hook: UsageHook | None = None

# Which component triggered a model call (for FinOps attribution); set by module wrappers.
current_caller: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "tectonic_model_caller", default={"agent_id": "runtime", "team": None, "purpose": "unspecified"}
)


def set_usage_hook(hook: UsageHook | None) -> None:
    global _usage_hook
    _usage_hook = hook


def reasoning_model() -> str:
    return os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"


def utility_model() -> str:
    return os.getenv("TECTONIC_UTILITY_MODEL") or "gemini-3.5-flash-lite"


def embedding_model() -> str:
    return os.getenv("TECTONIC_EMBEDDING_MODEL") or "gemini-embedding-2"


def embedding_dim() -> int:
    return int(os.getenv("TECTONIC_EMBEDDING_DIM", "768"))


def _api_key() -> str:
    return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""


def online() -> bool:
    return bool(_api_key()) and os.getenv("TECTONIC_OFFLINE", "0") != "1"


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


async def _report(model: str, kind: str, input_tokens: int, output_tokens: int, estimated: bool) -> None:
    if _usage_hook is None:
        return
    caller = current_caller.get()
    event = {"model": model, "kind": kind, "input_tokens": input_tokens, "output_tokens": output_tokens,
             "estimated_tokens": estimated, **caller}
    try:
        result = _usage_hook(event)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # accounting must never break a model call
        _log.exception("usage hook failed")


# --- generation --------------------------------------------------------------------------------
@dataclass
class Generation:
    text: str
    model: str
    offline: bool
    input_tokens: int = 0
    output_tokens: int = 0


_client = None


def _genai_client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=_api_key())
    return _client


async def generate(prompt: str, *, model: str | None = None, system: str | None = None, json_mode: bool = False,
                   temperature: float = 0.2, max_output_tokens: int = 1024, timeout_s: float = 30.0) -> Generation | None:
    """Gemini text generation. Returns None when offline or on error (callers then use a heuristic)."""
    if not online():
        return None
    from google.genai import types

    model = model or utility_model()
    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        system_instruction=system,
        response_mime_type="application/json" if json_mode else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # no tools on utility calls
    )
    try:
        resp = await asyncio.wait_for(
            _genai_client().aio.models.generate_content(model=model, contents=prompt, config=config), timeout_s
        )
    except Exception as exc:
        _log.warning("gemini generate failed (%s): %s", model, exc)
        return None
    usage = getattr(resp, "usage_metadata", None)
    in_tok = getattr(usage, "prompt_token_count", None) or estimate_tokens(prompt + (system or ""))
    out_tok = getattr(usage, "candidates_token_count", None) or estimate_tokens(resp.text or "")
    await _report(model, "generate", in_tok, out_tok, estimated=usage is None)
    return Generation(text=(resp.text or "").strip(), model=model, offline=False, input_tokens=in_tok, output_tokens=out_tok)


async def generate_json(prompt: str, **kwargs: Any) -> dict[str, Any] | None:
    gen = await generate(prompt, json_mode=True, **kwargs)
    if gen is None or not gen.text:
        return None
    text = gen.text
    match = re.search(r"\{.*\}", text, re.S)
    try:
        value = json.loads(match.group(0) if match else text)
    except (json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, dict) else None


# --- embeddings ----------------------------------------------------------------------------------
# Retrieval task type for the *next* embed call; Vector DB's port has no task argument, so the
# Agentic-RAG / Vector DB wrappers set this around queries.
embedding_task: contextvars.ContextVar[str] = contextvars.ContextVar("tectonic_embedding_task", default="RETRIEVAL_DOCUMENT")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def hash_embed(text: str, dim: int | None = None) -> list[float]:
    """Offline fallback: signed feature hashing of unigrams + bigrams, L2-normalised.

    Captures lexical overlap only (no semantics), but is deterministic, fast and dimension-
    compatible with the Gemini vectors, so the vector store schema never changes between modes.
    """
    dim = dim or embedding_dim()
    vec = [0.0] * dim
    toks = _TOKEN_RE.findall(text.lower())
    feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class EmbeddingService:
    """Gemini embeddings with an LRU cache and per-text fallback. Satisfies Vector DB's port."""

    def __init__(self, cache_size: int = 4096) -> None:
        self._cache: OrderedDict[tuple[str, str, str], list[float]] = OrderedDict()
        self._cache_size = cache_size
        self.last_mode = "offline" if not online() else "gemini"

    @property
    def model_name(self) -> str:
        return embedding_model() if online() else f"local-hash-{embedding_dim()}"

    def _cached(self, key: tuple[str, str, str]) -> list[float] | None:
        vec = self._cache.get(key)
        if vec is not None:
            self._cache.move_to_end(key)
        return vec

    def _store(self, key: tuple[str, str, str], vec: list[float]) -> None:
        self._cache[key] = vec
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def embed_many(self, texts: list[str], task: str | None = None) -> list[list[float]]:
        task = task or embedding_task.get()
        model = self.model_name
        out: list[list[float] | None] = [self._cached((model, task, t)) for t in texts]
        missing = [i for i, v in enumerate(out) if v is None]
        if missing and online():
            from google.genai import types

            try:
                resp = await asyncio.wait_for(
                    _genai_client().aio.models.embed_content(
                        model=embedding_model(),
                        contents=[texts[i] for i in missing],
                        config=types.EmbedContentConfig(output_dimensionality=embedding_dim(), task_type=task),
                    ),
                    30.0,
                )
                for i, emb in zip(missing, resp.embeddings):
                    values = list(emb.values)
                    norm = math.sqrt(sum(v * v for v in values)) or 1.0  # truncated dims must be re-normalised
                    out[i] = [v / norm for v in values]
                    self._store((model, task, texts[i]), out[i])  # type: ignore[arg-type]
                await _report(embedding_model(), "embed", sum(estimate_tokens(texts[i]) for i in missing), 0, estimated=True)
                self.last_mode = "gemini"
            except Exception as exc:
                _log.warning("gemini embeddings failed, using local fallback: %s", exc)
                self.last_mode = "offline"
        for i in range(len(out)):
            if out[i] is None:
                out[i] = hash_embed(texts[i])
        return out  # type: ignore[return-value]

    # vector_db.core.ports.EmbeddingProvider
    async def embed(self, text: str, *, model: str | None = None, tenant_id: str = "") -> list[float]:
        return (await self.embed_many([text]))[0]


_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Process-wide embedder, so Vector DB and Agentic-RAG share one cache."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
