"""LangGraph definition.

        START
          |
        guard ----(blocked)----> refuse ----> END        node 1: safety (regex + LLM classifier)
          |
     load_memory                                          node 2: long-term memory read (per user_id)
          |
        agent <----> tools                                node 3: sales agent (+ ToolNode, 7 tools)
          |
       remember ----> END                                 node 4: long-term memory write (per user_id)

Short-term memory = LangGraph checkpointer, keyed by thread_id (namespaced with user_id).
"""
import json
import re
from typing import Annotated, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, trim_messages
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from . import config, db, safety
from .tools import ALL_TOOLS

MAX_HISTORY_MESSAGES = 24  # sliding window sent to the model (full history stays in the checkpoint)

_llm_cache = {}


def _llm(max_tokens: int, tools: bool = False):
    key = (max_tokens, tools)
    if key not in _llm_cache:
        m = ChatAnthropic(model=config.MODEL_NAME, api_key=config.ANTHROPIC_API_KEY or None,
                          max_tokens=max_tokens, timeout=45, max_retries=2)
        _llm_cache[key] = m.bind_tools(ALL_TOOLS) if tools else m
    return _llm_cache[key]


class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    user_id: str
    run_id: str
    blocked: str            # '' | off_topic | injection | rate_limit
    memories: list[str]
    turn_start: int         # index in messages where this turn begins
    tokens_in: int
    tokens_out: int


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _usage(msg) -> tuple[int, int]:
    u = getattr(msg, "usage_metadata", None) or {}
    return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))


# ------------------------------------------------------------------ node 1: guard
def guard(state: State):
    msgs = state["messages"]
    last = msgs[-1]
    user_text = text_of(last.content)
    base = {"turn_start": len(msgs) - 1, "tokens_in": 0, "tokens_out": 0, "blocked": ""}

    if safety.regex_injection(user_text):
        return {**base, "blocked": "injection"}
    if db.tokens_used_today(state["user_id"]) >= config.DAILY_TOKEN_LIMIT_PER_USER:
        return {**base, "blocked": "rate_limit"}

    # give the classifier the previous assistant turn so replies like "yes" are understood
    prev = next((text_of(m.content) for m in reversed(msgs[:-1]) if isinstance(m, AIMessage) and text_of(m.content)), "")
    prompt = f"Previous assistant message (for context only): {prev[:300]!r}\n\nLATEST customer message:\n{user_text}"
    try:
        out = _llm(8).invoke([SystemMessage(content=safety.GUARD_SYSTEM), HumanMessage(content=prompt)])
        tin, tout = _usage(out)
        label = text_of(out.content).strip().upper()
    except Exception:  # classifier failure: fail open, the agent prompt still enforces the rules
        return base
    base.update(tokens_in=tin, tokens_out=tout)
    if label.startswith("INJECTION"):
        base["blocked"] = "injection"
    elif label.startswith("OFF"):
        base["blocked"] = "off_topic"
    return base


def route_after_guard(state: State):
    return "refuse" if state.get("blocked") else "load_memory"


def refuse(state: State):
    human = state["messages"][-1]
    reply = safety.REFUSALS[state["blocked"]]
    # Overwrite (same id) the offending user message so it does not pollute later turns.
    return {"messages": [HumanMessage(content=f"[message removed by safety filter: {state['blocked']}]", id=human.id),
                         AIMessage(content=reply)]}


# ------------------------------------------------------------------ node 2: load long-term memory
def load_memory(state: State):
    return {"memories": db.get_memories(state["user_id"])}


# ------------------------------------------------------------------ node 3: sales agent
def agent(state: State):
    history = trim_messages(state["messages"], max_tokens=MAX_HISTORY_MESSAGES, token_counter=len,
                            strategy="last", start_on="human", include_system=False, allow_partial=False)
    out = _llm(config.MAX_OUTPUT_TOKENS, tools=True).invoke(
        [SystemMessage(content=safety.system_prompt(state.get("memories", [])))] + list(history))
    tin, tout = _usage(out)
    return {"messages": [out], "tokens_in": state.get("tokens_in", 0) + tin,
            "tokens_out": state.get("tokens_out", 0) + tout}


# ------------------------------------------------------------------ node 4: write long-term memory
def remember(state: State):
    user_text = text_of(state["messages"][state["turn_start"]].content)
    if not safety.memory_worthy(user_text):
        return {}
    try:
        out = _llm(150).invoke([SystemMessage(content=safety.MEMORY_SYSTEM), HumanMessage(content=user_text)])
        tin, tout = _usage(out)
        raw = text_of(out.content)
        m = re.search(r"\[.*\]", raw, re.S)
        facts = json.loads(m.group(0)) if m else []
    except Exception:
        return {}
    for f in facts[:3]:
        if isinstance(f, str) and safety.safe_memory_fact(f):
            db.add_memory(state["user_id"], f)
    return {"tokens_in": state.get("tokens_in", 0) + tin, "tokens_out": state.get("tokens_out", 0) + tout}


_tool_node = ToolNode(ALL_TOOLS)


def tools(state: State, config):
    """Runs the tools. ToolNode executes calls in parallel, so a `confirm_pending_action` sharing a step
    with other calls (e.g. prepare_order) could race them. Such confirms are rejected outright."""
    last = state["messages"][-1]
    calls = last.tool_calls
    if len(calls) > 1 and any(c["name"] == "confirm_pending_action" for c in calls):
        rejected = [ToolMessage(content=json.dumps({"error": "BLOCKED: confirm_pending_action must be the only tool "
                                                    "call in a step, and only after the customer said yes."}),
                                tool_call_id=c["id"], name=c["name"])
                    for c in calls if c["name"] == "confirm_pending_action"]
        rest = [c for c in calls if c["name"] != "confirm_pending_action"]
        ok = _tool_node.invoke({"messages": [AIMessage(content="", tool_calls=rest, id=last.id)]}, config)["messages"]
        return {"messages": ok + rejected}
    return _tool_node.invoke(state, config)


def build_graph(checkpointer):
    g = StateGraph(State)
    g.add_node("guard", guard)
    g.add_node("refuse", refuse)
    g.add_node("load_memory", load_memory)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("remember", remember)

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", route_after_guard, {"refuse": "refuse", "load_memory": "load_memory"})
    g.add_edge("refuse", END)
    g.add_edge("load_memory", "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "remember"})
    g.add_edge("tools", "agent")
    g.add_edge("remember", END)
    return g.compile(checkpointer=checkpointer)
