"""
CustomerSimulator — the ground-truth oracle for ReviveAI.

RULES
-----
This file encodes the recovery probability for every
(failure_code, action, timing) combination.  These rules are:
  1. Written to ground_truth.db at generation time.
  2. Used by the eval harness to resolve actual outcomes.
  3. NEVER imported by any agent module (enforced by convention;
     the guard at the bottom raises if imported from agent paths).

SALARY WINDOW
-------------
Customers receive salary on the 25th–31st and 1st–5th of each month.
Transactions created on those days are "after_salary"; others are "before".

TIMING (bank_server_down)
--------------------------
For bank failures the relevant window is time-since-failure, not salary:
  < 30 min  → retry_quick  → 8%
  30 min–2h → retry_2h     → 74%
  > 2h      → do_nothing   → 31% organic
"""

import json
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Guard: agent modules must not import this file
# ---------------------------------------------------------------------------
_CALLER_CHECK = os.getenv("ALLOW_SIMULATOR_IMPORT", "false").lower()

# Guard: agent modules must not import this file.
# Enforced by checking the importer's module name at import time.
import sys as _sys

def _check_caller() -> None:
    """Raise if an agent module is importing us."""
    # When run as __main__ the frame depth may be 1 — skip the check.
    try:
        frame = _sys._getframe(2)
    except ValueError:
        return
    caller_file: str = frame.f_globals.get("__file__", "") or ""
    _AGENT_PATHS = (
        "triage", "strategy", "execution",
        "gates", "intelligence", "webhooks",
    )
    if any(p in caller_file for p in _AGENT_PATHS):
        if _CALLER_CHECK != "true":
            raise ImportError(
                "simulator.py must not be imported by agent modules. "
                "Set ALLOW_SIMULATOR_IMPORT=true only in the eval harness."
            )

_check_caller()
del _check_caller


SEED = int(os.getenv("SEED", "42"))
_rng = random.Random(SEED)


# ---------------------------------------------------------------------------
# Salary window helper
# ---------------------------------------------------------------------------

def _after_salary_window(ts: datetime) -> bool:
    """True if ts falls in the salary credit window (25th–31st or 1st–5th)."""
    d = ts.day
    return d >= 25 or d <= 5


# ---------------------------------------------------------------------------
# Core probability table
# ---------------------------------------------------------------------------

