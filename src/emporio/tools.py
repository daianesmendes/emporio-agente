"""Ferramentas que o agente pode chamar (function calling).

Regra de ouro: o LLM nunca calcula nem "lembra" preço, estoque ou prazo.
Tudo que é fato da operação sai daqui, em Python, a partir dos dados.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from . import config
from .data import Order, Product, Store, normalize, only_digits
from .policies import CUSTOMER_TOPICS, get_policy

PIX_DISCOUNT = 0.05
HANDOFF_FILE = Path(__file__).resolve().parents[2] / ".sessions" / "atendimentos_humanos.jsonl"

# Palavras do cliente -> categoria do catálogo.
CATEGORY_SYNONYMS = {
    "violao": 5, "violoes": 5,
    "guitarra": 1, "guitarras": 1,
    "baixo": 2, "baixos": 2, "contrabaixo": 2, "contrabaixos": 2,
    "bateria": 3, "baterias": 3, "percussao": 3,
    "teclado": 4, "teclados": 4, "piano": 4, "pianos": 4, "sintetizador": 4, "synth": 4,
    "ukulele": 9, "ukuleles": 9, "uke": 9,
    "flauta": 6, "clarinete": 6, "saxofone": 6, "sax": 6, "oboe": 6,
    "trompete": 7, "trombone": 7, "tuba": 7, "trompa": 7,
    "violino": 8, "violinos": 8, "viola": 8, "violoncelo": 8, "cello": 8,
}

STOPWORDS = {
    "de", "da", "do", "das", "dos", "e", "o", "a", "os", "as", "um", "uma", "para", "pra",
    "com", "sem", "em", "no", "na", "que", "tem", "quero", "voces", "vcs", "algum", "alguma",
}

ORDER_STATUS = {
    "pending": "Pendente — aguardando confirmação do pagamento",
    "confirmed": "Confirmado — pagamento aprovado, pedido em separação",
    "shipped": "Enviado — a caminho",
    "delivered": "Entregue",
    "cancelled": "Cancelado",
}


def _payment_label(method: str) -> str:
    if method.startswith("credit_"):
        return f"Cartão de crédito em {method.split('_')[1]}"
    return {"pix": "PIX", "debit": "Cartão de débito", "boleto": "Boleto bancário"}.get(method, method)


def _brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ----------------------------------------------------------------------
# Representação de produto
# ----------------------------------------------------------------------
def _availability(p: Product) -> str:
    if p.status == "discontinued":
        return "descontinuado (não faz mais parte do catálogo)"
    if p.status == "coming_soon":
        return "em breve (ainda não disponível para venda)"
    if p.stock == 0:
        return "esgotado no momento"
    if p.stock <= 3:
        return f"disponível — últimas {p.stock} unidades"
    return "disponível"


def _price_info(store: Store, p: Product) -> dict:
    promo = store.active_promotion(p.id)
    info = {"preco_tabela": _brl(p.price)}
    if promo:
        promo_price = p.price * (1 - promo.discount_percent / 100)
        info["promocao_ativa"] = {
            "campanha": promo.description,
            "desconto": f"{promo.discount_percent:g}%",
            "preco_promocional": _brl(promo_price),
            "observacao": "Desconto PIX não acumula com promoção.",
        }
    else:
        info["preco_pix"] = _brl(p.price * (1 - PIX_DISCOUNT)) + " (5% off no PIX)"
    # Parcelamento já calculado: o modelo não precisa (nem deve) fazer a conta.
    final = _final_price(store, p)
    n = _max_installments(final)
    info["parcelamento_maximo"] = f"{n}x de {_brl(final / n)} sem juros" if n > 1 else "somente à vista"
    return info


def _final_price(store: Store, p: Product) -> float:
    promo = store.active_promotion(p.id)
    return p.price * (1 - promo.discount_percent / 100) if promo else p.price


def _product_card(store: Store, p: Product, detailed: bool = False) -> dict:
    card = {
        "id": p.id,
        "nome": p.name,
        "categoria": p.category,
        "disponibilidade": _availability(p),
        **_price_info(store, p),
    }
    if detailed:
        card["especificacoes"] = p.specs
        if p.description_reliable:
            card["descricao"] = p.description
        else:
            card["aviso_interno"] = (
                "Descrição do cadastro está inconsistente com o nome (cita outra marca). "
                "Não use a descrição; responda só com nome, preço e especificações."
            )
        expired = store.expired_promotions(p.id)
        if expired and not store.active_promotion(p.id):
            card["promocoes_encerradas"] = [f"{e.description} ({e.discount_percent:g}%)" for e in expired]
    return card


# ----------------------------------------------------------------------
# Busca
# ----------------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    return [t for t in normalize(text).replace("-", " ").split() if t not in STOPWORDS]


def _haystack(p: Product) -> str:
    parts = [p.name, p.category, " ".join(str(v) for v in p.specs.values())]
    if p.description_reliable:
        parts.append(p.description)
    return normalize(" ".join(parts)).replace("-", " ")


def _score(p: Product, tokens: list[str]) -> float:
    if not tokens:
        return 1.0
    hay = _haystack(p)
    name = normalize(p.name).replace("-", " ")
    score = 0.0
    for t in tokens:
        if t in name.split():
            score += 2  # palavra exata no nome vale mais
        elif t in hay:
            score += 1
    # Bônus para nomes de modelo digitados "colados" ou com erro leve (ex.: "gd 20")
    score += SequenceMatcher(None, " ".join(tokens), name).ratio()
    return score


def _resolve_category(categoria: str | None, store: Store) -> int | None:
    if not categoria:
        return None
    norm = normalize(categoria)
    for cid, name in store.categories.items():
        if normalize(name) == norm:
            return cid
    return CATEGORY_SYNONYMS.get(norm)


def buscar_produtos(
    store: Store,
    termo: str | None = None,
    categoria: str | None = None,
    preco_max: float | None = None,
    preco_min: float | None = None,
    limite: int = 8,
) -> dict:
    tokens = _tokens(termo or "")
    cat_id = _resolve_category(categoria, store)
    # Palavras como "violão" no termo também viram filtro de categoria.
    for t in list(tokens):
        if cat_id is None and t in CATEGORY_SYNONYMS:
            cat_id = CATEGORY_SYNONYMS[t]
        if t in CATEGORY_SYNONYMS:
            tokens.remove(t)

    if cat_id is not None and not any(p.category_id == cat_id for p in store.products.values()):
        return {
            "resultado": "categoria_sem_produtos",
            "categoria": store.categories.get(cat_id),
            "mensagem": "Não há nenhum produto desta categoria no sistema no momento. "
                        "Não sugira modelos específicos desta categoria.",
        }

    candidates = [p for p in store.products.values() if cat_id is None or p.category_id == cat_id]
    if preco_max is not None:
        candidates = [p for p in candidates if _final_price(store, p) <= preco_max]
    if preco_min is not None:
        candidates = [p for p in candidates if _final_price(store, p) >= preco_min]

    scored = [(p, _score(p, tokens)) for p in candidates]
    if tokens:
        scored = [(p, s) for p, s in scored if s >= 1]
    scored.sort(key=lambda x: (-x[1], _final_price(store, x[0])))

    available = [p for p, _ in scored if p.available]
    unavailable = [p for p, _ in scored if not p.available]

    result = {
        "filtros_aplicados": {
            "termo": termo, "categoria": store.categories.get(cat_id) if cat_id else None,
            "preco_min": preco_min, "preco_max": preco_max,
        },
        "total_disponiveis": len(available),
        "produtos": [_product_card(store, p) for p in available[:limite]],
    }
    # Produtos indisponíveis só aparecem quando o cliente buscou algo específico,
    # para o agente poder dizer "esse está esgotado, mas temos...".
    if tokens and unavailable:
        result["indisponiveis_correspondentes"] = [_product_card(store, p) for p in unavailable[:3]]
    if not available and not unavailable:
        result["mensagem"] = "Nenhum produto encontrado com esses critérios."
    return result


def _alternatives(store: Store, p: Product, n: int = 3) -> list[dict]:
    pool = [
        q for q in store.products.values()
        if q.id != p.id and q.category_id == p.category_id and q.available
    ]
    pool.sort(key=lambda q: abs(_final_price(store, q) - _final_price(store, p)))
    return [_product_card(store, q) for q in pool[:n]]


def detalhes_produto(store: Store, produto: str) -> dict:
    if str(produto).isdigit() and int(produto) in store.products:
        matches = [store.products[int(produto)]]
    else:
        tokens = _tokens(produto)
        scored = sorted(
            ((p, _score(p, tokens)) for p in store.products.values()),
            key=lambda x: -x[1],
        )
        best = scored[0][1] if scored else 0
        if best < 2:
            return {"resultado": "nao_encontrado", "mensagem": f"Nenhum produto corresponde a '{produto}'."}
        # Empate próximo = ambíguo: devolve opções para o cliente escolher.
        matches = [p for p, s in scored if s >= best - 0.3][:4]

    if len(matches) > 1:
        return {
            "resultado": "ambiguo",
            "mensagem": "Mais de um produto corresponde. Pergunte ao cliente qual deles.",
            "opcoes": [_product_card(store, p) for p in matches],
        }

    p = matches[0]
    card = _product_card(store, p, detailed=True)
    if not p.available:
        card["alternativas_disponiveis"] = _alternatives(store, p)
    return card


# ----------------------------------------------------------------------
# Pedidos (com verificação de identidade)
# ----------------------------------------------------------------------
def _find_customer(store: Store, contato: str):
    contato = (contato or "").strip().lower()
    digits = only_digits(contato)
    for c in store.customers.values():
        if "@" in contato and c.email == contato:
            return c
        if len(digits) >= 8 and only_digits(c.phone).endswith(digits[-8:]):
            return c
    return None


def _days(a: str, b: date) -> int:
    return (b - datetime.strptime(a, "%Y-%m-%d").date()).days


def _deadlines(o: Order, today: date) -> dict:
    info: dict = {"dias_desde_a_compra": _days(o.date, today)}
    if o.status == "delivered" and o.estimated_delivery:
        since = _days(o.estimated_delivery, today)
        info["dias_desde_a_entrega_estimada"] = since
        info["arrependimento_7_dias_apos_recebimento"] = "dentro do prazo" if since <= 7 else "prazo encerrado"
        info["garantia_legal_90_dias_apos_recebimento"] = "dentro do prazo" if since <= 90 else "prazo encerrado"
    info["troca_por_defeito_30_dias_apos_compra"] = (
        "dentro do prazo" if info["dias_desde_a_compra"] <= 30 else "prazo encerrado (acionar garantia)"
    )
    if o.status == "shipped" and o.estimated_delivery and _days(o.estimated_delivery, today) > 0:
        info["alerta"] = f"Entrega atrasada: previsão era {o.estimated_delivery}."
    if o.status == "pending" and info["dias_desde_a_compra"] > 3:
        info["alerta"] = "Pagamento pendente há mais de 3 dias (boleto compensa em até 3 dias úteis)."
    info["referencia"] = "Data de recebimento real não consta no sistema; usada a previsão de entrega."
    return info


def _order_view(store: Store, o: Order, today: date) -> dict:
    items = []
    for i in o.items:
        p = store.products.get(i.product_id)
        items.append({"produto": p.name if p else f"Produto {i.product_id}", "quantidade": i.quantity})
    view = {
        "numero_pedido": o.id,
        "data_do_pedido": o.date,
        "status": ORDER_STATUS.get(o.status, o.status),
        "itens": items,
        "valor_total": _brl(o.total),
        "forma_de_pagamento": _payment_label(o.payment_method),
    }
    if o.tracking_code:
        view["codigo_rastreio"] = o.tracking_code
    if o.estimated_delivery:
        view["previsao_de_entrega"] = o.estimated_delivery
    if o.notes:
        view["observacao"] = o.notes
    if o.status != "cancelled":
        view["prazos"] = _deadlines(o, today)
    return view


def consultar_pedido(store: Store, email_ou_telefone: str, numero_pedido: int | None = None) -> dict:
    customer = _find_customer(store, email_ou_telefone)
    # Mesma resposta para "contato errado" e "pedido de outra pessoa":
    # não revelamos se um pedido ou cliente existe (LGPD).
    not_found = {
        "resultado": "nao_encontrado",
        "mensagem": "Não localizamos pedido com esses dados. Peça para o cliente conferir "
                    "o número do pedido e o e-mail ou telefone usado na compra.",
    }
    if not customer:
        return not_found

    today = config.now().date()
    orders = [o for o in store.orders.values() if o.customer_id == customer.id]
    if numero_pedido is not None:
        orders = [o for o in orders if o.id == int(numero_pedido)]
        if not orders:
            return not_found
    return {
        "cliente": customer.name.split()[0],
        "cidade": customer.city,
        "pedidos": [_order_view(store, o, today) for o in sorted(orders, key=lambda o: o.date, reverse=True)],
    }


# ----------------------------------------------------------------------
# Pagamento
# ----------------------------------------------------------------------
def _min_installment(n: int) -> float:
    if n <= 3:
        return 50.0
    if n <= 6:
        return 80.0
    return 100.0


def _max_installments(valor: float) -> int:
    """Maior número de parcelas sem juros que respeita a parcela mínima da faixa."""
    max_n = 1
    for n in range(2, 13):
        if valor / n >= _min_installment(n):
            max_n = n
    return max_n


def calcular_pagamento(store: Store, valor: float | None = None, produto_id: int | None = None) -> dict:
    promotional = False
    if produto_id is not None:
        p = store.products.get(int(produto_id))
        if not p:
            return {"erro": f"Produto {produto_id} não encontrado."}
        promotional = store.active_promotion(p.id) is not None
        valor = _final_price(store, p)
    if not valor or valor <= 0:
        return {"erro": "Informe um valor ou um produto."}

    max_n = _max_installments(valor)
    options = {f"{n}x": _brl(valor / n) for n in sorted({1, 2, 3, 6, 10, 12, max_n}) if n <= max_n}

    return {
        "valor_base": _brl(valor) + (" (já com promoção)" if promotional else ""),
        "pix": _brl(valor) + " (sem desconto adicional: promoção não acumula)"
        if promotional else _brl(valor * (1 - PIX_DISCOUNT)) + " (5% de desconto)",
        "debito": _brl(valor),
        "boleto": _brl(valor) + " (compensação em até 3 dias úteis)",
        "credito_sem_juros": {"maximo_de_parcelas": f"{max_n}x de {_brl(valor / max_n)}", "exemplos": options},
        "combinar_formas_de_pagamento": "permitido" if valor > 2000 else "não permitido (só acima de R$ 2.000)",
    }


# ----------------------------------------------------------------------
# Políticas e atendimento humano
# ----------------------------------------------------------------------
def consultar_politicas(store: Store, topico: str) -> dict:
    return {"topico": topico, "conteudo": get_policy(topico)}


def encaminhar_para_humano(store: Store, motivo: str, resumo: str, contato: str | None = None) -> dict:
    now = config.now()
    HANDOFF_FILE.parent.mkdir(exist_ok=True)
    seq = sum(1 for _ in open(HANDOFF_FILE, encoding="utf-8")) + 1 if HANDOFF_FILE.exists() else 1
    protocol = f"EM-{now:%Y%m%d}-{seq:04d}"
    with open(HANDOFF_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "protocolo": protocol, "registrado_em": now.isoformat(timespec="minutes"),
            "motivo": motivo, "resumo": resumo, "contato": contato,
        }, ensure_ascii=False) + "\n")
    return {
        "protocolo": protocol,
        "prazo_de_retorno": "até 24 horas úteis",
        "mensagem": "Registrado e encaminhado para a equipe responsável.",
    }


# ----------------------------------------------------------------------
# Esquemas expostos ao LLM
# ----------------------------------------------------------------------
TOOL_FUNCTIONS = {
    "buscar_produtos": buscar_produtos,
    "detalhes_produto": detalhes_produto,
    "consultar_pedido": consultar_pedido,
    "calcular_pagamento": calcular_pagamento,
    "consultar_politicas": consultar_politicas,
    "encaminhar_para_humano": encaminhar_para_humano,
}


def tool_schemas(store: Store) -> list[dict]:
    categories = list(store.categories.values())

    def fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        }}

    return [
        fn("buscar_produtos",
           "Busca instrumentos no catálogo com preço, promoção e disponibilidade atuais. "
           "Use para perguntas abertas: 'quais violões até R$1000', 'tem guitarra Fender?', 'ukulele para iniciante'.",
           {
               "termo": {"type": "string", "description": "Palavras-chave: marca, modelo, tipo, característica."},
               "categoria": {"type": "string", "enum": categories},
               "preco_max": {"type": "number", "description": "Preço máximo em reais."},
               "preco_min": {"type": "number", "description": "Preço mínimo em reais."},
           }),
        fn("detalhes_produto",
           "Ficha completa de UM produto específico (preço, promoção, especificações, disponibilidade e "
           "alternativas se indisponível). Use quando o cliente cita um modelo: 'quanto custa o Takamine GD20?'.",
           {"produto": {"type": "string", "description": "Nome/modelo do produto ou seu id."}},
           ["produto"]),
        fn("consultar_pedido",
           "Consulta status, itens, rastreio e prazos de troca/devolução de pedidos. "
           "EXIGE o e-mail ou telefone usado na compra: peça ao cliente antes de chamar. "
           "Sem número do pedido, lista os pedidos do cliente.",
           {
               "email_ou_telefone": {"type": "string"},
               "numero_pedido": {"type": "integer"},
           },
           ["email_ou_telefone"]),
        fn("calcular_pagamento",
           "Calcula opções de pagamento (PIX com desconto, débito, boleto, parcelamento sem juros) "
           "respeitando promoções e parcelas mínimas. Prefira passar produto_id.",
           {
               "produto_id": {"type": "integer"},
               "valor": {"type": "number", "description": "Valor total em reais, se não houver produto_id."},
           }),
        fn("consultar_politicas",
           "Retorna uma seção do manual oficial da loja. Use para regras: trocas, devoluções, "
           "frete, garantia, pagamento, promoções, privacidade.",
           {"topico": {"type": "string", "enum": CUSTOMER_TOPICS}},
           ["topico"]),
        fn("encaminhar_para_humano",
           "Registra o caso para a equipe humana e gera protocolo. Use para reclamações, pedidos de "
           "cancelamento/devolução a efetivar, garantia, ou quando o cliente pedir um atendente.",
           {
               "motivo": {"type": "string", "enum": ["reclamacao", "devolucao_ou_troca", "cancelamento",
                                                      "garantia", "pedido_atrasado", "outro"]},
               "resumo": {"type": "string", "description": "Resumo objetivo do caso para a equipe."},
               "contato": {"type": "string", "description": "Nome e telefone/e-mail do cliente, se informado."},
           },
           ["motivo", "resumo"]),
    ]


def run_tool(store: Store, name: str, arguments: dict) -> str:
    """Executa a ferramenta e devolve JSON. Erros viram mensagem para o LLM, não exceção."""
    func = TOOL_FUNCTIONS.get(name)
    if not func:
        return json.dumps({"erro": f"Ferramenta desconhecida: {name}"})
    try:
        return json.dumps(func(store, **arguments), ensure_ascii=False)
    except TypeError as e:
        return json.dumps({"erro": f"Argumentos inválidos para {name}: {e}"}, ensure_ascii=False)
