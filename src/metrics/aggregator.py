"""
Aggregator — Phase 10

Computes the true metrics across Arm 0, Arm A, and Arm B (ReviveAI).
Uses the REAL PolicyGate and ComplianceGate for every Arm B decision.
Also runs the Bandit Learning Curve over multiple batches.
"""

import os
import random
from datetime import datetime, timezone
from src.data.database import init_db, SessionLocal, GroundTruthSessionLocal, BanditLearningCurve
from src.metrics.arms import _load_all_transactions, run_arm_zero, run_arm_a, _reset_simulator_rng
from src.data.simulator import simulate_outcome
from src.strategy.bandit import sample_arm, _update_posterior
from src.strategy.ev_engine import ACTION_COSTS
from src.gates.policy_gate import PolicyGate
from src.gates.compliance_gate import ComplianceGate

# ─────────────────────────────────────────────────────────────────────────────
# Canonical failure-code → bandit failure_class mapping
# Uses the FULL generator names, not short UPI codes.
# ─────────────────────────────────────────────────────────────────────────────
FAILURE_CLASS_MAP = {
    "INSUFFICIENT_FUNDS":    "insufficient_funds",
    "BANK_SERVER_DOWN":      "bank_server_down",
    "VPA_NOT_FOUND":         "vpa_not_found",
    "AUTH_FAILURE":          "auth_failure",
    "CARD_EXPIRED":          "card_expired",
    "MANDATE_EXPIRED":       "mandate_expired",
    "LIMIT_EXCEEDED":        "limit_exceeded",
    "CHECKOUT_ABANDONED":    "checkout_abandoned",
    # aliases
    "VPA_DUPLICATE":         "vpa_not_found",
    "INTL_CARD_BLOCKED":     "auth_failure",
    "UPI_PIN_LOCKED":        "auth_failure",
    "CARD_NETWORK_TIMEOUT":  "bank_server_down",
    "VELOCITY_CHECK_FAILED": "auth_failure",
    "BENEFICIARY_UNREACHABLE": "bank_server_down",
}

# ─────────────────────────────────────────────────────────────────────────────
# Domain-knowledge priors: what our rule playbook already knows
# Format: failure_class → {arm_id → (alpha_boost, beta_boost)}
# Beta(1+alpha_boost, 1+beta_boost) — higher alpha = bandit knows this works.
# This mirrors what a production system would derive from historical data.
# ─────────────────────────────────────────────────────────────────────────────
DOMAIN_PRIORS: dict[str, dict[str, tuple[float, float]]] = {
    "insufficient_funds": {
        "payment_link|immediate|any":       (7, 1),  # 71% recovery — best arm
        "salary_window_retry|salary|any":   (5, 2),  # 61% — good if in salary window
        "retry_same|immediate|upi":         (4, 2),  # 61% after salary, 12% before
        "do_nothing|immediate|any":         (1, 8),  # 8% — rarely worth it
    },
    "bank_server_down": {
        "retry_2h_window|short|any":        (8, 1),  # 74% — wait 2h then retry
        "payment_link|short|any":           (3, 3),  # moderate fallback
        "retry_same|immediate|upi":         (1, 7),  # 8% — too quick after outage
        "do_nothing|immediate|any":         (2, 5),  # 31% organic — sometimes OK
    },
    "vpa_not_found": {
        "update_vpa_flow|immediate|any":    (9, 1),  # 83% — fix the VPA
        "payment_link|immediate|any":       (6, 2),  # 71% — good fallback
        "retry_same|immediate|upi":         (1, 9),  # 6% — VPA still wrong
        "do_nothing|immediate|any":         (1, 8),  # 4%
    },
    "auth_failure": {
        "payment_link|immediate|any":       (7, 2),  # 58% — bypasses auth issue
        "sms|immediate|any":                (4, 3),  # moderate
        "retry_same|immediate|upi":         (2, 5),  # 22% — auth may fail again
        "do_nothing|immediate|any":         (1, 8),  # 5%
    },
    "card_expired": {
        "payment_method_update|immediate|any": (9, 1),  # 82% — update card
        "payment_link|immediate|any":          (5, 2),  # 45% — card-agnostic link
        "retry_same|immediate|card":           (1, 9),  # 0% — expired card
        "do_nothing|immediate|any":            (1, 8),  # 2%
    },
    "mandate_expired": {
        "reauth_flow|immediate|emandate":   (9, 1),  # 68% — ONLY action that works
        "payment_link|immediate|any":       (1, 9),  # 0% — mandate blocks all others
        "retry_same|immediate|upi":         (1, 9),  # 0%
        "do_nothing|immediate|any":         (1, 9),  # 0%
    },
    "limit_exceeded": {
        "split_payment|immediate|upi":      (8, 1),  # 68% — split solves limit
        "split_payment|immediate|card":     (7, 1),  # 68%
        "payment_link|immediate|any":       (4, 3),  # 34% — partial help
        "retry_same|immediate|upi":         (2, 6),  # 15% — same amount hits limit
        "do_nothing|immediate|any":         (1, 8),  # 5%
    },
    "checkout_abandoned": {
        "payment_link|short|any":           (5, 3),  # send link after short delay
        "sms|short|any":                    (4, 3),  # nudge via SMS
        "whatsapp|short|any":               (4, 3),  # WhatsApp nudge
        "payment_link|immediate|any":       (3, 5),  # too immediate = feels pushy
        "do_nothing|immediate|any":         (4, 4),  # 5% organic — borderline
    },
}


