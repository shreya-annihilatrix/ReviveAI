"""
Triage cache — keyed by sha256(failure_code|payment_method|bank).

On hit  → return cached TriageResult dict, log hit.
On miss → caller runs LLM, then calls write() to persist.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------

def make_cache_key(failure_code: str, payment_method: str, bank: str) -> str:
    """sha256(failure_code|payment_method|bank) — same inputs always same key."""
    raw = f"{failure_code or ''}|{payment_method or ''}|{bank or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get(db, failure_code: str, payment_method: str, bank: str) -> dict | None:
    """
    Return cached classification dict or None on miss.
    Logs hit/miss at DEBUG level.
    """
    from src.data.database import TriageCache

    key = make_cache_key(failure_code, payment_method, bank)
    row = db.query(TriageCache).filter_by(
        failure_code=failure_code,
        payment_method=payment_method,
        bank=bank,
    ).first()

    if row is None:
        log.debug("Cache MISS  key=%s  fc=%s pm=%s bank=%s",
                  key[:8], failure_code, payment_method, bank)
        return None

    log.debug("Cache HIT   key=%s  fc=%s pm=%s bank=%s",
              key[:8], failure_code, payment_method, bank)
    return {
        "failure_type":         row.classification,
        "root_cause":           row.explanation or "",
        "confidence":           row.recovery_probability or 0.0,
        "recommended_channel":  "",          # not stored separately — fine
        "reasoning":            f"[cache hit] {row.explanation or ''}",
        "source":               "cache",
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write(
    db,
    failure_code: str,
    payment_method: str,
    bank: str,
    classification: str,
    recovery_probability: float,
    explanation: str,
) -> None:
    """
    Upsert a cache entry.  Silently skips if key already exists
    (unique constraint on failure_code, payment_method, bank).
    """
    from src.data.database import TriageCache

    # Check for existing entry first to avoid constraint errors
    existing = db.query(TriageCache).filter_by(
        failure_code=failure_code,
        payment_method=payment_method,
        bank=bank,
    ).first()

    if existing:
        # Update in place
        existing.classification      = classification
        existing.recovery_probability = recovery_probability
        existing.explanation          = explanation
    else:
        row = TriageCache(
            failure_code=failure_code,
            payment_method=payment_method,
            bank=bank,
            classification=classification,
            recovery_probability=recovery_probability,
            explanation=explanation,
        )
        db.add(row)

    db.commit()
    log.debug("Cache WRITE fc=%s pm=%s bank=%s cls=%s",
              failure_code, payment_method, bank, classification)
