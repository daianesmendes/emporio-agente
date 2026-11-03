"""Configuração lida do ambiente (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    models: list[str]
    now_override: str


def get_settings() -> Settings:
    return Settings(
        base_url=os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        api_key=os.getenv("LLM_API_KEY", ""),
        models=[m.strip() for m in os.getenv("LLM_MODELS", "gemini-3.8-flash").split(",") if m.strip()],
        now_override=os.getenv("AGENT_NOW", "").strip(),
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