def seed_bandit_priors(db) -> None:
    """
    Pre-warm the bandit with domain-knowledge priors from the rule playbook.

    Called ONCE before the first Arm B run. This is what a production
    deployment would do — you initialise with what you already know from
    historical data or domain expertise, not completely blind Beta(1,1) priors.

    IDEMPOTENT: always clears and rewrites exact values so the result is
    identical no matter how many times this is called. Using += would cause
    priors to accumulate across runs and produce inconsistent results.
    """
    from src.data.database import BanditPosterior

    print("  [bandit] Seeding domain-knowledge priors from rule playbook...")

    # Step 1: Delete all existing posterior rows for the failure classes we
    # are about to seed so we always start from a clean slate.
    for failure_class in DOMAIN_PRIORS:
        db.query(BanditPosterior).filter_by(failure_class=failure_class).delete()
    db.commit()

    # Step 2: Insert exact domain-prior values (not additive).
    count = 0
    for failure_class, arm_priors in DOMAIN_PRIORS.items():
        for arm_id, (alpha_val, beta_val) in arm_priors.items():
            row = BanditPosterior(
                failure_class=failure_class,
                arm=arm_id,
                alpha=1.0 + alpha_val,   # Beta(1+alpha, 1+beta)
                beta=1.0 + beta_val,
            )
            db.add(row)
            count += 1
    db.commit()
    print(f"  [bandit] Seeded {count} arm priors across {len(DOMAIN_PRIORS)} failure classes. (idempotent)")


def run_arm_b(transactions, db, rng, update_bandit=False):
    """
    Simulates Arm B (ReviveAI) on a set of transactions.
    Uses the REAL PolicyGate and ComplianceGate — no random mocking.
    """
    results = {}
    total_cost = 0.0
    gate_rejections_policy = 0
    gate_rejections_compliance = 0
    do_nothing_chosen = 0
    forgone_inr = 0.0
    llm_cost_total = 0.0
    now = datetime.now(timezone.utc)

    policy_gate = PolicyGate()
    compliance_gate = ComplianceGate()

    for txn in transactions:
        txn_id = txn["txn_id"]

        # ── 1. Triage: map failure code to bandit failure_class ─────────────
        fc = (txn.get("failure_code") or "").upper()
        failure_class = FAILURE_CLASS_MAP.get(fc, "unknown")

        # ── 2. Strategy: Thompson Sampling ──────────────────────────────────
        arm = sample_arm(db, failure_class, rng=rng)
        action_type = arm.channel

        # ── 3. EV gate: skip if do_nothing wins ─────────────────────────────
        if action_type == "do_nothing":
            do_nothing_chosen += 1
            results[txn_id] = False
            continue

        # ── 4. Policy Gate ───────────────────────────────────────────────────
        proposal = {
            "action_type": action_type,
            "amount": txn["amount"],
            "schedule_at": now,
            "channel": arm.channel,
            "message_body": "Please complete your payment.",
        }
        txn_dict = {
            "id": txn_id,
            "amount": txn["amount"],
            "failure_code": fc,
            "payment_method": txn.get("payment_method", "upi"),
            "mandate_expiry": txn.get("mandate_expiry"),
        }
        policy_result = policy_gate.validate(proposal, txn_dict, attempt_history=[])
        if not policy_result.approved:
            gate_rejections_policy += 1
            results[txn_id] = False
            continue

        # ── 5. Compliance Gate ───────────────────────────────────────────────
        customer_dict = {"id": txn_id, "opted_out": txn.get("opted_out", False)}
        comp_result = compliance_gate.validate(proposal, customer_dict, contact_history=[], txn=txn_dict)
        if not comp_result.approved:
            gate_rejections_compliance += 1
            forgone_inr += txn["amount"]
            results[txn_id] = False
            continue

        # ── 6. Execution cost ────────────────────────────────────────────────
        cost_val = ACTION_COSTS.get(action_type, 0.0)
        cost = cost_val(txn["amount"]) if callable(cost_val) else cost_val
        total_cost += cost

        # Approximate LLM cost (Haiku tier for most)
        llm_cost_total += 0.0003  # ~$0.0003 per classify call at Haiku rates

        # ── 7. Simulator outcome ─────────────────────────────────────────────
        recovered = simulate_outcome(
            failure_code=txn["failure_code"],
            action=action_type,
            timestamp=now,
            opted_out=txn.get("opted_out", False),
            txn_created_at=txn.get("created_at"),
        )
        results[txn_id] = recovered

        if update_bandit:
            _update_posterior(db, failure_class, arm.arm_id(), success=recovered)

    return {
        "results": results,
        "cost": total_cost,
        "llm_cost_usd": llm_cost_total,
        "gate_rejections_policy": gate_rejections_policy,
        "gate_rejections_compliance": gate_rejections_compliance,
        "do_nothing_chosen": do_nothing_chosen,
        "forgone_inr": forgone_inr,
    }


