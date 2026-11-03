"""Loop do agente: LLM decide, ferramentas executam, LLM responde."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .data import Store
from .llm import AllModelsUnavailable, LLMClient
from .prompts import build_system_prompt
from .tools import run_tool, tool_schemas

log = logging.getLogger(__name__)

SESSIONS_DIR = Path(__file__).resolve().parents[2] / ".sessions"
MAX_TOOL_ROUNDS = 4       # teto de idas e voltas por mensagem (protege a cota)
MAX_HISTORY_MESSAGES = 40  # histórico enviado ao modelo

FALLBACK_REPLY = (
    "Opa, tive um probleminha técnico aqui e não consegui consultar o sistema agora. "
    "Você pode tentar de novo em instantes ou falar com a equipe pelo (67) 3341-4444?"
)


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result: str


@dataclass
class AgentReply:
    text: str
    model: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


def _trim(history: list[dict]) -> list[dict]:
    """Mantém as mensagens mais recentes, sempre começando numa mensagem do usuário
    (cortar no meio de uma chamada de ferramenta quebra a conversa para o modelo)."""
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    cut = len(history) - MAX_HISTORY_MESSAGES
    while cut < len(history) and history[cut].get("role") != "user":
        cut += 1
    return history[cut:]


class Agent:
    def __init__(self, store: Store | None = None, llm: LLMClient | None = None, session_id: str | None = None):
        self.store = store or Store()
        self.llm = llm or LLMClient(config.get_settings())
        self.tools = tool_schemas(self.store)
        self.session_id = session_id
        self.history: list[dict] = self._load() if session_id else []

    # --- persistência ---------------------------------------------------
    def _path(self) -> Path:
        return SESSIONS_DIR / f"{self.session_id}.json"

    def _load(self) -> list[dict]:
        path = self._path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def _save(self) -> None:
        if not self.session_id:
            return
        SESSIONS_DIR.mkdir(exist_ok=True)
        self._path().write_text(json.dumps(self.history, ensure_ascii=False, indent=1), encoding="utf-8")

    def reset(self) -> None:
        self.history = []
        self._save()

    # --- conversa -------------------------------------------------------
    def reply(self, user_text: str) -> AgentReply:
        system = {"role": "system", "content": build_system_prompt(self.store, config.now())}
        turn: list[dict] = [{"role": "user", "content": user_text}]
        calls: list[ToolCall] = []
        model = None

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                msg, model = self.llm.chat([system, *_trim(self.history), *turn], self.tools)
                # exclude_none preserva campos extras do provedor (ex.: assinaturas
                # de "pensamento" do Gemini) que precisam voltar na próxima chamada.
                turn.append(msg.model_dump(exclude_none=True))
                if not msg.tool_calls:
                    break
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = run_tool(self.store, tc.function.name, args)
                    calls.append(ToolCall(tc.function.name, args, result))
                    turn.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            else:
                # Estourou o teto de rodadas sem resposta final.
                log.warning("Teto de %d rodadas atingido.", MAX_TOOL_ROUNDS)
                turn.append({"role": "assistant", "content": FALLBACK_REPLY})
        except AllModelsUnavailable as e:
            log.error("Nenhum modelo disponível: %s", e)
            return AgentReply(FALLBACK_REPLY, None, calls)

        text = (turn[-1].get("content") or "").strip() or FALLBACK_REPLY
        self.history.extend(turn)
        self._save()
        return AgentReply(text, model, calls)
