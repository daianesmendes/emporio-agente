"""Testes do loop do agente e do fallback com LLM simulado (sem gastar cota)."""

import json
from types import SimpleNamespace

import openai
import pytest

from emporio import agent as agent_mod
from emporio.agent import FALLBACK_REPLY, Agent
from emporio.config import Settings
from emporio.data import Store
from emporio.llm import LLMClient


def _msg(content=None, tool_calls=None):
    calls = [
        SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
        for i, (n, a) in enumerate(tool_calls or [])
    ]
    dump = {"role": "assistant", "content": content}
    if calls:
        dump["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls
        ]
    return SimpleNamespace(
        content=content,
        tool_calls=calls or None,
        model_dump=lambda exclude_none=True: {k: v for k, v in dump.items() if v is not None},
    )


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append(messages)
        return self.script.pop(0), "fake-model"


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "SESSIONS_DIR", tmp_path)
    monkeypatch.setenv("AGENT_NOW", "2026-03-25 14:00")


def test_loop_executa_ferramenta_e_responde():
    llm = FakeLLM([
        _msg(tool_calls=[("detalhes_produto", {"produto": "Takamine GD20"})]),
        _msg("O Takamine GD20 sai por R$ 2.199,00!"),
    ])
    a = Agent(store=Store(), llm=llm)
    reply = a.reply("Quanto custa o Takamine GD20?")

    assert reply.text.startswith("O Takamine")
    assert [c.name for c in reply.tool_calls] == ["detalhes_produto"]
    tool_msg = llm.calls[1][-1]
    assert tool_msg["role"] == "tool" and "2.199,00" in tool_msg["content"]


def test_historico_persistido_entre_sessoes():
    a = Agent(store=Store(), llm=FakeLLM([_msg("Oi! Sou o Clave.")]), session_id="cliente1")
    a.reply("Oi")
    b = Agent(store=Store(), llm=FakeLLM([]), session_id="cliente1")
    assert [m["role"] for m in b.history] == ["user", "assistant"]


def test_teto_de_rodadas_devolve_mensagem_segura():
    loop = [_msg(tool_calls=[("buscar_produtos", {"termo": "violão"})])] * agent_mod.MAX_TOOL_ROUNDS
    reply = Agent(store=Store(), llm=FakeLLM(loop)).reply("violão")
    assert reply.text == FALLBACK_REPLY


def test_fallback_de_modelo_quando_cota_estoura():
    client = LLMClient(Settings(base_url="http://x", api_key="k", models=["m1", "m2"], now_override=""))
    used = []

    def create(model, **kwargs):
        used.append(model)
        if model == "m1":
            # Cria o erro sem depender da biblioteca HTTP interna do SDK.
            err = openai.RateLimitError.__new__(openai.RateLimitError)
            err.message = "quota"
            raise err
        return SimpleNamespace(choices=[SimpleNamespace(message="ok")])

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    msg, model = client.chat([], [])
    assert (msg, model) == ("ok", "m2")
    # m1 fica em espera: a próxima chamada já vai direto para m2.
    client.chat([], [])
    assert used == ["m1", "m2", "m2"]


def test_fallback_para_provedor_de_reserva_com_historico_limpo():
    from emporio.config import Provider

    reserva = Provider("reserva", "http://y", "k2", ["r1"])
    client = LLMClient(Settings(base_url="http://x", api_key="k", models=["m1"], now_override="", fallback=reserva))
    sent = {}

    def principal(model, **kwargs):
        err = openai.RateLimitError.__new__(openai.RateLimitError)
        err.message = "quota"
        raise err

    def secundario(model, messages, **kwargs):
        sent["messages"] = messages
        return SimpleNamespace(choices=[SimpleNamespace(message="ok")])

    client.clients["principal"] = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=principal)))
    client.clients["reserva"] = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=secundario)))

    historico = [{
        "role": "assistant", "content": None, "extra_content": {"google": {"thought_signature": "abc"}},
        "tool_calls": [{"id": "c1", "type": "function", "extra_content": {"x": 1},
                        "function": {"name": "buscar_produtos", "arguments": "{}"}}],
    }]
    assert client.chat(historico, []) == ("ok", "r1")
    # Campos específicos do Gemini não vão para o outro provedor.
    msg = sent["messages"][0]
    assert "extra_content" not in msg and "extra_content" not in msg["tool_calls"][0]