def get_recovery_probability(
    failure_code: str,
    action: str,
    timestamp: datetime,
    opted_out: bool = False,
    txn_created_at: Optional[datetime] = None,
    notes: Optional[dict] = None,
) -> float:
    """
    Return the recovery probability for a given (failure_code, action, context).

    Parameters
    ----------
    failure_code    : e.g. "INSUFFICIENT_FUNDS"
    action          : e.g. "payment_link", "retry_same", "do_nothing"
    timestamp       : when the action is being taken
    opted_out       : if True, always 0.0 (compliance hard-stop)
    txn_created_at  : needed for bank_server_down timing
    notes           : parsed JSON from Transaction.notes (adversarial metadata)
    """
    # Hard rule 1: opted-out customer → 0% for ANY action
    if opted_out:
        return 0.0

    fc = failure_code.upper()
    act = action.lower()
    after_salary = _after_salary_window(timestamp)

    # Hard rule 2: mandate failures → only reauth_flow works
    if fc == "MANDATE_EXPIRED":
        if act == "reauth_flow":
            return 0.68
        return 0.0   # any other action on mandate → 0%

    # -- INSUFFICIENT_FUNDS --------------------------------------------------
    if fc == "INSUFFICIENT_FUNDS":
        if act == "do_nothing":
            return 0.08
        if act == "retry_same":
            return 0.61 if after_salary else 0.12
        if act == "payment_link":
            return 0.71 if after_salary else 0.38
        # any other action treated as retry_same fallback
        return 0.61 if after_salary else 0.12

    # -- BANK_SERVER_DOWN ----------------------------------------------------
    if fc == "BANK_SERVER_DOWN":
        if act == "do_nothing":
            return 0.31   # bank recovers organically
        if act in ("retry_quick", "retry_under_30min", "retry_same"):
            # check actual elapsed time if txn_created_at is available
            if txn_created_at is not None:
                elapsed = (timestamp - txn_created_at).total_seconds() / 60
                if elapsed < 30:
                    return 0.08
                if elapsed <= 120:
                    return 0.74
                return 0.31   # too late — treat as organic
            return 0.08   # assume quick retry if no timing info
        if act in ("retry_2h_window", "retry_delayed"):
            return 0.74
        return 0.31

    # -- VPA_NOT_FOUND -------------------------------------------------------
    if fc == "VPA_NOT_FOUND":
        if act == "do_nothing":
            return 0.04
        if act == "retry_same":
            return 0.06
        if act == "payment_link":
            return 0.71
        if act == "update_vpa_flow":
            return 0.83
        return 0.06

    # -- AUTH_FAILURE --------------------------------------------------------
    if fc == "AUTH_FAILURE":
        if act == "do_nothing":
            return 0.05
        if act in ("retry", "retry_same"):
            return 0.22
        if act == "payment_link":
            return 0.58
        return 0.22

    # -- CARD_EXPIRED --------------------------------------------------------
    if fc == "CARD_EXPIRED":
        if act == "retry":
            return 0.00   # expired card cannot be retried
        if act == "retry_same":
            return 0.00
        if act == "payment_method_update":
            return 0.82
        if act == "payment_link":
            return 0.45
        if act == "do_nothing":
            return 0.02
        return 0.00

    # -- LIMIT_EXCEEDED ------------------------------------------------------
    if fc == "LIMIT_EXCEEDED":
        if act == "do_nothing":
            return 0.05
        if act in ("payment_link", "payment_link_full"):
            return 0.34
        if act == "split_payment":
            return 0.68
        return 0.15

    # Unknown failure code — conservative default
    return 0.05


# ---------------------------------------------------------------------------
# Probabilistic outcome resolver
# ---------------------------------------------------------------------------

def simulate_outcome(
    failure_code: str,
    action: str,
    timestamp: datetime,
    opted_out: bool = False,
    txn_created_at: Optional[datetime] = None,
    notes: Optional[dict] = None,
) -> bool:
    """
    Roll the dice and return True (recovered) or False (not recovered).

    The same SEED ensures reproducibility across runs.
    """
    prob = get_recovery_probability(
        failure_code=failure_code,
        action=action,
        timestamp=timestamp,
        opted_out=opted_out,
        txn_created_at=txn_created_at,
        notes=notes,
    )
    return _rng.random() < prob


# ---------------------------------------------------------------------------
# Ground-truth writer
# ---------------------------------------------------------------------------

def write_ground_truth(
    gt_db,
    transaction_id: int,
    failure_code: str,
    opted_out: bool,
    notes: Optional[dict] = None,
) -> None:
    """
    Pre-compute and persist the oracle data for one transaction.

    'recovery_probability' stored = probability under the OPTIMAL action,
    so the eval harness can measure how close the agent got.
    """
    from src.data.database import GroundTruth

    # Determine optimal probability (best possible action, after_salary)
    optimal_ts = _make_after_salary_ts()
    optimal_action = _optimal_action(failure_code)
    optimal_prob = get_recovery_probability(
        failure_code=failure_code,
        action=optimal_action,
        timestamp=optimal_ts,
        opted_out=opted_out,
        notes=notes,
    )
    recoverable = optimal_prob > 0.10

    # Check if row already exists (idempotent)
    existing = gt_db.query(GroundTruth).filter_by(transaction_id=transaction_id).first()
    if existing:
        return

    gt = GroundTruth(
        transaction_id=transaction_id,
        recoverable=recoverable,
        recovery_probability=optimal_prob,
        actual_outcome=None,   # set by eval harness after agent acts
        reason=(
            f"failure={failure_code} opted_out={opted_out} "
            f"optimal_action={optimal_action} prob={optimal_prob:.2f}"
        ),
    )
    gt_db.add(gt)
    gt_db.commit()


