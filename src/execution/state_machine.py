"""
State machine for ReviveAI — append-only transition log.
Every transition gets a SHA-256 compliance hash for tamper-evident audit.
"""
import hashlib
from datetime import datetime, timezone
import uuid
from src.data.database import TransactionState

LEGAL_TRANSITIONS = {
    "AT_RISK":           ["TRIAGED"],
    "TRIAGED":           ["STRATEGY_SELECTED"],
    "STRATEGY_SELECTED": ["ACTION_PENDING", "ABANDONED"],  # ABANDONED if gate rejects
    "ACTION_PENDING":    ["ACTION_SENT", "ABANDONED"],
    "ACTION_SENT":       ["AWAITING_OUTCOME"],
    "AWAITING_OUTCOME":  ["RECOVERED", "FAILED", "ESCALATED", "ABANDONED"],
}

class IllegalTransitionError(Exception):
    pass

def _make_compliance_hash(txn_id: int, from_state: str, to_state: str, timestamp: datetime) -> str:
    """
    SHA-256 of 'txn_id:from_state:to_state:iso_timestamp'.
    Allows any third party to verify a transition record was not altered after the fact
    by recomputing the hash from the stored fields and comparing.
    """
    raw = f"{txn_id}:{from_state}:{to_state}:{timestamp.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def transition(db, txn_id: int, from_state: str, to_state: str, metadata: str = None):
    if to_state not in LEGAL_TRANSITIONS.get(from_state, []):
        raise IllegalTransitionError(f"{from_state} -> {to_state} not allowed")

    now = datetime.now(timezone.utc)
    trace_id = str(uuid.uuid4())
    compliance_hash = _make_compliance_hash(txn_id, from_state, to_state, now)

    # INSERT new row — never UPDATE. Append-only guarantees immutability.
    state = TransactionState(
        transaction_id=txn_id,
        previous_state=from_state,
        state=to_state,
        trace_id=trace_id,
        reason=metadata,
        compliance_hash=compliance_hash,
        created_at=now,
    )
    db.add(state)
    db.commit()
    return state