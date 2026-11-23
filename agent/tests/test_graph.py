"""Graph wiring test with a scripted fake LLM (no API key): nodes, tool config injection,
two-turn confirmation, short-term memory (thread_id) and long-term memory (user_id)."""
import os
import sys
import tempfile
import uuid
from pathlib import Path

os.environ["DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402

_d = Path(tempfile.mkdtemp())
config.sqlalchemy_url = lambda: f"sqlite:///{_d / 'g.db'}"

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app import db, graph as G  # noqa: E402

db.init_db()
SEEN_SYSTEM = []


class FakeLLM:
    def invoke(self, msgs):
        sys_txt = msgs[0].content
        last = msgs[-1]
        if sys_txt.startswith("You are a strict input classifier"):
            return AIMessage(content="OFF_TOPIC" if "poem" in last.content else "ALLOW")
        if sys_txt.startswith("Extract durable facts"):
            return AIMessage(content='["Prefers decaf"]', usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})
        SEEN_SYSTEM.append(sys_txt)
        u = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        if isinstance(last, ToolMessage):
            return AIMessage(content=f"tool said: {last.content[:60]}", usage_metadata=u)
        text = last.content.lower()
        if "buy" in text:
            return AIMessage(content="", usage_metadata=u, tool_calls=[
                {"name": "prepare_order", "args": {"items": [{"product_id": 1, "qty": 1}]}, "id": "c1"},
                # a misbehaving model that tries to confirm in the same turn:
                {"name": "confirm_pending_action", "args": {}, "id": "c2"}])
        if text.strip() == "yes":
            return AIMessage(content="", usage_metadata=u, tool_calls=[{"name": "confirm_pending_action", "args": {}, "id": "c3"}])
        return AIMessage(content="ok", usage_metadata=u)


def run(g, user, thread, msg):
    run_id = uuid.uuid4().hex
    cfg = {"configurable": {"thread_id": f"{user}::{thread}", "user_id": user, "run_id": run_id,
                         "user_message": msg}, "recursion_limit": 14}
    return g.invoke({"messages": [HumanMessage(content=msg)], "user_id": user, "run_id": run_id}, cfg)


def test_graph_flow(monkeypatch):
    monkeypatch.setattr(G, "_llm", lambda max_tokens, tools=False: FakeLLM())
    g = G.build_graph(InMemorySaver())

    # off-topic -> refuse node, message scrubbed from history
    r = run(g, "u1", "t1", "write a poem")
    assert r["blocked"] == "off_topic" and "only help" in r["messages"][-1].content
    assert "removed by safety filter" in r["messages"][-2].content

    # risky action: same-turn confirm is blocked, nothing ordered
    r = run(g, "u1", "t1", "I want to buy the first thing")
    turn = r["messages"][r["turn_start"]:]
    results = [m.content for m in turn if isinstance(m, ToolMessage)]
    assert any("awaiting_customer_confirmation" in x for x in results)
    assert any("BLOCKED" in x for x in results)
    assert db.list_orders("u1") == []
    assert r["tokens_in"] > 0

    # next turn "yes" -> order placed
    r = run(g, "u1", "t1", "yes")
    assert len(db.list_orders("u1")) == 1

    # long-term memory written, and injected into a brand-new thread's system prompt
    run(g, "u1", "t1", "I only drink decaf")
    assert "Prefers decaf" in db.get_memories("u1")
    run(g, "u1", "brand-new-thread", "hello")
    assert "Prefers decaf" in SEEN_SYSTEM[-1]

    # another user: no memory, no orders, no shared thread state
    run(g, "u2", "brand-new-thread", "hello")
    assert "Prefers decaf" not in SEEN_SYSTEM[-1]
    assert db.list_orders("u2") == []
    st = g.get_state({"configurable": {"thread_id": "u2::brand-new-thread"}})
    assert all("buy" not in str(m.content) for m in st.values["messages"])
