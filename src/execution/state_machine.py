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

def transition(db, txn_id: int, from_state: str, to_state: str, metadata: str = None):
    if to_state not in LEGAL_TRANSITIONS.get(from_state, []):
        raise IllegalTransitionError(f"{from_state} → {to_state} not allowed")
    
    # INSERT new row into transaction_states — never UPDATE
    trace_id = str(uuid.uuid4())
    state = TransactionState(
        transaction_id=txn_id,
        previous_state=from_state,
        state=to_state,
        trace_id=trace_id,
        reason=metadata,
        created_at=datetime.now(timezone.utc),
    )
    db.add(state)
    db.commit()
    return state