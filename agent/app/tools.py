"""Agent tools.

Security design:
  * `user_id` and `run_id` are NEVER tool arguments the model can choose. They are read from the
    LangGraph RunnableConfig that the API layer sets, so the model cannot touch another user's data.
  * Risky actions (placing / cancelling an order) are two-step. `prepare_*` only stores a pending
    action; `confirm_pending_action` refuses to run unless the pending action was created in an
    EARLIER /chat request, i.e. the customer had a chance to say "yes". The model cannot skip this.
"""
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from . import db, safety


def _ids(config: RunnableConfig) -> tuple[str, str]:
    c = config["configurable"]
    return c["user_id"], c["run_id"]


def money(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _brief(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "category": p["category"], "price": money(p["price_cents"]),
            "in_stock": p["stock"], "description": p["description"]}


class OrderLine(BaseModel):
    product_id: int = Field(description="Product id from search_products")
    qty: int = Field(default=1, ge=1, le=20, description="Quantity (1-20)")


@tool
def search_products(query: str = "", category: str = "") -> list[dict]:
    """Search the store catalogue. `query` is free text (e.g. 'light roast coffee'); `category` is one of
    coffee, tea, equipment, mugs. Leave both empty to list everything. Returns id, name, price, stock."""
    found = db.list_products(query=query, category=category, limit=8)
    return [_brief(p) for p in found] or [{"info": "No matching products."}]


@tool
def get_product_details(product_id: int) -> dict:
    """Get full details and live stock level for one product id."""
    p = db.get_product(product_id)
    if not p or not p["active"]:
        return {"error": "Product not found."}
    return _brief(p)


@tool
def get_my_orders(config: RunnableConfig) -> list[dict]:
    """List the current customer's own orders (id, items, total, status)."""
    user_id, _ = _ids(config)
    out = []
    for o in db.list_orders(user_id, limit=10):
        out.append({"order_id": o["id"], "status": o["status"], "total": money(o["total_cents"]),
                    "items": [f"{i['qty']}x {i['name']}" for i in o["items"]], "placed_at": o["created_at"]})
    return out or [{"info": "This customer has no orders yet."}]


@tool
def prepare_order(items: list[OrderLine], config: RunnableConfig) -> dict:
    """STEP 1 of buying. Validates stock and price and stores a PENDING order. Nothing is purchased yet.
    After calling this you MUST show the summary and ask the customer to confirm. Do not call
    confirm_pending_action in the same turn."""
    user_id, run_id = _ids(config)
    lines, summary, total = [], [], 0
    for it in items:
        p = db.get_product(it.product_id)
        if not p or not p["active"]:
            return {"error": f"Product {it.product_id} not found."}
        if p["stock"] < it.qty:
            return {"error": f"Only {p['stock']} of '{p['name']}' in stock."}
        lines.append({"product_id": p["id"], "qty": it.qty})
        total += p["price_cents"] * it.qty
        summary.append(f"{it.qty}x {p['name']} @ {money(p['price_cents'])}")
    if not lines:
        return {"error": "No items given."}
    db.set_pending(user_id, "place_order", {"lines": lines, "total_cents": total}, run_id)
    return {"status": "awaiting_customer_confirmation", "items": summary, "total": money(total),
            "instruction": "Ask the customer to explicitly confirm (yes/no) before buying."}


@tool
def prepare_cancel_order(order_id: int, config: RunnableConfig) -> dict:
    """STEP 1 of cancelling one of the customer's orders. Stores a PENDING cancellation; nothing is cancelled
    yet. You MUST ask the customer to confirm."""
    user_id, run_id = _ids(config)
    o = db.get_order(order_id, user_id)  # scoped to this user: other users' orders are invisible
    if not o:
        return {"error": "Order not found for this customer."}
    if o["status"] != "placed":
        return {"error": f"Order is already '{o['status']}' and cannot be cancelled."}
    db.set_pending(user_id, "cancel_order", {"order_id": order_id}, run_id)
    return {"status": "awaiting_customer_confirmation", "order_id": order_id, "total": money(o["total_cents"]),
            "instruction": "Ask the customer to explicitly confirm (yes/no) before cancelling."}


@tool
def confirm_pending_action(config: RunnableConfig) -> dict:
    """STEP 2. Execute the pending order/cancellation. ONLY call this after the customer has clearly said yes
    in a message that came AFTER you showed them the summary."""
    user_id, run_id = _ids(config)
    pend = db.get_pending(user_id)
    if not pend:
        return {"error": "Nothing is pending. Use prepare_order or prepare_cancel_order first."}
    if pend["run_id"] == run_id:
        return {"error": "BLOCKED: the customer has not confirmed yet. Show the summary and ask them to confirm."}
    if not safety.is_affirmative(config["configurable"].get("user_message", "")):
        return {"error": "BLOCKED: the customer's latest message is not a clear 'yes'. Ask them to reply 'yes' to confirm "
                         "or 'no' to cancel."}
    db.clear_pending(user_id)
    if pend["kind"] == "place_order":
        order, err = db.create_order(user_id, pend["payload"]["lines"])
        if err:
            return {"error": err}
        return {"status": "order_placed", "order_id": order["id"], "total": money(order["total_cents"]),
                "items": [f"{i['qty']}x {i['name']}" for i in order["items"]]}
    if pend["kind"] == "cancel_order":
        o = db.set_order_status(pend["payload"]["order_id"], "cancelled", user_id)
        return {"status": "order_cancelled", "order_id": o["id"]} if o else {"error": "Order not found."}
    return {"error": "Unknown pending action."}


@tool
def discard_pending_action(config: RunnableConfig) -> dict:
    """Throw away the pending order/cancellation (customer said no / changed their mind)."""
    user_id, _ = _ids(config)
    db.clear_pending(user_id)
    return {"status": "discarded"}


ALL_TOOLS = [search_products, get_product_details, get_my_orders, prepare_order,
             prepare_cancel_order, confirm_pending_action, discard_pending_action]
