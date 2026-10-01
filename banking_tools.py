"""
LLM tools for the voice agent, backed by a MOCK bank (in-memory fake data).

Built against pipecat-ai 1.12.0 as read from the installed source: tools are
async "direct functions" whose first parameter is `params` (FunctionCallParams);
the schema is extracted from the signature + docstring, and they are listed in
`LLMContext(tools=[...])`. Per-session state travels via
`PipelineWorker(app_resources=...)` -> `params.app_resources`.

Security rule: account data is gated IN CODE (`_require_verified`), never by
the prompt alone - a 4B model can be talked out of a prompt rule.

This is a stand-in for a real core-banking / CRM API. Replace MOCK_CUSTOMERS
access with real backend calls; the tool surface can stay the same.
"""

import hashlib
import hmac
from dataclasses import dataclass, field

from loguru import logger

from pipecat.services.llm_service import FunctionCallParams

MAX_VERIFY_ATTEMPTS = 3


def _h(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


# Fake data. Real systems never keep PINs like this; hashed here only so the
# comparison code path is the realistic one.
MOCK_CUSTOMERS = {
    "1001": {
        "name": "Priya Raman",
        "pin_hash": _h("4321"),
        "balance": 48250.75,
        "transactions": [
            {"date": "2026-09-28", "desc": "Electricity bill", "amount": -1840.00},
            {"date": "2026-09-27", "desc": "Salary credit", "amount": 52000.00},
            {"date": "2026-09-25", "desc": "Grocery store", "amount": -2310.50},
            {"date": "2026-09-22", "desc": "Mobile recharge", "amount": -399.00},
        ],
    },
    "1002": {
        "name": "Arun Kumar",
        "pin_hash": _h("9876"),
        "balance": 1520.00,
        "transactions": [
            {"date": "2026-09-29", "desc": "ATM withdrawal", "amount": -2000.00},
            {"date": "2026-09-20", "desc": "UPI transfer received", "amount": 3000.00},
        ],
    },
}
MOCK_TICKETS = {
    "T-1001": {"status": "open", "summary": "Card replacement requested", "eta": "3 working days"},
    "T-1002": {"status": "resolved", "summary": "Net banking password reset", "eta": None},
}


@dataclass
class SessionState:
    """Per-call state, passed to the worker as app_resources."""

    verified_customer_id: str | None = None
    failed_attempts: int = 0
    locked: bool = False
    end_requested: bool = False
    end_reason: str | None = None
    events: list[str] = field(default_factory=list)  # audit trail (tool, outcome)

    def audit(self, msg: str) -> None:
        self.events.append(msg)
        logger.info(f"AUDIT {msg}")  # never log PINs or full account numbers


def _require_verified(state: SessionState) -> dict | None:
    """Return an error result if the caller isn't verified, else None."""
    if state.locked:
        return {"error": "Verification is locked for this call. Offer to transfer to a human agent."}
    if state.verified_customer_id is None:
        return {"error": "Caller is not verified. Ask for customer ID and PIN, then call verify_customer."}
    return None


async def verify_customer(params: FunctionCallParams, customer_id: str, pin: str):
    """Verify the caller's identity. Must succeed before any account information is shared.

    Args:
        customer_id: The caller's customer ID, digits only.
        pin: The caller's 4 digit PIN, digits only.
    """
    state: SessionState = params.app_resources
    if state.locked:
        await params.result_callback({"verified": False, "error": "Too many failed attempts. Offer transfer to a human."})
        return
    cust = MOCK_CUSTOMERS.get(str(customer_id).strip())
    ok = cust is not None and hmac.compare_digest(cust["pin_hash"], _h(str(pin).strip()))
    if ok:
        state.verified_customer_id = str(customer_id).strip()
        state.failed_attempts = 0
        state.audit(f"verify_customer ok id={customer_id}")
        await params.result_callback({"verified": True, "customer_name": cust["name"]})
        return
    state.failed_attempts += 1
    state.audit(f"verify_customer FAIL id={customer_id} attempt={state.failed_attempts}")
    if state.failed_attempts >= MAX_VERIFY_ATTEMPTS:
        state.locked = True
        await params.result_callback({"verified": False, "error": "Too many failed attempts. Offer transfer to a human."})
        return
    # Same message for unknown ID and wrong PIN: don't reveal which was wrong.
    await params.result_callback({"verified": False, "error": "Customer ID or PIN did not match."})


async def get_account_balance(params: FunctionCallParams):
    """Get the verified caller's current account balance in rupees."""
    state: SessionState = params.app_resources
    if (err := _require_verified(state)) is not None:
        await params.result_callback(err)
        return
    cust = MOCK_CUSTOMERS[state.verified_customer_id]
    state.audit("get_account_balance")
    await params.result_callback({"balance_rupees": cust["balance"]})


async def get_recent_transactions(params: FunctionCallParams, count: int = 3):
    """Get the verified caller's most recent transactions, newest first.

    Args:
        count: How many transactions to return, 1 to 5.
    """
    state: SessionState = params.app_resources
    if (err := _require_verified(state)) is not None:
        await params.result_callback(err)
        return
    n = max(1, min(int(count), 5))
    cust = MOCK_CUSTOMERS[state.verified_customer_id]
    state.audit(f"get_recent_transactions n={n}")
    await params.result_callback({"transactions": cust["transactions"][:n]})


async def get_ticket_status(params: FunctionCallParams, ticket_id: str):
    """Look up the status of a support ticket. No verification needed.

    Args:
        ticket_id: The ticket number, for example T-1001.
    """
    state: SessionState = params.app_resources
    t = MOCK_TICKETS.get(str(ticket_id).strip().upper())
    state.audit(f"get_ticket_status found={t is not None}")
    await params.result_callback(t or {"error": "No ticket found with that number."})


async def transfer_to_human(params: FunctionCallParams, reason: str):
    """Hand the caller to a human agent. Use when the caller asks for a person,
    is upset, verification is locked, or the request is outside what you can do.

    Args:
        reason: Short reason for the transfer.
    """
    state: SessionState = params.app_resources
    state.audit(f"transfer_to_human reason={reason!r}")
    # STUB: no real call transfer exists yet (needs the telephony provider's
    # transfer/forward API). For now we tell the caller and end the call.
    state.end_requested = True
    state.end_reason = f"transfer: {reason}"
    await params.result_callback(
        {"status": "transfer_requested", "instruction": "Tell the caller a human agent will call back shortly, then say goodbye."}
    )


async def end_call(params: FunctionCallParams):
    """End the call. Use only after the caller says they need nothing else."""
    state: SessionState = params.app_resources
    state.audit("end_call")
    state.end_requested = True
    state.end_reason = "caller done"
    await params.result_callback({"status": "ending", "instruction": "Say a brief goodbye."})


TOOLS = [
    verify_customer,
    get_account_balance,
    get_recent_transactions,
    get_ticket_status,
    transfer_to_human,
    end_call,
]
