"""Manual de políticas: extração do PDF e divisão por seção.

O manual tem ~11 mil caracteres. Em vez de um RAG vetorial (chunking +
embeddings), cada seção numerada vira um "tópico" que o agente pede pelo nome.
Isso garante que a seção chega inteira (sem cortar uma tabela ao meio) e
custa zero chamadas extras de embedding.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

from .data import RAW_DIR

PDF_PATH = RAW_DIR / "politicas_da_loja.pdf"

# Número da seção no PDF -> nome do tópico exposto ao agente.
TOPICS = {
    1: "sobre_a_loja_e_contato",
    2: "horario_de_funcionamento",
    3: "formas_de_pagamento",
    4: "trocas_e_devolucoes",
    5: "frete_e_entregas",
    6: "promocoes",
    7: "diretrizes_de_atendimento",
    8: "garantia",
    9: "privacidade_lgpd",
    10: "disposicoes_finais",
}

# Tópicos que o agente pode consultar para responder clientes.
# Diretrizes de atendimento vão direto no prompt de sistema.
CUSTOMER_TOPICS = [t for n, t in TOPICS.items() if n not in (7, 10)]

# Contradições e ambiguidades encontradas no manual, com a decisão tomada.
CURATION_NOTES = {
    "sobre_a_loja_e_contato": (
        "Telefone/WhatsApp oficial: (67) 3341-4444 (consta nos Dados da Empresa e na capa). "
        "O número (67) 3321-4500 aparece só na seção 7 e não deve ser informado. "
        "E-mail: contato@emporiodamusica.com.br (sem acento; a versão com acento é erro de digitação). "
        "O manual fala em 'mais de 300 instrumentos', mas só existe o que está no sistema: "
        "nunca confirme um produto ou categoria sem consultar o catálogo."
    ),
    "formas_de_pagamento": (
        "A tabela resume 'parcela mínima R$ 100' para crédito, mas a seção 3.1 detalha por faixa e prevalece: "
        "até 3x com parcela mínima de R$ 50; 4x a 6x, mínimo R$ 80; 7x a 12x, mínimo R$ 100. "
        "Use a ferramenta calcular_pagamento para valores exatos."
    ),
    "trocas_e_devolucoes": (
        "Use a ferramenta consultar_pedido para saber a data do pedido/entrega e calcular os prazos. "
        "O sistema não guarda a data real de recebimento; usamos a previsão de entrega como referência. "
        "Defeito: até 30 dias da compra, troca direta na loja (4.2). Depois disso, a seção 4.2 manda acionar "
        "o fabricante, mas a seção 8.1 garante 90 dias de garantia legal (CDC) a partir do recebimento: "
        "dentro dos 90 dias, informe que o produto está na garantia legal e encaminhe o caso para a equipe "
        "da loja avaliar; depois dos 90 dias, vale a garantia do fabricante, com intermediação da loja."
    ),
    "garantia": (
        "Para casos de defeito, cruze com consultar_pedido (prazos) e com o tópico trocas_e_devolucoes."
    ),
}


def _clean(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line or re.fullmatch(r"Página \d+", line):
            continue
        if line.startswith("Empório da Música Manual de Políticas"):
            continue
        lines.append(line)
    return "\n".join(lines)


@lru_cache(maxsize=1)
def load_sections(pdf_path: Path = PDF_PATH) -> dict[str, str]:
    reader = PdfReader(str(pdf_path))
    # A página 1 é a capa; o conteúdo começa na página 2.
    body = _clean("\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages[1:]))
    # Remove o rodapé final ("Empório da Música — Sua música começa aqui.")
    body = body.split("Empório da Música — Sua música começa aqui.")[0]

    # Só aceita "N. Título" como seção se N for o próximo número esperado:
    # assim listas numeradas dentro de uma seção (ex.: 7.2) não viram seções.
    headings = []
    expected = 1
    for m in re.finditer(r"^(\d{1,2})\. (.+)$", body, flags=re.MULTILINE):
        if int(m.group(1)) == expected:
            headings.append(m)
            expected += 1

    sections: dict[str, str] = {}
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        num = int(m.group(1))
        topic = TOPICS.get(num)
        if topic:
            content = body[m.end():end].strip()
            sections[topic] = f"## {num}. {m.group(2).strip()}\n{content}"
    return sections


def get_policy(topic: str) -> str:
    sections = load_sections()
    if topic not in sections:
        return f"Tópico desconhecido. Opções: {', '.join(CUSTOMER_TOPICS)}"
    text = sections[topic]
    if topic in CURATION_NOTES:
        text += f"\n\n[Nota da curadoria] {CURATION_NOTES[topic]}"
    return text
