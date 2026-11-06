"""Runtime configuration from environment (.env is loaded automatically)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


@dataclass(frozen=True)
class Settings:
    model_smart: str = field(
        default_factory=lambda: _env("CLOSEBOOKS_MODEL_SMART", "claude-opus-5-5")
    )
    model_fast: str = field(
        default_factory=lambda: _env("CLOSEBOOKS_MODEL_FAST", "claude-haiku-5-5")
    )
    llm_offline: bool = field(default_factory=lambda: _env("CLOSEBOOKS_LLM_OFFLINE", "0") == "1")
    cache_dir: Path = field(
        default_factory=lambda: Path(_env("CLOSEBOOKS_CACHE_DIR", ".cache/llm"))
    )
    auto_post_min_confidence: float = field(
        default_factory=lambda: float(_env("CLOSEBOOKS_AUTO_POST_MIN_CONFIDENCE", "0.90"))
    )
    approval_threshold: Decimal = field(
        default_factory=lambda: Decimal(_env("CLOSEBOOKS_APPROVAL_THRESHOLD", "5000"))
    )


def get_settings() -> Settings:
    return Settings()
