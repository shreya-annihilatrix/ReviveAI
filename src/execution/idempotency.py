import hashlib
from src.data.database import RecoveryAttempt, Outbox

def get_or_create_idempotency_key(db, txn_id: int, attempt_no: int, action_type: str) -> str:
    key = hashlib.sha256(f"{txn_id}:{attempt_no}:{action_type}".encode("utf-8")).hexdigest()
    
    # Store in DB BEFORE the API call
    existing = db.query(RecoveryAttempt).filter_by(idempotency_key=key).first()
    if not existing:
        new_attempt = RecoveryAttempt(
            transaction_id=txn_id,
            attempt_no=attempt_no,
            action_type=action_type,
            idempotency_key=key,
            status="CREATED"
        )
        db.add(new_attempt)
        db.commit()
    return key
