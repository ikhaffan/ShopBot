"""Application database (products, orders, memories, chat log).

Uses SQLAlchemy Core so the exact same code runs on Postgres (Neon / Supabase) in
production and on a local SQLite file in development.
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, UniqueConstraint,
    case, create_engine, delete, func, insert, select, update,
)

from . import config

metadata = MetaData()


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


products = Table(
    "products", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(200), nullable=False),
    Column("description", Text, default=""),
    Column("category", String(100), default=""),
    Column("price_cents", Integer, nullable=False),
    Column("stock", Integer, nullable=False, default=0),
    Column("image_url", Text, default=""),
    Column("active", Boolean, default=True),
)

orders = Table(
    "orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(100), nullable=False, index=True),
    Column("items", Text, nullable=False),  # JSON: [{product_id, name, qty, unit_price_cents}]
    Column("total_cents", Integer, nullable=False),
    Column("status", String(30), nullable=False, default="placed"),
    Column("created_at", DateTime, default=now),
)

pending_actions = Table(
    "pending_actions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(100), nullable=False, index=True),
    Column("kind", String(30), nullable=False),  # place_order | cancel_order
    Column("payload", Text, nullable=False),
    Column("run_id", String(64), nullable=False),  # the /chat request that created it
    Column("created_at", DateTime, default=now),
)

user_memories = Table(
    "user_memories", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(100), nullable=False, index=True),
    Column("fact", String(300), nullable=False),
    Column("created_at", DateTime, default=now),
    UniqueConstraint("user_id", "fact", name="uq_memory_user_fact"),
)

messages = Table(
    "messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(100), nullable=False, index=True),
    Column("thread_id", String(100), nullable=False, index=True),
    Column("role", String(20), nullable=False),
    Column("content", Text, nullable=False),
    Column("tool_calls", Text, default="[]"),
    Column("tokens_in", Integer, default=0),
    Column("tokens_out", Integer, default=0),
    Column("blocked", String(30), default=""),  # '', off_topic, injection
    Column("created_at", DateTime, default=now),
)

_engine = None
_BLOCKED = case((messages.c.blocked != "", 1), else_=0)


def get_engine():
    global _engine
    if _engine is None:
        url = config.sqlalchemy_url()
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=300)
        _engine = create_engine(url, **kwargs)
    return _engine


def init_db():
    metadata.create_all(get_engine())
    with get_engine().begin() as c:
        n = c.execute(select(func.count()).select_from(products)).scalar()
        if n == 0:
            from .seed import SEED_PRODUCTS
            c.execute(insert(products), SEED_PRODUCTS)


def _row(r):
    return dict(r._mapping)


# ---------------------------------------------------------------- products
def list_products(query: str = "", category: str = "", include_inactive=False, limit=50):
    stmt = select(products)
    if not include_inactive:
        stmt = stmt.where(products.c.active.is_(True))
    if category:
        stmt = stmt.where(func.lower(products.c.category) == category.lower())
    with get_engine().begin() as c:
        rows = [_row(r) for r in c.execute(stmt.order_by(products.c.id))]
    if query:
        words = [w for w in query.lower().replace(",", " ").split() if len(w) > 1]

        def score(p):
            hay = f"{p['name']} {p['description']} {p['category']}".lower()
            return sum(1 for w in words if w in hay)

        scored = [(score(p), p) for p in rows]
        rows = [p for s, p in sorted(scored, key=lambda t: -t[0]) if s > 0]
    return rows[:limit]


def get_product(pid: int):
    with get_engine().begin() as c:
        r = c.execute(select(products).where(products.c.id == pid)).first()
    return _row(r) if r else None


def create_product(data: dict):
    with get_engine().begin() as c:
        return c.execute(insert(products).values(**data)).inserted_primary_key[0]


def update_product(pid: int, data: dict):
    with get_engine().begin() as c:
        c.execute(update(products).where(products.c.id == pid).values(**data))


def delete_product(pid: int):
    # soft delete keeps old orders readable
    with get_engine().begin() as c:
        c.execute(update(products).where(products.c.id == pid).values(active=False))


# ---------------------------------------------------------------- orders
def create_order(user_id: str, lines: list[dict]):
    """lines: [{product_id, qty}]. Atomically checks + decrements stock. Returns (order|None, error|None)."""
    with get_engine().begin() as c:
        items, total = [], 0
        for ln in lines:
            p = c.execute(select(products).where(products.c.id == ln["product_id"])).first()
            if not p or not p.active:
                return None, f"Product {ln['product_id']} is not available."
            qty = int(ln["qty"])
            if qty < 1 or qty > 20:
                return None, "Quantity must be between 1 and 20."
            res = c.execute(
                update(products).where(products.c.id == p.id, products.c.stock >= qty)
                .values(stock=products.c.stock - qty))
            if res.rowcount != 1:
                return None, f"Only {p.stock} of '{p.name}' left in stock."
            items.append({"product_id": p.id, "name": p.name, "qty": qty, "unit_price_cents": p.price_cents})
            total += qty * p.price_cents
        oid = c.execute(insert(orders).values(
            user_id=user_id, items=json.dumps(items), total_cents=total, status="placed", created_at=now()
        )).inserted_primary_key[0]
    return get_order(oid), None


def _order(r):
    d = _row(r)
    d["items"] = json.loads(d["items"])
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    return d


def get_order(oid: int, user_id: str | None = None):
    stmt = select(orders).where(orders.c.id == oid)
    if user_id is not None:
        stmt = stmt.where(orders.c.user_id == user_id)
    with get_engine().begin() as c:
        r = c.execute(stmt).first()
    return _order(r) if r else None


def list_orders(user_id: str | None = None, limit=100):
    stmt = select(orders).order_by(orders.c.id.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(orders.c.user_id == user_id)
    with get_engine().begin() as c:
        return [_order(r) for r in c.execute(stmt)]


def set_order_status(oid: int, status: str, user_id: str | None = None):
    """Cancelling restores stock. Returns the updated order or None."""
    order = get_order(oid, user_id)
    if not order:
        return None
    with get_engine().begin() as c:
        if status == "cancelled" and order["status"] != "cancelled":
            for it in order["items"]:
                c.execute(update(products).where(products.c.id == it["product_id"])
                          .values(stock=products.c.stock + it["qty"]))
        c.execute(update(orders).where(orders.c.id == oid).values(status=status))
    return get_order(oid)


# ---------------------------------------------------------------- pending (risky) actions
def set_pending(user_id: str, kind: str, payload: dict, run_id: str):
    with get_engine().begin() as c:
        c.execute(delete(pending_actions).where(pending_actions.c.user_id == user_id))
        c.execute(insert(pending_actions).values(
            user_id=user_id, kind=kind, payload=json.dumps(payload), run_id=run_id, created_at=now()))


def get_pending(user_id: str):
    with get_engine().begin() as c:
        r = c.execute(select(pending_actions).where(pending_actions.c.user_id == user_id)
                      .order_by(pending_actions.c.id.desc())).first()
    if not r:
        return None
    d = _row(r)
    d["payload"] = json.loads(d["payload"])
    return d


def clear_pending(user_id: str):
    with get_engine().begin() as c:
        c.execute(delete(pending_actions).where(pending_actions.c.user_id == user_id))


# ---------------------------------------------------------------- long-term memory
def get_memories(user_id: str, limit=30) -> list[str]:
    with get_engine().begin() as c:
        rows = c.execute(select(user_memories.c.fact).where(user_memories.c.user_id == user_id)
                         .order_by(user_memories.c.id.desc()).limit(limit)).all()
    return [r[0] for r in rows][::-1]


def add_memory(user_id: str, fact: str) -> bool:
    fact = fact.strip()[:300]
    if not fact:
        return False
    with get_engine().begin() as c:
        exists = c.execute(select(user_memories.c.id).where(
            user_memories.c.user_id == user_id, func.lower(user_memories.c.fact) == fact.lower())).first()
        if exists:
            return False
        c.execute(insert(user_memories).values(user_id=user_id, fact=fact, created_at=now()))
    return True


def delete_memories(user_id: str):
    with get_engine().begin() as c:
        c.execute(delete(user_memories).where(user_memories.c.user_id == user_id))


# ---------------------------------------------------------------- chat log / usage
def log_message(user_id, thread_id, role, content, tool_calls=None, tin=0, tout=0, blocked=""):
    with get_engine().begin() as c:
        c.execute(insert(messages).values(
            user_id=user_id, thread_id=thread_id, role=role, content=content,
            tool_calls=json.dumps(tool_calls or [], default=str), tokens_in=tin, tokens_out=tout,
            blocked=blocked, created_at=now()))


def tokens_used_today(user_id: str) -> int:
    since = now() - timedelta(hours=24)
    with get_engine().begin() as c:
        r = c.execute(select(func.coalesce(func.sum(messages.c.tokens_in + messages.c.tokens_out), 0))
                      .where(messages.c.user_id == user_id, messages.c.created_at >= since)).scalar()
    return int(r or 0)


def _msg(r):
    d = _row(r)
    d["tool_calls"] = json.loads(d["tool_calls"] or "[]")
    d["created_at"] = d["created_at"].isoformat()
    return d


def thread_history(user_id: str, thread_id: str, limit=200):
    with get_engine().begin() as c:
        rows = c.execute(select(messages).where(messages.c.user_id == user_id, messages.c.thread_id == thread_id)
                         .order_by(messages.c.id).limit(limit)).all()
    return [_msg(r) for r in rows]


def admin_conversations(limit=60):
    with get_engine().begin() as c:
        rows = c.execute(
            select(messages.c.user_id, messages.c.thread_id,
                   func.count().label("n"), func.max(messages.c.created_at).label("last"),
                   func.sum(messages.c.tokens_in).label("tin"), func.sum(messages.c.tokens_out).label("tout"),
                   func.sum(_BLOCKED).label("blocked"))
            .group_by(messages.c.user_id, messages.c.thread_id)
            .order_by(func.max(messages.c.created_at).desc()).limit(limit)).all()
    return [{"user_id": r.user_id, "thread_id": r.thread_id, "messages": r.n,
             "last": r.last.isoformat() if r.last else None,
             "tokens_in": int(r.tin or 0), "tokens_out": int(r.tout or 0), "blocked": int(r.blocked or 0)}
            for r in rows]


def admin_stats():
    with get_engine().begin() as c:
        tin, tout, n_replies, blocked = c.execute(select(
            func.coalesce(func.sum(messages.c.tokens_in), 0), func.coalesce(func.sum(messages.c.tokens_out), 0),
            func.count(), func.coalesce(func.sum(_BLOCKED), 0)
        ).where(messages.c.role == "assistant")).one()
        users = c.execute(select(func.count(func.distinct(messages.c.user_id)))).scalar()
        n_orders, revenue = c.execute(select(func.count(), func.coalesce(func.sum(orders.c.total_cents), 0))
                                      .where(orders.c.status != "cancelled")).one()
        low = c.execute(select(func.count()).select_from(products)
                        .where(products.c.stock <= 3, products.c.active.is_(True))).scalar()
        mem = c.execute(select(func.count()).select_from(user_memories)).scalar()
    tin, tout = int(tin), int(tout)
    cost = tin / 1e6 * config.PRICE_IN_PER_M + tout / 1e6 * config.PRICE_OUT_PER_M
    return {"users": users, "agent_replies": n_replies, "blocked": int(blocked), "tokens_in": tin,
            "tokens_out": tout, "est_cost_usd": round(cost, 4), "orders": n_orders,
            "revenue_cents": int(revenue), "low_stock": low, "memories": mem}


def admin_memories(limit=200):
    with get_engine().begin() as c:
        rows = c.execute(select(user_memories).order_by(user_memories.c.id.desc()).limit(limit)).all()
    return [{"user_id": r.user_id, "fact": r.fact, "created_at": r.created_at.isoformat()} for r in rows]
