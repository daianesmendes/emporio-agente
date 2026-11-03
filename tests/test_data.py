import pytest

from emporio.data import Store, normalize


@pytest.fixture(scope="module")
def store():
    return Store()


def test_carrega_todas_as_tabelas(store):
    assert len(store.products) == 65
    assert len(store.orders) == 20
    assert len(store.customers) == 50
    assert sum(p.active for p in store.promotions) == 4


def test_pedido_com_dois_itens(store):
    assert {i.product_id for i in store.orders[15].items} == {82, 106}


def test_normalize_remove_acentos():
    assert normalize("  Violão  Elétrico ") == "violao eletrico"


def test_descricao_com_marca_trocada_e_marcada(store):
    assert store.products[135].description_reliable is False  # "Music Man" descrito como Fender
    assert store.products[95].description_reliable is True


def test_problemas_de_qualidade_detectados(store):
    kinds = [i.kind for i in store.issues]
    assert kinds.count("categoria_vazia") == 3
    assert kinds.count("total_divergente") == 2
    assert "ativo_sem_estoque" in kinds


def test_promocao_ativa_e_expirada(store):
    assert store.active_promotion(127).discount_percent == 10
    assert store.active_promotion(96) is None  # só tem promoções vencidas
    assert len(store.expired_promotions(96)) == 2
