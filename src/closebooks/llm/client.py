"""Thin Claude client for the pipeline: structured outputs + disk cache + cost tracking.

Why a cache: during the hackathon we re-run the full pipeline dozens of times against the
leaderboard. Caching by (model, system, prompt, schema) makes re-runs free, fast and
deterministic, and lets tests run offline (CLOSEBOOKS_LLM_OFFLINE=1).

Usage:
    llm = LLMClient()
    result = llm.structured(MySchema, system="...", prompt="...", tier="fast")
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

from closebooks.config import Settings, get_settings

log = logging.getLogger(__name__)

Tier = Literal["fast", "smart"]

# USD per 1M tokens (input, output, cache read). Update if the organizers use other models.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


class LLMOfflineCacheMiss(RuntimeError):
    """Raised in offline mode when a prompt is not in the cache."""


class LLMRefusal(RuntimeError):
    """The model declined. The pipeline should route the item to human review."""


@dataclass
class Usage:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)

    def add(self, model: str, usage: anthropic.types.Usage) -> None:
        self.calls += 1
        cache_read = usage.cache_read_input_tokens or 0
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += cache_read
        p_in, p_out, p_cache = PRICES.get(model, (0.0, 0.0, 0.0))
        cost = (
            usage.input_tokens * p_in + usage.output_tokens * p_out + cache_read * p_cache
        ) / 1_000_000
        self.cost_usd += cost
        self.by_model[model] = self.by_model.get(model, 0.0) + cost

    def summary(self) -> str:
        return (
            f"LLM calls={self.calls} cache_hits={self.cache_hits} "
            f"in={self.input_tokens} out={self.output_tokens} "
            f"cache_read={self.cache_read_tokens} cost=${self.cost_usd:.4f}"
        )


class LLMClient:
    def __init__(self, settings: Settings | None = None, client: anthropic.Anthropic | None = None):
        self.settings = settings or get_settings()
        self._client = client
        self.usage = Usage()
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            # max_retries covers 408/409/429/5xx and connection errors with backoff
            self._client = anthropic.Anthropic(max_retries=4, timeout=120.0)
        return self._client

    def model_for(self, tier: Tier) -> str:
        return self.settings.model_fast if tier == "fast" else self.settings.model_smart

    # ------------------------------------------------------------------ cache

    def _key(self, model: str, system: str, prompt: str, schema: str) -> str:
        payload = json.dumps([model, system, prompt, schema], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.settings.cache_dir / key[:2] / f"{key}.json"

    def _cache_get(self, key: str) -> dict | None:
        path = self._cache_path(key)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _cache_put(self, key: str, data: dict) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ calls

    def structured[T: BaseModel](
        self,
        schema: type[T],
        *,
        system: str,
        prompt: str,
        tier: Tier = "fast",
        max_tokens: int = 4000,
    ) -> T:
        """Ask Claude for a response validated against a Pydantic schema.

        Keep `system` STABLE across calls (rules, chart of accounts, examples) and put the
        per-item data in `prompt`: the system prompt is marked for prompt caching.
        """
        model = self.model_for(tier)
        key = self._key(
            model, system, prompt, json.dumps(schema.model_json_schema(), sort_keys=True)
        )
        cached = self._cache_get(key)
        if cached is not None:
            self.usage.cache_hits += 1
            return schema.model_validate(cached["output"])
        if self.settings.llm_offline:
            raise LLMOfflineCacheMiss(f"offline and no cache for {schema.__name__} ({key[:8]})")

        response = self.client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        self.usage.add(model, response.usage)
        if response.stop_reason == "refusal":
            raise LLMRefusal(f"model refused ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError(f"hit max_tokens={max_tokens} for {schema.__name__}")
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError(f"no parsed output for {schema.__name__}")
        self._cache_put(
            key,
            {
                "model": model,
                "schema": schema.__name__,
                "request_id": response._request_id,
                "output": parsed.model_dump(mode="json"),
            },
        )
        return parsed
