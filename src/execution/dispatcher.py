"""
Crash-safe outbox dispatcher.

Algorithm per cycle:
1. SELECT top-N outbox rows WHERE status = 'PENDING' ORDER BY id ASC.
2. For each row:
   a. Load the linked RecoveryAttempt to get the idempotency_key.
      (The key was written BEFORE this point — that's the crash-safety guarantee.)
   b. Call RazorpayClient.dispatch(action_type, payload, idempotency_key).
   c. On success  → mark outbox DISPATCHED, update attempt status to DISPATCHED.
   d. On failure  → mark outbox FAILED,     update attempt status to FAILED.
3. Return a summary dict.

Crash behaviour:
- If the process dies between step (b) and (c), the next restart re-reads
  the same PENDING row and calls Razorpay again with the same idempotency_key.
  Razorpay deduplicates, so no double-charge occurs.
"""

import json
import logging
from datetime import datetime, timezone

from src.data.database import Outbox, RecoveryAttempt
from src.execution.outbox import get_pending_items, mark_dispatched, mark_failed
from src.execution.razorpay_client import RazorpayClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_payload(raw: str | None) -> dict:
    """Safely decode the JSON payload stored in the outbox row."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Could not parse outbox payload: %s", raw)
        return {}


def _update_attempt_status(db, attempt_id: int, status: str) -> None:
    """Flip the RecoveryAttempt status and set completed_at."""
    attempt = db.get(RecoveryAttempt, attempt_id)
    if attempt is None:
        log.error("RecoveryAttempt id=%s not found", attempt_id)
        return
    attempt.status = status
    attempt.completed_at = datetime.now(timezone.utc)
    db.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_dispatch_cycle(
    db,
    client: RazorpayClient | None = None,
    limit: int = 50,
) -> dict:
    """
    Process up to *limit* pending outbox items in one cycle.

    Parameters
    ----------
    db      : SQLAlchemy Session
    client  : RazorpayClient instance (defaults to a new mock-aware one)
    limit   : max rows to process per call

    Returns
    -------
    dict with keys: processed, dispatched, failed
    """
    if client is None:
        client = RazorpayClient()

    pending = get_pending_items(db, limit=limit)

    results = {"processed": 0, "dispatched": 0, "failed": 0}

    for item in pending:
        results["processed"] += 1
        log.info(
            "Dispatching outbox id=%s txn=%s action=%s",
            item.id,
            item.transaction_id,
            item.action_type,
        )

        # Fetch idempotency key from the linked attempt
        attempt = db.get(RecoveryAttempt, item.recovery_attempt_id)
        if attempt is None:
            log.error(
                "Outbox item %s references missing RecoveryAttempt %s — marking FAILED",
                item.id,
                item.recovery_attempt_id,
            )
            mark_failed(db, item)
            results["failed"] += 1
            continue

        idempotency_key = attempt.idempotency_key
        payload = _parse_payload(item.payload)

        # ----------------------------------------------------------------
        # THE CRITICAL SECTION
        # idempotency_key already persisted → safe to call API now.
        # ----------------------------------------------------------------
        response = client.dispatch(
            action_type=item.action_type,
            payload=payload,
            idempotency_key=idempotency_key,
        )

        if response.get("success"):
            mark_dispatched(db, item)
            _update_attempt_status(db, item.recovery_attempt_id, "DISPATCHED")
            results["dispatched"] += 1
            log.info("Outbox id=%s → DISPATCHED", item.id)
        else:
            mark_failed(db, item)
            _update_attempt_status(db, item.recovery_attempt_id, "FAILED")
            results["failed"] += 1
            log.warning(
                "Outbox id=%s → FAILED error=%s",
                item.id,
                response.get("error", "unknown"),
            )

    return results
