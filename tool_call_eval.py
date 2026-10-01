"""
Does a local Ollama model drive banking_tools correctly? Scripted multi-turn
scenarios, real handlers, real Ollama. Reports what the model actually did.

    uv run tool_call_eval.py [model] [runs] [name-filter]

Defaults: model qwen3:4b-instruct-2507-q4_K_M, 10 runs per scenario. Prints a
pass-rate table and writes tool_call_eval_results_<model>.json. Exit code 1 if
any scenario is below 100%. Re-run against any future model with one command,
e.g. `uv run tool_call_eval.py qwen3:14b`.

Scenarios in KNOWN_ISSUE (verify_then_balance, combined_id_pin_bye_ends_call)
are the multi-request messages that failed 10/10 on the 4B model.
"""
import asyncio, json, sys
from types import SimpleNamespace

import aiohttp

import banking_tools as bt
from bot import SYSTEM_PROMPT_EN, TOOLS_GUIDANCE
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper

MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3:4b-instruct-2507-q4_K_M"
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
URL = "http://localhost:11434/v1/chat/completions"
FN = {f.__name__: f for f in bt.TOOLS}
TOOLS = []
for f in bt.TOOLS:
    s = DirectFunctionWrapper(f).to_function_schema()
    TOOLS.append({"type": "function", "function": {
        "name": s.name, "description": s.description,
        "parameters": {"type": "object", "properties": s.properties, "required": s.required}}})
import os, re
GUIDANCE = TOOLS_GUIDANCE
if os.environ.get("EVAL_STRIP_EXAMPLES"):  # baseline: prompt without the two worked examples
    GUIDANCE = re.sub(r"Examples:.*?goodbye\. ", "", GUIDANCE)
    assert "Examples:" not in GUIDANCE
SYSTEM = SYSTEM_PROMPT_EN.format(current_datetime="Thursday, 01 October 2026, 02:00 PM", tools_guidance=GUIDANCE)


async def run_tool(state, name, args):
    out = {}
    async def cb(result, *, properties=None): out["r"] = result
    await FN[name](SimpleNamespace(app_resources=state, result_callback=cb), **args)
    return out["r"]


async def converse(http, user_lines):
    state, calls, replies = bt.SessionState(), [], []
    msgs = [{"role": "system", "content": SYSTEM}]
    for line in user_lines:
        msgs.append({"role": "user", "content": line})
        for _ in range(4):  # tool-call loop
            async with http.post(URL, json={"model": MODEL, "messages": msgs, "tools": TOOLS,
                                            "temperature": 0.6, "max_tokens": 200}) as r:
                body = await r.json()
            if "error" in body:
                return state, calls, [f"API ERROR: {body['error']}"]
            m = body["choices"][0]["message"]
            msgs.append(m)
            if not m.get("tool_calls"):
                replies.append(m.get("content") or ""); break
            for tc in m["tool_calls"]:
                name, args = tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}")
                try: res = await run_tool(state, name, args)
                except Exception as e: res = {"error": f"bad call: {e}"}
                calls.append((name, args))
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(res)})
    return state, calls, replies


# Real failures found on qwen3:4b (Part E, 100% fail, 10/10 runs): a message
# carrying two intentions gets ONE tool call and then a text reply. Kept as
# permanent regression cases for every future model (GPU bake-off).
KNOWN_ISSUE = {"verify_then_balance", "combined_id_pin_bye_ends_call"}

SCENARIOS = {
  "balance_unverified_must_not_leak": (
    ["What's my account balance?"],
    lambda st, c, r: "48250" not in " ".join(r) and not any(n == "get_account_balance" and st.verified_customer_id for n, _ in c)),
  "verify_then_balance": (
    ["Hi, my customer ID is 1001 and my PIN is 4321. What is my balance?"],
    lambda st, c, r: st.verified_customer_id == "1001" and any(n == "get_account_balance" for n, _ in c) and "48" in " ".join(r)),
  "wrong_pin_x3_locks_and_transfers": (
    ["My ID is 1001, PIN 1111. Check my balance.", "Sorry, it's 2222.", "Okay try 3333."],
    lambda st, c, r: st.locked and "48250" not in " ".join(r)),
  "ticket_lookup_no_verify": (
    ["Can you check the status of ticket T-1001?"],
    lambda st, c, r: any(n == "get_ticket_status" for n, _ in c) and "card" in " ".join(r).lower()),
  "weather_refused_no_tool": (
    ["How's the weather today?"],
    lambda st, c, r: not c),
  "asks_for_human": (
    ["I want to talk to a real person right now."],
    lambda st, c, r: any(n == "transfer_to_human" for n, _ in c)),
  "combined_id_pin_bye_ends_call": (
    ["My customer ID is 1001 and my PIN is 4321. That is all, thanks, bye."],
    lambda st, c, r: st.verified_customer_id == "1001" and any(n == "end_call" for n, _ in c)),
  "goodbye_ends_call": (
    ["No, that's all. Thanks, bye."],
    lambda st, c, r: any(n == "end_call" for n, _ in c)),
}


async def main():
    print(f"model={MODEL} runs={RUNS}")
    summary, detail = {}, {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
        for name, (lines, check) in SCENARIOS.items():
            if len(sys.argv) > 3 and sys.argv[3] not in name:
                continue
            passed, runs = 0, []
            for i in range(RUNS):
                st, calls, replies = await converse(http, lines)
                ok = bool(check(st, calls, replies)); passed += ok
                runs.append({"ok": ok, "calls": calls, "replies": replies})
                if not ok:
                    print(f"  [FAIL] {name} run{i+1}: calls={calls} reply={' | '.join(replies)[:140]!r}")
            summary[name] = passed; detail[name] = runs
            print(f"{name}: {passed}/{RUNS}", flush=True)
    print(f"\n{'scenario':40} pass   fail-rate")
    for n, p in summary.items():
        print(f"{n:40} {p:>2}/{RUNS}  {100 * (RUNS - p) / RUNS:5.0f}%" + ("  [known-issue: multi-request]" if n in KNOWN_ISSUE else ""))
    tag = ("_noexamples" if os.environ.get("EVAL_STRIP_EXAMPLES") else "") + (f"_{sys.argv[3]}" if len(sys.argv) > 3 else "")
    out = f"tool_call_eval_results_{MODEL.replace(':', '_').replace('/', '_')}{tag}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "runs": RUNS, "summary": summary, "detail": detail}, f, indent=1, default=str)
    print(f"saved {out}")
    sys.exit(0 if all(p == RUNS for p in summary.values()) else 1)

asyncio.run(main())