# ---------------------------------------------------------------------------
# Helpers for ground-truth writer
# ---------------------------------------------------------------------------

_OPTIMAL_ACTION_MAP = {
    "INSUFFICIENT_FUNDS": "payment_link",
    "BANK_SERVER_DOWN": "retry_2h_window",
    "VPA_NOT_FOUND": "update_vpa_flow",
    "AUTH_FAILURE": "payment_link",
    "CARD_EXPIRED": "payment_method_update",
    "MANDATE_EXPIRED": "reauth_flow",
    "LIMIT_EXCEEDED": "split_payment",
}

def _optimal_action(failure_code: str) -> str:
    return _OPTIMAL_ACTION_MAP.get(failure_code.upper(), "payment_link")


def _make_after_salary_ts() -> datetime:
    """Return a datetime guaranteed to be in the salary window (27th of this month)."""
    now = datetime.now(timezone.utc)
    try:
        return now.replace(day=27)
    except ValueError:
        return now.replace(day=1) + timedelta(days=26)


# ---------------------------------------------------------------------------
# Pretty-print probability table
# ---------------------------------------------------------------------------

def print_probability_table() -> None:
    after = _make_after_salary_ts()
    before = after.replace(day=15)

    headers = [
        ("retry_same",            "retry_same"),
        ("payment_link",          "payment_link"),
        ("do_nothing",            "do_nothing"),
        ("update_vpa_flow",       "update_vpa_flow"),
        ("payment_method_update", "payment_method_update"),
        ("reauth_flow",           "reauth_flow"),
        ("split_payment",         "split_payment"),
        ("retry_2h_window",       "retry_2h_window"),
    ]

    failure_codes = list(_OPTIMAL_ACTION_MAP.keys())

    col_w = 24
    hdr = f"{'Failure Code':<25}" + "".join(f"{h:<{col_w}}" for _, h in headers)
    print("\n" + "=" * len(hdr))
    print("RECOVERY PROBABILITY TABLE  (after_salary / before_salary)")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    for fc in failure_codes:
        row = f"{fc:<25}"
        for action, _ in headers:
            pa = get_recovery_probability(fc, action, after)
            pb = get_recovery_probability(fc, action, before)
            cell = f"{pa:.0%}/{pb:.0%}"
            row += f"{cell:<{col_w}}"
        print(row)

    print("=" * len(hdr))
    print("Format: after_salary / before_salary")
    print(f"Opted-out customer: 0% for ALL actions (hard compliance rule)")
    print(f"MANDATE_EXPIRED + any != reauth_flow: 0% (hard rule)")
    print()


# ---------------------------------------------------------------------------
# Standalone entry point — checkpoint:
#   python src/data/simulator.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.data.database import (
        init_db,
        SessionLocal,
        GroundTruthSessionLocal,
        Transaction,
        Customer,
    )
    from src.data.generator import generate

    print("[simulator] Initialising databases …")
    init_db()

    main_db = SessionLocal()
    gt_db = GroundTruthSessionLocal()

    try:
        # Generate synthetic data if tables are empty
        count = main_db.query(Transaction).count()
        if count == 0:
            print("[simulator] No transactions found — running generator …")
            generate(main_db)
        else:
            print(f"[simulator] Found {count} existing transactions — skipping generator.")

        # Write ground truth for every transaction
        txns = (
            main_db.query(Transaction, Customer)
            .join(Customer, Transaction.customer_id == Customer.id)
            .all()
        )
        print(f"[simulator] Writing ground truth for {len(txns)} transactions …")
        written = 0
        for txn, customer in txns:
            notes = json.loads(txn.notes) if txn.notes else None
            write_ground_truth(
                gt_db=gt_db,
                transaction_id=txn.id,
                failure_code=txn.failure_code or "UNKNOWN",
                opted_out=customer.opted_out,
                notes=notes,
            )
            written += 1

        print(f"[simulator] Ground truth written for {written} transactions.")

        # Print the probability table
        print_probability_table()

    finally:
        main_db.close()
        gt_db.close()
