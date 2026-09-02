"""
Thompson-Sampling Contextual Bandit — Phase 6.

Arms = (channel, timing_bucket, instrument_type) triplets.
Priors start at Beta(1, 1) (uninformative — 50% prior).
After each outcome: success → alpha += 1, failure → beta += 1.
Posteriors persisted in bandit_posteriors table across batches.

Timing buckets:
  "immediate"  → < 30 min
  "short"      → 30 min – 4 h
  "salary"     → after salary credit window
  "next_day"   → +24 h
"""

import logging
import random
from typing import NamedTuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arm definition
# ---------------------------------------------------------------------------

class Arm(NamedTuple):
    channel: str          # "payment_link", "sms", "whatsapp", "retry_same", etc.
    timing_bucket: str    # "immediate", "short", "salary", "next_day"
    instrument_type: str  # "upi", "card", "netbanking", "emandate", "any"

    def arm_id(self) -> str:
        return f"{self.channel}|{self.timing_bucket}|{self.instrument_type}"


# All arms the bandit considers
ALL_ARMS: list[Arm] = [
    # Payment links
    Arm("payment_link",          "immediate",  "any"),
    Arm("payment_link",          "short",      "any"),
    Arm("payment_link",          "salary",     "any"),
    Arm("payment_link",          "next_day",   "any"),
    # SMS / WhatsApp
    Arm("sms",                   "immediate",  "any"),
    Arm("sms",                   "short",      "any"),
    Arm("whatsapp",              "immediate",  "any"),
    Arm("whatsapp",              "short",      "any"),
    # Retry
    Arm("retry_same",            "immediate",  "upi"),
    Arm("retry_same",            "short",      "upi"),
    Arm("retry_same",            "immediate",  "card"),
    Arm("retry_same",            "short",      "card"),
    Arm("retry_same",            "immediate",  "netbanking"),
    # Specialist actions (these are the high-probability arms per failure class)
    Arm("reauth_flow",           "immediate",  "emandate"),   # MANDATE_EXPIRED → 68%
    Arm("update_vpa_flow",       "immediate",  "any"),        # VPA_NOT_FOUND → 83%
    Arm("payment_method_update", "immediate",  "any"),        # CARD_EXPIRED → 82%
    Arm("retry_2h_window",       "short",      "any"),        # BANK_SERVER_DOWN → 74%
    Arm("salary_window_retry",   "salary",     "any"),        # INSUFFICIENT_FUNDS → 61%
    Arm("split_payment",         "immediate",  "upi"),        # LIMIT_EXCEEDED → 68%
    Arm("split_payment",         "immediate",  "card"),
    # Do nothing (explicit EV=0 baseline)
    Arm("do_nothing",            "immediate",  "any"),
]

# Map arm_id → Arm for fast lookup
ARM_BY_ID: dict[str, Arm] = {a.arm_id(): a for a in ALL_ARMS}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_or_create_posterior(db, failure_class: str, arm_id: str) -> tuple[float, float]:
    """
    Return (alpha, beta) for (failure_class, arm_id).
    Creates a Beta(1,1) prior if the row doesn't exist yet.
    """
    from src.data.database import BanditPosterior

    row = db.query(BanditPosterior).filter_by(
        failure_class=failure_class,
        arm=arm_id,
    ).first()

    if row is None:
        row = BanditPosterior(failure_class=failure_class, arm=arm_id,
                              alpha=1.0, beta=1.0)
        db.add(row)
        db.commit()

    return row.alpha, row.beta


def _update_posterior(db, failure_class: str, arm_id: str, success: bool) -> None:
    """
    Update Beta posterior after an observed outcome.
      success=True  → alpha += 1
      success=False → beta  += 1
    """
    from src.data.database import BanditPosterior

    row = db.query(BanditPosterior).filter_by(
        failure_class=failure_class,
        arm=arm_id,
    ).first()

    if row is None:
        row = BanditPosterior(failure_class=failure_class, arm=arm_id,
                              alpha=1.0, beta=1.0)
        db.add(row)

    if success:
        row.alpha += 1.0
    else:
        row.beta += 1.0

    db.commit()
    log.debug("Bandit updated: fc=%s arm=%s success=%s α=%.1f β=%.1f",
              failure_class, arm_id, success, row.alpha, row.beta)


# ---------------------------------------------------------------------------
# Thompson sampling
# ---------------------------------------------------------------------------

def sample_arm(
    db,
    failure_class: str,
    eligible_arms: list[Arm] | None = None,
    rng: random.Random | None = None,
) -> Arm:
    """
    Thompson sampling: draw θ ~ Beta(α, β) for each arm, pick argmax θ.

    Parameters
    ----------
    db            : SQLAlchemy session
    failure_class : e.g. "insufficient_funds"
    eligible_arms : subset of ALL_ARMS to consider (None = all)
    rng           : optional seeded Random for reproducibility in tests

    Returns
    -------
    The selected Arm.
    """
    arms = eligible_arms if eligible_arms is not None else ALL_ARMS
    _rng = rng or random.Random()

    best_arm   = arms[0]
    best_theta = -1.0

    for arm in arms:
        alpha, beta = _get_or_create_posterior(db, failure_class, arm.arm_id())
        # Draw from Beta distribution using inverse CDF trick via random
        theta = _rng.betavariate(alpha, beta)
        if theta > best_theta:
            best_theta = theta
            best_arm   = arm

    log.debug("Bandit sampled: fc=%s arm=%s theta=%.3f",
              failure_class, best_arm.arm_id(), best_theta)
    return best_arm


# ---------------------------------------------------------------------------
# Outcome feedback
# ---------------------------------------------------------------------------

def record_outcome(
    db,
    failure_class: str,
    arm_id: str,
    recovered: bool,
) -> None:
    """
    Call this after the customer outcome is known.
    Updates the Beta posterior for (failure_class, arm_id).
    """
    _update_posterior(db, failure_class, arm_id, success=recovered)


# ---------------------------------------------------------------------------
# Posterior summary (for dashboard)
# ---------------------------------------------------------------------------

def get_posterior_summary(db, failure_class: str) -> list[dict]:
    """
    Return a sorted list of {arm_id, alpha, beta, mean, arm} dicts
    for the given failure class. Useful for dashboard display.
    """
    rows = []
    for arm in ALL_ARMS:
        alpha, beta = _get_or_create_posterior(db, failure_class, arm.arm_id())
        mean = alpha / (alpha + beta)
        rows.append({
            "arm_id":  arm.arm_id(),
            "alpha":   alpha,
            "beta":    beta,
            "mean":    round(mean, 4),
            "arm":     arm,
        })
    return sorted(rows, key=lambda r: r["mean"], reverse=True)
