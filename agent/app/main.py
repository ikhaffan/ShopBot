"""ShopBot API.

Public (no login):  POST /chat, GET /health, GET /products
Customer-scoped:    GET /orders, /history, /memory  (need the caller's own user_id)
Admin:              /admin/*  (X-Admin-Token header)
"""
import hmac
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

from . import config, db, safety
from .graph import build_graph, text_of

ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,100}$")

state = {"graph": None, "pool": None}


def make_checkpointer():
    """Short-term memory store. Postgres in production, SQLite file locally."""
    if config.is_postgres():
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            config.psycopg_url(), min_size=1, max_size=5, open=True, check=ConnectionPool.check_connection,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        state["pool"] = pool
        saver = PostgresSaver(pool)
        saver.setup()
        return saver
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    conn = sqlite3.connect(str(config.ROOT / "shopbot_checkpoints.db"), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    state["graph"] = build_graph(make_checkpointer())
    yield
    if state["pool"]:
        state["pool"].close()


app = FastAPI(title="ShopBot Agent API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=False)


# ------------------------------------------------------------------ helpers
def check_id(value: str, name: str):
    if not ID_RE.match(value or ""):
        raise HTTPException(422, f"{name} must be 1-100 chars of letters, digits, _ . : @ -")


def require_admin(x_admin_token: str = Header(default="")):
    if not config.ADMIN_TOKEN or not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(401, "Invalid admin token")


# ------------------------------------------------------------------ agent API (graded)
class ChatIn(BaseModel):
    user_id: str
    thread_id: str
    message: str = Field(min_length=1)


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.post("/chat")
def chat(body: ChatIn):
    check_id(body.user_id, "user_id")
    check_id(body.thread_id, "thread_id")
    message = body.message.strip()
    if not message:
        raise HTTPException(422, "message is empty")
    if len(message) > config.MAX_MESSAGE_CHARS:
        reply = safety.REFUSALS["too_long"]
        return {"reply": reply, "tool_calls": [], "tokens": {"in": 0, "out": 0}}
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server")

    run_id = uuid.uuid4().hex
    cfg = {
        # thread ids are namespaced by user, so two users can never share (or guess) a conversation
        "configurable": {"thread_id": f"{body.user_id}::{body.thread_id}", "user_id": body.user_id, "run_id": run_id,
                         "user_message": message},
        "recursion_limit": 14,
    }
    try:
        result = state["graph"].invoke(
            {"messages": [HumanMessage(content=message)], "user_id": body.user_id, "run_id": run_id}, cfg)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Agent error: {type(e).__name__}: {str(e)[:300]}")

    turn = result["messages"][result.get("turn_start", 0):]
    results = {m.tool_call_id: text_of(m.content) for m in turn if isinstance(m, ToolMessage)}
    tool_calls = [{"name": tc["name"], "args": tc["args"], "result": results.get(tc["id"], "")[:600]}
                  for m in turn if isinstance(m, AIMessage) for tc in (m.tool_calls or [])]
    reply = next((text_of(m.content) for m in reversed(turn) if isinstance(m, AIMessage) and text_of(m.content)),
                 "Sorry, I couldn't produce an answer. Please try again.")
    tin, tout = int(result.get("tokens_in", 0)), int(result.get("tokens_out", 0))
    blocked = result.get("blocked", "")

    db.log_message(body.user_id, body.thread_id, "user", message, blocked=blocked)
    db.log_message(body.user_id, body.thread_id, "assistant", reply, tool_calls=tool_calls,
                   tin=tin, tout=tout, blocked=blocked)
    return {"reply": reply, "tool_calls": tool_calls, "tokens": {"in": tin, "out": tout}}


# ------------------------------------------------------------------ storefront API
@app.get("/products")
def products(q: str = "", category: str = ""):
    return db.list_products(query=q, category=category)


@app.get("/orders")
def my_orders(user_id: str):
    check_id(user_id, "user_id")
    return db.list_orders(user_id)


@app.get("/history")
def history(user_id: str, thread_id: str):
    check_id(user_id, "user_id")
    check_id(thread_id, "thread_id")
    return db.thread_history(user_id, thread_id)


@app.get("/memory")
def my_memory(user_id: str):
    check_id(user_id, "user_id")
    return {"user_id": user_id, "facts": db.get_memories(user_id)}


@app.delete("/memory")
def forget_me(user_id: str):
    check_id(user_id, "user_id")
    db.delete_memories(user_id)
    return {"ok": True}


# ------------------------------------------------------------------ admin API
class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    category: str = ""
    price_cents: int = Field(ge=0)
    stock: int = Field(ge=0)
    image_url: str = ""
    active: bool = True


class OrderStatusIn(BaseModel):
    status: str = Field(pattern="^(placed|shipped|delivered|cancelled)$")


@app.get("/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats():
    return db.admin_stats()


@app.get("/admin/conversations", dependencies=[Depends(require_admin)])
def admin_conversations():
    return db.admin_conversations()


@app.get("/admin/conversations/{user_id}/{thread_id}", dependencies=[Depends(require_admin)])
def admin_thread(user_id: str, thread_id: str):
    return db.thread_history(user_id, thread_id)


@app.get("/admin/orders", dependencies=[Depends(require_admin)])
def admin_orders():
    return db.list_orders(None, limit=200)


@app.patch("/admin/orders/{oid}", dependencies=[Depends(require_admin)])
def admin_order_status(oid: int, body: OrderStatusIn):
    o = db.set_order_status(oid, body.status)
    if not o:
        raise HTTPException(404, "Order not found")
    return o


@app.get("/admin/products", dependencies=[Depends(require_admin)])
def admin_products():
    return db.list_products(include_inactive=True, limit=500)


@app.post("/admin/products", dependencies=[Depends(require_admin)])
def admin_product_create(p: ProductIn):
    return {"id": db.create_product(p.model_dump())}


@app.put("/admin/products/{pid}", dependencies=[Depends(require_admin)])
def admin_product_update(pid: int, p: ProductIn):
    if not db.get_product(pid):
        raise HTTPException(404, "Product not found")
    db.update_product(pid, p.model_dump())
    return db.get_product(pid)


@app.delete("/admin/products/{pid}", dependencies=[Depends(require_admin)])
def admin_product_delete(pid: int):
    db.delete_product(pid)
    return {"ok": True}


@app.get("/admin/memories", dependencies=[Depends(require_admin)])
def admin_memories():
    return db.admin_memories()
