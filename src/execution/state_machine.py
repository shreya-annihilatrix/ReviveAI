from datetime import datetime, timezone
import uuid

from src.data.database import TransactionState


ALLOWED_TRANSITIONS = {
    "AT_RISK": {"TRIAGED"},
    "TRIAGED": {"STRATEGY_SELECTED"},
    "STRATEGY_SELECTED": {"ACTION_PENDING"},
    "ACTION_PENDING": {"ACTION_SENT"},
    "ACTION_SENT": {"AWAITING_OUTCOME"},
    "AWAITING_OUTCOME": {
        "RECOVERED",
        "FAILED",
        "ESCALATED",
        "ABANDONED",
    },
    "RECOVERED": set(),
    "FAILED": set(),
    "ESCALATED": set(),
    "ABANDONED": set(),
}


def can_transition(current_state, new_state):
    allowed = ALLOWED_TRANSITIONS.get(current_state, set())

    return new_state in allowed


def validate_transition(current_state, new_state):
    if not can_transition(current_state, new_state):
        raise ValueError(
            f"Invalid state transition: "
            f"{current_state} -> {new_state}"
        )

    return True


def record_transition(
    db,
    transaction_id,
    current_state,
    new_state,
    reason=None,
    trace_id=None,
):
    validate_transition(current_state, new_state)

    if trace_id is None:
        trace_id = str(uuid.uuid4())

    state = TransactionState(
        transaction_id=transaction_id,
        previous_state=current_state,
        state=new_state,
        trace_id=trace_id,
        reason=reason,
        created_at=datetime.now(timezone.utc),
    )

    db.add(state)
    db.commit()

    return state