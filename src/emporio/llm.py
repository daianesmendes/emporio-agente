"""Cliente LLM agnóstico de provedor, com fallback entre modelos e provedores.

Usa o SDK da OpenAI porque Gemini, Groq, OpenAI e Ollama expõem a mesma
API de chat completions; trocar de provedor é só mudar o .env.

No plano gratuito do Gemini cada modelo tem a própria cota (ex.: 5 req/min,
20 req/dia). Quando um modelo falha (429 cota, 503 sobrecarga, timeout), ele
entra em espera e o próximo da lista assume; esgotado o provedor principal,
entra o provedor de reserva (ex.: Groq). O cliente final não percebe.
"""

from __future__ import annotations

import logging
import time

import openai
from openai import OpenAI

from .config import Provider, Settings

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60
REQUEST_TIMEOUT = 30

# Campos padrão de mensagem. Provedores diferentes podem rejeitar campos
# extras de outro (ex.: assinaturas de raciocínio do Gemini).
_STANDARD_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}


class AllModelsUnavailable(RuntimeError):
    pass


def _portable(messages: list[dict]) -> list[dict]:
    clean = []
    for m in messages:
        msg = {k: v for k, v in m.items() if k in _STANDARD_KEYS}
        if "tool_calls" in msg:
            msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                for tc in msg["tool_calls"]
            ]
        clean.append(msg)
    return clean


class LLMClient:
    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise ValueError("LLM_API_KEY não configurada. Copie .env.example para .env e preencha.")
        self.providers: list[Provider] = settings.providers
        # max_retries=0: quem decide retentar ou trocar de modelo é o fallback abaixo.
        self.clients = {
            p.name: OpenAI(base_url=p.base_url, api_key=p.api_key, max_retries=0, timeout=REQUEST_TIMEOUT)
            for p in self.providers
        }
        self._cooldown_until: dict[str, float] = {}
        self._unavailable: set[str] = set()

    # Mantido para compatibilidade com quem usa um único cliente (ex.: testes).
    @property
    def client(self):
        return self.clients[self.providers[0].name]

    @client.setter
    def client(self, value):
        self.clients[self.providers[0].name] = value

    def chat(self, messages: list[dict], tools: list[dict]):
        """Retorna (mensagem, modelo_usado)."""
        errors = []
        for i, provider in enumerate(self.providers):
            # O provedor principal recebe o histórico como está; os demais, só campos padrão.
            payload = messages if i == 0 else _portable(messages)
            extra = {"reasoning_effort": provider.reasoning_effort} if provider.reasoning_effort else {}
            for model in provider.models:
                key = f"{provider.name}:{model}"
                if key in self._unavailable or self._cooldown_until.get(key, 0) > time.time():
                    continue
                try:
                    resp = self.clients[provider.name].chat.completions.create(
                        model=model, messages=payload, tools=tools, temperature=0.3, **extra,
                    )
                    return resp.choices[0].message, model
                except openai.NotFoundError as e:
                    log.warning("Modelo %s não existe no provedor; removido da lista.", model)
                    self._unavailable.add(key)
                    errors.append(f"{model}: 404 {e.message}")
                except openai.BadRequestError as e:
                    # Ex.: o modelo não aceita algo do histórico vindo de outro provedor.
                    log.warning("%s recusou a requisição (400); usando o próximo modelo.", model)
                    errors.append(f"{model}: 400 {e.message}")
                except (openai.RateLimitError, openai.InternalServerError,
                        openai.APITimeoutError, openai.APIConnectionError) as e:
                    # Cota (429), sobrecarga (503) ou lentidão: não insiste no mesmo
                    # modelo — a própria lista de fallback é a estratégia de retentativa.
                    log.warning("%s indisponível (%s); usando o próximo modelo.", model, type(e).__name__)
                    self._cooldown_until[key] = time.time() + COOLDOWN_SECONDS
                    errors.append(f"{model}: {type(e).__name__}")
        raise AllModelsUnavailable("; ".join(errors) or "Todos os modelos estão em espera.")
