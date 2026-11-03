"""Configuração lida do ambiente (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    models: list[str]
    reasoning_effort: str = ""


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    models: list[str]
    now_override: str
    reasoning_effort: str = "low"
    fallback: Provider | None = None

    @property
    def providers(self) -> list[Provider]:
        primary = Provider("principal", self.base_url, self.api_key, self.models, self.reasoning_effort)
        return [primary] + ([self.fallback] if self.fallback else [])


def _models(var: str, default: str = "") -> list[str]:
    return [m.strip() for m in os.getenv(var, default).split(",") if m.strip()]


def get_settings() -> Settings:
    fallback = None
    fb_key = os.getenv("LLM_FALLBACK_API_KEY", "").strip()
    fb_models = _models("LLM_FALLBACK_MODELS")
    if fb_key and fb_models and not fb_key.startswith("cole-"):
        fallback = Provider(
            name="reserva",
            base_url=os.getenv("LLM_FALLBACK_BASE_URL", "").strip(),
            api_key=fb_key,
            models=fb_models,
            reasoning_effort=os.getenv("LLM_FALLBACK_REASONING_EFFORT", "").strip(),
        )
    return Settings(
        base_url=os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        api_key=os.getenv("LLM_API_KEY", ""),
        models=_models("LLM_MODELS", "gemini-3-flash-preview"),
        now_override=os.getenv("AGENT_NOW", "").strip(),
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "low").strip(),
        fallback=fallback,
    )


def now() -> datetime:
    """Data/hora de referência do agente.

    AGENT_NOW permite fixar o "agora" (ex.: 2026-03-25 14:00) para que prazos
    de troca e status de pedido sejam reproduzíveis nos exemplos e testes.
    """
    override = get_settings().now_override
    if override:
        fmt = "%Y-%m-%d %H:%M" if " " in override else "%Y-%m-%d"
        return datetime.strptime(override, fmt)
    return datetime.now()
