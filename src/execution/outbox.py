from datetime import datetime, timezone

from src.data.database import Outbox


def _now():
    return datetime.now(timezone.utc)


def create_outbox_item(
    db,
    transaction_id,
    recovery_attempt_id,
    action_type,
    payload,
):
    item = Outbox(
        transaction_id=transaction_id,
        recovery_attempt_id=recovery_attempt_id,
        action_type=action_type,
        payload=payload,
        status="PENDING",
        created_at=_now(),
    )

    db.add(item)
    db.commit()

    return item


def get_pending_items(db, limit=50):
    return (
        db.query(Outbox)
        .filter(Outbox.status == "PENDING")
        .order_by(Outbox.id)
        .limit(limit)
        .all()
    )


def mark_dispatched(db, item):
    item.status = "DISPATCHED"
    item.dispatched_at = _now()

    db.commit()

    return item


def mark_failed(db, item):
    item.status = "FAILED"

    db.commit()

    return item