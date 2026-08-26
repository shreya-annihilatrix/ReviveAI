"""
Phase 4 — Arm 0 and Arm A baseline measurements.

Arm 0 (do_nothing):
    No intervention. Call the simulator with action='do_nothing'
    for every transaction. Captures organic recovery — customers who
    retry on their own, banks that recover, etc.
    This is your floor.

Arm A (naive_retry):
    Fixed policy regardless of failure type:
      T+1h  → retry_same (same instrument, same bank)
      T+24h → retry_same (second attempt)
      T+24h → send_sms   (generic English SMS nudge)
    This is current industry practice — no intelligence, no personalisation.
    This is the number your agent must beat.

Both arms:
  - Use ALL 120 transactions (84 training + 36 holdout)
  - Are fully deterministic with seed=42
  - Return {txn_id: bool} and a summary dict

Run standalone:
    python -m src.metrics.arms
"""

import os
import sys
import random
from datetime import datetime, timedelta, timezone
from typing import Any

# Allow simulator import from this non-agent module
os.environ.setdefault("ALLOW_SIMULATOR_IMPORT", "true")

from src.data.simulator import simulate_outcome  # noqa: E402

SEED = int(os.getenv("SEED", "42"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    """Attach UTC timezone to a naive datetime from the DB."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _load_all_transactions(main_db, gt_db) -> list[dict]:
    """
    Load all 120 transactions as plain dicts (84 from main DB + 36 holdout).
    Each dict has the keys the simulator needs:
      failure_code, opted_out, amount, mandate_expiry, created_at
    """
    from src.data.database import Transaction, Customer, EvalHoldout

    rows: list[dict] = []

    # Training set (84)
    results = (
        main_db.query(Transaction, Customer)
        .join(Customer, Transaction.customer_id == Customer.id)
        .all()
    )
    for txn, cust in results:
        rows.append({
            "txn_id":         txn.id,
            "source":         "training",
            "failure_code":   txn.failure_code or "UNKNOWN",
            "amount":         txn.amount,
            "opted_out":      cust.opted_out,
            "created_at":     _utc(txn.created_at),
            "mandate_expiry": _utc(txn.mandate_expiry),
        })

    # Holdout set (36)
    holdout_results = gt_db.query(EvalHoldout).all()
    for h in holdout_results:
        rows.append({
            "txn_id":         f"hld_{h.id}",
            "source":         "holdout",
            "failure_code":   h.failure_code or "UNKNOWN",
            "amount":         h.amount,
            "opted_out":      h.opt_out_status,
            "created_at":     _utc(h.created_at),
            "mandate_expiry": _utc(h.mandate_expiry),
        })

    return rows


def _mandate_expired_days(txn: dict, action_ts: datetime) -> float | None:
    expiry = txn.get("mandate_expiry")
    if expiry is None:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return max((action_ts - expiry).days, 0)


# ---------------------------------------------------------------------------
# Arm 0 — do nothing
# ---------------------------------------------------------------------------

def _reset_simulator_rng() -> None:
    """Reset the simulator's module-level RNG to SEED so each arm run is deterministic."""
    import src.data.simulator as _sim
    _sim._rng = __import__("random").Random(SEED)


# ---------------------------------------------------------------------------
# Arm 0 — do nothing
# ---------------------------------------------------------------------------

def run_arm_zero(transactions: list[dict]) -> dict[str, bool]:
    """
    No intervention. Simulate organic recovery for every transaction.

    Returns
    -------
    {txn_id: recovered_bool}
    """
    _reset_simulator_rng()
    now = _now()
    results: dict[str, bool] = {}

    for txn in transactions:
        recovered = simulate_outcome(
            failure_code=txn["failure_code"],
            action="do_nothing",
            timestamp=now,
            opted_out=txn["opted_out"],
            txn_created_at=txn.get("created_at"),
        )
        results[txn["txn_id"]] = recovered

    return results


# ---------------------------------------------------------------------------
# Arm A — naive retry policy
# ---------------------------------------------------------------------------

def run_arm_a(transactions: list[dict]) -> dict[str, bool]:
    """
    Naive policy: retry_same at T+1h, retry_same at T+24h,
    then send generic English SMS.  First success wins.

    Returns
    -------
    {txn_id: recovered_bool}
    """
    rng = random.Random(SEED)
    now = _now()

    # Fixed action sequence regardless of failure type
    attempts = [
        ("retry_same", now + timedelta(hours=1)),
        ("retry_same", now + timedelta(hours=24)),
        ("send_sms",   now + timedelta(hours=24)),
    ]

    results: dict[str, bool] = {}

    for txn in transactions:
        recovered = False
        for action, ts in attempts:
            recovered = simulate_outcome(
                failure_code=txn["failure_code"],
                action=action,
                timestamp=ts,
                opted_out=txn["opted_out"],
                txn_created_at=txn.get("created_at"),
            )
            if recovered:
                break
        results[txn["txn_id"]] = recovered

    return results


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _summarise(label: str, results: dict[str, bool], transactions: list[dict]) -> dict:
    """Print and return recovery stats."""
    txn_by_id = {t["txn_id"]: t for t in transactions}
    recovered_amt = sum(
        txn_by_id[tid]["amount"]
        for tid, ok in results.items()
        if ok
    )
    total_amt = sum(t["amount"] for t in transactions)
    n_recovered = sum(results.values())
    n_total = len(results)
    pct = n_recovered / n_total * 100

    print(f"{label:<28} recovered {n_recovered:>3} of {n_total}"
          f" = {pct:5.1f}%   Rs.{recovered_amt:>11,.0f}"
          f"  (of Rs.{total_amt:,.0f} at risk)")

    return {
        "label":         label,
        "recovered":     n_recovered,
        "total":         n_total,
        "pct":           round(pct, 2),
        "amount_recovered": round(recovered_amt, 2),
        "amount_at_risk":   round(total_amt, 2),
    }


# ---------------------------------------------------------------------------
# Standalone entry point — checkpoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.data.database import (
        init_db, SessionLocal, GroundTruthSessionLocal,
    )
    from src.data.generator import generate

    init_db()
    main_db = SessionLocal()
    gt_db   = GroundTruthSessionLocal()

    try:
        # Seed DB if empty
        from src.data.database import Transaction
        if main_db.query(Transaction).count() == 0:
            print("[arms] DB empty — running generator first ...")
            generate(main_db, gt_db)

        transactions = _load_all_transactions(main_db, gt_db)
        print(f"[arms] Loaded {len(transactions)} transactions "
              f"({sum(1 for t in transactions if t['source']=='training')} training + "
              f"{sum(1 for t in transactions if t['source']=='holdout')} holdout)\n")

        # Run arms
        arm0_results = run_arm_zero(transactions)
        armA_results = run_arm_a(transactions)

        print("=" * 75)
        print("BASELINE RESULTS  (seed=42, deterministic)")
        print("=" * 75)
        s0 = _summarise("Arm 0 (do nothing)",   arm0_results, transactions)
        sA = _summarise("Arm A (naive retry)",   armA_results, transactions)
        print("-" * 75)

        uplift_pct = sA["pct"] - s0["pct"]
        uplift_rs  = sA["amount_recovered"] - s0["amount_recovered"]
        print(f"  Arm A uplift over Arm 0:  +{uplift_pct:.1f}pp   "
              f"Rs.+{uplift_rs:,.0f}")
        print()
        print("  Arm B (your agent) target: TBD — fill in at Phase 10")
        print("=" * 75)

        # Reproducibility check
        print("\n[arms] Reproducibility check (re-running both arms) ...")
        arm0_check = run_arm_zero(transactions)
        armA_check = run_arm_a(transactions)
        assert arm0_results == arm0_check, "Arm 0 is NOT deterministic!"
        assert armA_results == armA_check, "Arm A is NOT deterministic!"
        print("[arms] PASSED — both arms are fully deterministic with seed=42")

    finally:
        main_db.close()
        gt_db.close()
