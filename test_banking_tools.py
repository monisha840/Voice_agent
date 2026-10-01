"""Offline tests for banking_tools (no Ollama/Pipecat pipeline needed)."""
import asyncio
from types import SimpleNamespace

from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper

import banking_tools as bt


def call(fn, state, **kw):
    out = {}

    async def cb(result, *, properties=None):
        out["r"] = result

    p = SimpleNamespace(app_resources=state, result_callback=cb)
    asyncio.run(fn(p, **kw))
    return out["r"]


def test_schemas_extract():
    for fn in bt.TOOLS:
        s = DirectFunctionWrapper(fn).to_function_schema()
        assert s.name == fn.__name__ and s.description, fn.__name__
    s = DirectFunctionWrapper(bt.verify_customer).to_function_schema()
    assert set(s.required) == {"customer_id", "pin"}


def test_data_gated_until_verified():
    st = bt.SessionState()
    assert "error" in call(bt.get_account_balance, st)
    assert "error" in call(bt.get_recent_transactions, st)


def test_verify_then_balance():
    st = bt.SessionState()
    assert call(bt.verify_customer, st, customer_id="1001", pin="4321")["verified"]
    assert call(bt.get_account_balance, st)["balance_rupees"] == 48250.75
    assert len(call(bt.get_recent_transactions, st, count=2)["transactions"]) == 2


def test_lockout_after_three_failures_even_with_right_pin():
    st = bt.SessionState()
    for _ in range(3):
        assert not call(bt.verify_customer, st, customer_id="1001", pin="0000")["verified"]
    assert st.locked
    assert not call(bt.verify_customer, st, customer_id="1001", pin="4321")["verified"]
    assert "error" in call(bt.get_account_balance, st)


def test_unknown_id_same_message_as_wrong_pin():
    a = call(bt.verify_customer, bt.SessionState(), customer_id="9999", pin="1111")
    b = call(bt.verify_customer, bt.SessionState(), customer_id="1001", pin="1111")
    assert a == b


def test_end_and_transfer_flag():
    st = bt.SessionState(); call(bt.end_call, st); assert st.end_requested
    st = bt.SessionState(); call(bt.transfer_to_human, st, reason="angry"); assert st.end_requested
