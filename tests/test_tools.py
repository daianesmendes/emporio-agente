import json

import pytest

from emporio import tools as T
from emporio.data import Store
from emporio.policies import CUSTOMER_TOPICS, load_sections


@pytest.fixture(scope="module")
def store():
    return Store()


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setenv("AGENT_NOW", "2026-03-25 14:00")


# --- Catálogo -----------------------------------------------------------
def test_violoes_ate_mil_reais(store):
    r = T.buscar_produtos(store, termo="violões", preco_max=1000)
    assert r["filtros_aplicados"]["categoria"] == "Violões"
    assert r["total_disponiveis"] > 0
    assert all(p["categoria"] == "Violões" for p in r["produtos"])


def test_busca_ignora_acento_e_caixa(store):
    a = T.buscar_produtos(store, termo="VIOLAO takamine")
    b = T.buscar_produtos(store, termo="violão Takamine")
    assert [p["id"] for p in a["produtos"]] == [p["id"] for p in b["produtos"]]


def test_preco_do_takamine_gd20(store):
    r = T.detalhes_produto(store, "Takamine GD20")
    assert r["id"] == 95
    assert r["preco_tabela"] == "R$ 2.199,00"
    assert "promocoes_encerradas" in r  # promoção vencida não pode ser prometida


def test_promocao_ativa_nao_acumula_com_pix(store):
    r = T.detalhes_produto(store, "127")
    assert r["promocao_ativa"]["desconto"] == "10%"
    assert "preco_pix" not in r


def test_esgotado_sugere_alternativas(store):
    r = T.detalhes_produto(store, "Giannini GF-3D")
    assert r["disponibilidade"] == "esgotado no momento"
    assert len(r["alternativas_disponiveis"]) == 3


def test_categoria_vazia_nao_inventa(store):
    assert T.buscar_produtos(store, termo="saxofone")["resultado"] == "categoria_sem_produtos"


def test_descricao_inconsistente_nao_e_exposta(store):
    r = T.detalhes_produto(store, "135")
    assert "descricao" not in r and "aviso_interno" in r


# --- Pedidos e LGPD -------------------------------------------------------
def test_pedido_exige_contato_do_dono(store):
    assert T.consultar_pedido(store, "lucas.mendes@jmail.com", 1)["resultado"] == "nao_encontrado"
    ok = T.consultar_pedido(store, "pedro.oliveira@jmail.com", 1)
    assert ok["pedidos"][0]["numero_pedido"] == 1


def test_pedido_por_telefone_em_qualquer_formato(store):
    a = T.consultar_pedido(store, "(67) 98432-1098")
    b = T.consultar_pedido(store, "67984321098")
    assert a == b and len(a["pedidos"]) == 2


def test_prazos_de_devolucao(store):
    r = T.consultar_pedido(store, "pedro.oliveira@jmail.com", 1)["pedidos"][0]
    assert r["prazos"]["arrependimento_7_dias_apos_recebimento"] == "prazo encerrado"


def test_pedido_enviado_atrasado(store):
    r = T.consultar_pedido(store, "anacarol.ferreira@coldmail.com", 8)["pedidos"][0]
    assert "atrasada" in r["prazos"]["alerta"]


# --- Pagamento ------------------------------------------------------------
def test_parcelamento_respeita_parcela_minima(store):
    r = T.calcular_pagamento(store, valor=159.9)
    assert r["credito_sem_juros"]["maximo_de_parcelas"].startswith("3x")
    r = T.calcular_pagamento(store, valor=2199)
    assert r["credito_sem_juros"]["maximo_de_parcelas"].startswith("12x")


def test_pix_com_desconto(store):
    assert T.calcular_pagamento(store, valor=1000)["pix"].startswith("R$ 950,00")


# --- Políticas e infraestrutura ------------------------------------------
def test_manual_dividido_em_todas_as_secoes():
    sections = load_sections()
    assert len(sections) == 10
    assert "Fluxo de Atendimento" in sections["diretrizes_de_atendimento"]
    assert all(t in sections for t in CUSTOMER_TOPICS)


def test_run_tool_nunca_lanca_excecao(store):
    out = json.loads(T.run_tool(store, "buscar_produtos", {"argumento_errado": 1}))
    assert "erro" in out
    assert "erro" in json.loads(T.run_tool(store, "nao_existe", {}))


def test_schemas_validos(store):
    names = {s["function"]["name"] for s in T.tool_schemas(store)}
    assert names == set(T.TOOL_FUNCTIONS)


def test_card_de_produto_traz_parcelamento_calculado(store):
    # Regressão: o modelo chegou a inventar "10x de R$ 79,90" para este produto,
    # mas de 7x a 12x a parcela mínima é R$ 100 (máximo possível: 7x).
    card = T.detalhes_produto(store, "Shelby SGD-195E")
    assert card["parcelamento_maximo"] == "7x de R$ 114,14 sem juros"  # 8x daria R$ 99,88


def test_encaminhamento_novo_e_atualizacao(store, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "HANDOFF_FILE", tmp_path / "casos.jsonl")
    novo = T.encaminhar_para_humano(store, "garantia", "traste solto", "Letícia")
    assert novo["protocolo"] == "EM-20260325-0001"
    upd = T.encaminhar_para_humano(store, "garantia", "telefone informado", "(67) 99789-0123",
                                   protocolo=novo["protocolo"])
    assert upd["protocolo"] == novo["protocolo"] and "atualizado" in upd["mensagem"]
    # Atualização não consome número de protocolo; protocolo inventado é recusado.
    assert T.encaminhar_para_humano(store, "outro", "x")["protocolo"] == "EM-20260325-0002"
    assert "erro" in T.encaminhar_para_humano(store, "outro", "x", protocolo="EM-1")
