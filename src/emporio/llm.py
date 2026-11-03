"""Cliente LLM agnóstico de provedor, com fallback entre modelos.

Usa o SDK da OpenAI porque Gemini, Groq, OpenAI e Ollama expõem a mesma
API de chat completions; trocar de provedor é só mudar o .env.

No plano gratuito do Gemini cada modelo tem a própria cota (ex.: 5 req/min,
20 req/dia). Quando um modelo devolve 429, ele entra em espera e o próximo
da lista assume — o cliente final não percebe.
"""

from __future__ import annotations

import logging
import time

import openai
from openai import OpenAI

from .config import Settings

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60


class AllModelsUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise ValueError("LLM_API_KEY não configurada. Copie .env.example para .env e preencha.")
        # max_retries=0: quem decide retentar ou trocar de modelo é o fallback abaixo.
        self.client = OpenAI(base_url=settings.base_url, api_key=settings.api_key, max_retries=0, timeout=60)
        self.models = settings.models
        self._cooldown_until: dict[str, float] = {}
        self._unavailable: set[str] = set()

    def chat(self, messages: list[dict], tools: list[dict]):
        """Retorna (mensagem, modelo_usado)."""
        errors = []
        for model in self.models:
            if model in self._unavailable or self._cooldown_until.get(model, 0) > time.time():
                continue
            for attempt in range(2):
                try:
                    resp = self.client.chat.completions.create(
                        model=model, messages=messages, tools=tools, temperature=0.3,
                    )
                    return resp.choices[0].message, model
                except openai.RateLimitError as e:
                    log.warning("Cota esgotada em %s; tentando o próximo modelo.", model)
                    self._cooldown_until[model] = time.time() + COOLDOWN_SECONDS
                    errors.append(f"{model}: 429 {e.message}")
                    break
                except openai.NotFoundError as e:
                    log.warning("Modelo %s não encontrado no provedor; removido da lista.", model)
                    self._unavailable.add(model)
                    errors.append(f"{model}: 404 {e.message}")
                    break
                except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as e:
                    errors.append(f"{model}: {type(e).__name__}")
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    break
        raise AllModelsUnavailable("; ".join(errors) or "Todos os modelos estão em espera por cota.")