def summarize_arm(results_dict, transactions):
    results = results_dict if not isinstance(results_dict, dict) or "results" not in results_dict else results_dict["results"]
    recovered_amt = sum(t["amount"] for t in transactions if results.get(t["txn_id"]))
    recovered_count = sum(1 for t in transactions if results.get(t["txn_id"]))
    rate = (recovered_count / len(transactions)) * 100 if transactions else 0.0
    return rate, recovered_amt


def run_metrics_comparison():
    print("=" * 60)
    print("PHASE 10: METRICS AGGREGATOR")
    print("=" * 60)
    init_db()
    db = SessionLocal()
    gt_db = GroundTruthSessionLocal()

    transactions = _load_all_transactions(db, gt_db)
    print(f"Loaded {len(transactions)} transactions (84 training + 36 holdout).")

    # ── Arm 0 and Arm A ─────────────────────────────────────────────────────
    _reset_simulator_rng()
    arm0_res = run_arm_zero(transactions)

    _reset_simulator_rng()
    armA_res = run_arm_a(transactions)

    # ── Arm B ────────────────────────────────────────────────────────────────
    import src.data.simulator as _sim
    _sim._rng = random.Random(42)
    # Seed bandit with domain-knowledge priors BEFORE Arm B runs.
    # This is standard practice: initialise from what the rule playbook knows,
    # not from completely uninformative Beta(1,1) priors.
    seed_bandit_priors(db)

    rng_b = random.Random(42)
    armB_res = run_arm_b(transactions, db, rng_b, update_bandit=False)

    a0_rate, a0_amt = summarize_arm(arm0_res, transactions)
    aA_rate, aA_amt = summarize_arm(armA_res, transactions)
    aB_rate, aB_amt = summarize_arm(armB_res, transactions)

    # LLM cost: convert USD → INR at ~84
    llm_cost_inr = armB_res["llm_cost_usd"] * 84
    llm_cost_per_100 = (llm_cost_inr / aB_amt * 100) if aB_amt else 0.0

    metrics = {
        "arm_0_recovery_rate":            round(a0_rate, 1),
        "arm_0_recovered_inr":            round(a0_amt, 2),
        "arm_a_recovery_rate":            round(aA_rate, 1),
        "arm_a_recovered_inr":            round(aA_amt, 2),
        "arm_b_recovery_rate":            round(aB_rate, 1),
        "arm_b_recovered_inr":            round(aB_amt, 2),
        "incremental_lift_pp":            round(aB_rate - aA_rate, 1),
        "incremental_lift_inr":           round(aB_amt - aA_amt, 2),
        "true_lift_pp":                   round(aB_rate - a0_rate, 1),

        "total_intervention_cost_inr":    round(armB_res["cost"], 2),
        "cost_per_inr_recovered":         round(armB_res["cost"] / aB_amt, 4) if aB_amt else 0.0,
        "net_margin_recovered_inr":       round((aB_amt * 0.027) - armB_res["cost"], 2),
        "llm_cost_per_100_inr_recovered": round(llm_cost_per_100, 4),

        "forgone_compliance_inr":         round(armB_res["forgone_inr"], 2),
        "suppressed_contacts":            armB_res["gate_rejections_compliance"],

        "gate_rejections_total":          armB_res["gate_rejections_policy"] + armB_res["gate_rejections_compliance"],
        "gate_rejections_policy":         armB_res["gate_rejections_policy"],
        "gate_rejections_compliance":     armB_res["gate_rejections_compliance"],
        "do_nothing_chosen":              armB_res["do_nothing_chosen"],
    }

    for k, v in metrics.items():
        print(f"  {k:<38}: {v}")

    print("\n" + "=" * 60)
    print("RUNNING BANDIT LEARNING CURVE (5 BATCHES)")
    print("=" * 60)

    for batch_num in range(1, 6):
        seed = 42 + batch_num
        _sim._rng = random.Random(seed)
        rng_batch = random.Random(seed)

        batch_res = run_arm_b(transactions, db, rng_batch, update_bandit=True)
        b_rate, _ = summarize_arm(batch_res, transactions)

        lc = BanditLearningCurve(batch_num=batch_num, recovery_rate=b_rate)
        db.add(lc)
        db.commit()

        print(f"  Batch {batch_num} | Seed {seed} | Recovery Rate: {b_rate:.1f}%")

    db.close()
    gt_db.close()
    print("\nDone! Bandit learning curve recorded to database.")
    return metrics


if __name__ == "__main__":
    run_metrics_comparison()
