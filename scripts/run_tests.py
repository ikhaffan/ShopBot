"""Run tests.json against a running agent API and print/save a benchmark table.

    python scripts/run_tests.py                       # http://localhost:8000
    python scripts/run_tests.py https://my-agent.hf.space
Writes benchmark_results.json and prints a Markdown table for the README.
"""
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
PRICE_IN, PRICE_OUT = 1.0, 5.0  # USD / 1M tokens (Haiku-class estimate)


def chat(client, user, thread, message):
    t0 = time.time()
    r = client.post(f"{BASE}/chat", json={"user_id": user, "thread_id": thread, "message": message}, timeout=90)
    r.raise_for_status()
    d = r.json()
    d["latency"] = time.time() - t0
    return d


def check(step_results, checks):
    last = step_results[-1]
    all_tools = [t["name"] for s in step_results for t in s["tool_calls"]]
    last_tools = [t["name"] for t in last["tool_calls"]]
    reply, last_reply = " ".join(s["reply"] for s in step_results).lower(), last["reply"].lower()
    fails = []
    for t in checks.get("tools_include", []):
        if t not in all_tools: fails.append(f"missing tool {t}")
    for t in checks.get("tools_exclude", []):
        if t in all_tools: fails.append(f"forbidden tool {t}")
    for t in checks.get("last_step_tools_include", []):
        if t not in last_tools: fails.append(f"last step missing tool {t}")
    for s in checks.get("reply_contains", []):
        if s.lower() not in reply: fails.append(f"reply lacks '{s}'")
    for s in checks.get("reply_excludes", []):
        if s.lower() in reply: fails.append(f"reply contains forbidden '{s}'")
    if checks.get("reply_contains_any") and not any(s.lower() in reply for s in checks["reply_contains_any"]):
        fails.append(f"reply has none of {checks['reply_contains_any']}")
    if checks.get("last_step_reply_contains_any") and not any(s.lower() in last_reply for s in checks["last_step_reply_contains_any"]):
        fails.append(f"last reply has none of {checks['last_step_reply_contains_any']}")
    return fails


def main():
    tests = json.loads((ROOT / "tests.json").read_text(encoding="utf-8"))
    run = uuid.uuid4().hex[:6]
    results = []
    with httpx.Client() as client:
        assert client.get(f"{BASE}/health", timeout=60).text.strip().strip('"') == "ok", "health check failed"
        for tc in tests:
            user, thread = f"bench_{run}_{tc['id']}", f"t_{run}_{tc['id']}"
            if "setup_other_user" in tc:
                chat(client, f"bench_{run}_other", "t1", tc["setup_other_user"]["message"])
            steps = []
            for i, st in enumerate(tc["steps"]):
                if st.get("new_thread"): thread += "_b"
                if st.get("as_new_user"): user += "_new"
                steps.append(chat(client, user, thread, st["message"]))
            fails = check(steps, tc["checks"])
            tin = sum(s["tokens"]["in"] for s in steps); tout = sum(s["tokens"]["out"] for s in steps)
            results.append({"id": tc["id"], "category": tc["category"], "passed": not fails, "fails": fails,
                            "latency_s": round(sum(s["latency"] for s in steps), 2), "tokens_in": tin, "tokens_out": tout,
                            "reply": steps[-1]["reply"][:300]})
            print(("PASS " if not fails else "FAIL ") + tc["id"], fails or "")
    passed = sum(r["passed"] for r in results)
    tin, tout = sum(r["tokens_in"] for r in results), sum(r["tokens_out"] for r in results)
    cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
    lat = sorted(r["latency_s"] for r in results)
    (ROOT / "benchmark_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\n| Test | Category | Result | Latency (s) | Tokens in/out |\n|---|---|---|---|---|")
    for r in results:
        print(f"| {r['id']} | {r['category']} | {'✅' if r['passed'] else '❌'} | {r['latency_s']} | {r['tokens_in']}/{r['tokens_out']} |")
    print(f"\n**{passed}/{len(results)} passed** · median latency {lat[len(lat)//2]}s · {tin}+{tout} tokens · est. cost ${cost:.4f}")


if __name__ == "__main__":
    main()
