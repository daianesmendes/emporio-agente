"""Carregamento, tipagem e checagem de qualidade dos dados da loja.

Os CSVs em data/raw ficam intocados; toda limpeza acontece aqui, em código,
para que cada decisão seja rastreável.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

# Marcas presentes no catálogo. Usadas para detectar descrições que citam
# uma marca diferente da que está no nome do produto.
KNOWN_BRANDS = [
    "yamaha", "tagima", "giannini", "rozini", "crafter", "martin", "taylor",
    "takamine", "shelby", "kala", "kalani", "ohana", "fender", "gibson",
    "ibanez", "prs", "music man", "pearl", "tama", "korg", "nord", "roland",
]


def normalize(text: str) -> str:
    """Minúsculas e sem acentos — base de toda comparação de texto."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def only_digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def brands_in(text: str) -> set[str]:
    norm = f" {normalize(text)} "
    return {b for b in KNOWN_BRANDS if f" {b} " in norm}


@dataclass
class Product:
    id: int
    name: str
    category_id: int
    category: str
    price: float
    description: str
    stock: int
    status: str  # active | discontinued | coming_soon
    specs: dict
    created_at: str
    description_reliable: bool = True

    @property
    def available(self) -> bool:
        return self.status == "active" and self.stock > 0


@dataclass
class Promotion:
    id: int
    product_id: int
    discount_percent: float
    description: str
    active: bool


@dataclass
class Customer:
    id: int
    name: str
    phone: str
    email: str
    city: str


@dataclass
class OrderItem:
    product_id: int
    quantity: int


@dataclass
class Order:
    id: int
    customer_id: int
    date: str
    status: str  # pending | confirmed | shipped | delivered | cancelled
    total: float
    payment_method: str
    tracking_code: str
    estimated_delivery: str
    notes: str
    items: list[OrderItem] = field(default_factory=list)


@dataclass
class DataIssue:
    kind: str
    detail: str


def _read(name: str, raw_dir: Path) -> list[dict]:
    with open(raw_dir / f"{name}.csv", encoding="utf-8") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


class Store:
    """Fonte única dos dados da operação, já tipados e checados."""

    def __init__(self, raw_dir: Path = RAW_DIR):
        categories = {int(r["category_id"]): r for r in _read("categories", raw_dir)}
        self.categories = {cid: r["name"] for cid, r in categories.items()}

        self.products: dict[int, Product] = {}
        for r in _read("products", raw_dir):
            cid = int(r["category_id"])
            self.products[int(r["product_id"])] = Product(
                id=int(r["product_id"]),
                name=r["name"],
                category_id=cid,
                category=self.categories.get(cid, "Sem categoria"),
                price=float(r["price_brl"]),
                description=r["description"],
                stock=int(r["stock_quantity"] or 0),
                status=r["status"],
                specs=json.loads(r["specs"]) if r["specs"] else {},
                created_at=r["created_at"],
            )

        self.promotions = [
            Promotion(
                id=int(r["promotion_id"]),
                product_id=int(r["product_id"]),
                discount_percent=float(r["discount_percent"]),
                description=r["description"],
                active=r["is_active"] == "1",
            )
            for r in _read("promotions", raw_dir)
        ]

        self.customers = {
            int(r["customer_id"]): Customer(
                id=int(r["customer_id"]),
                name=r["name"],
                phone=r["phone"],
                email=r["email"].lower(),
                city=r["city"],
            )
            for r in _read("customers", raw_dir)
        }

        self.orders: dict[int, Order] = {}
        for r in _read("orders", raw_dir):
            self.orders[int(r["order_id"])] = Order(
                id=int(r["order_id"]),
                customer_id=int(r["customer_id"]),
                date=r["order_date"],
                status=r["status"],
                total=float(r["total_brl"]),
                payment_method=r["payment_method"],
                tracking_code=r["tracking_code"],
                estimated_delivery=r["estimated_delivery"],
                notes=r["notes"],
            )
        for r in _read("order_items", raw_dir):
            self.orders[int(r["order_id"])].items.append(
                OrderItem(product_id=int(r["product_id"]), quantity=int(r["quantity"]))
            )

        self.issues = self._check_quality()

    # ------------------------------------------------------------------
    # Checagens de qualidade
    # ------------------------------------------------------------------
    def _check_quality(self) -> list[DataIssue]:
        issues: list[DataIssue] = []

        for p in self.products.values():
            name_brands = brands_in(p.name)
            other = brands_in(p.description) - name_brands
            if name_brands and other:
                p.description_reliable = False
                issues.append(DataIssue(
                    "descricao_inconsistente",
                    f"Produto {p.id} '{p.name}': descrição cita {sorted(other)}. "
                    "O agente usa nome/preço/specs e não repete a descrição.",
                ))
            if p.status == "active" and p.stock == 0:
                issues.append(DataIssue("ativo_sem_estoque", f"Produto {p.id} '{p.name}' ativo com estoque 0."))

        for cid, name in self.categories.items():
            if not any(p.category_id == cid for p in self.products.values()):
                issues.append(DataIssue("categoria_vazia", f"Categoria {cid} '{name}' não tem nenhum produto."))

        for o in self.orders.values():
            items_total = sum(
                self.products[i.product_id].price * i.quantity
                for i in o.items if i.product_id in self.products
            )
            if abs(items_total - o.total) > 0.01:
                issues.append(DataIssue(
                    "total_divergente",
                    f"Pedido {o.id}: total R$ {o.total:.2f} x soma dos itens a preço atual "
                    f"R$ {items_total:.2f}. O agente informa o total registrado no pedido.",
                ))
            for i in o.items:
                p = self.products.get(i.product_id)
                if p and p.status == "coming_soon":
                    issues.append(DataIssue(
                        "pedido_com_produto_em_breve",
                        f"Pedido {o.id} ({o.status}) contém '{p.name}', marcado como coming_soon.",
                    ))

        for promo in self.promotions:
            if promo.product_id not in self.products:
                issues.append(DataIssue("promocao_orfa", f"Promoção {promo.id} aponta para produto inexistente."))

        return issues

    # ------------------------------------------------------------------
    # Consultas básicas
    # ------------------------------------------------------------------
    def active_promotion(self, product_id: int) -> Promotion | None:
        """Maior desconto ativo do produto (promoções não são cumulativas)."""
        active = [p for p in self.promotions if p.product_id == product_id and p.active]
        return max(active, key=lambda p: p.discount_percent, default=None)

    def expired_promotions(self, product_id: int) -> list[Promotion]:
        return [p for p in self.promotions if p.product_id == product_id and not p.active]
