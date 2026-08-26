"""
Triage Cascade — Phase 5 main orchestrator.

Flow per transaction:
  1. Cache check          (0ms,  $0)
  2. Tier 1 rules         (0.1ms, $0)    ~85% stop here
  3. Tier 2 Haiku         (~800ms, ~$0.001)
  4. Tier 3 Sonnet        (~2s,   ~$0.01)  only if amount>10k or conf<0.65

CLI:
  python -m src.triage.cascade --eval
      Runs on all 36 holdout records, prints confusion matrix,
      saves PNG, writes to eval_results table.

  python -m src.triage.cascade --txn-id 42
      Triages a single transaction from the training set.
"""

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Failure-code → canonical failure_type mapping
# (used to derive true labels from holdout failure_code)
# ---------------------------------------------------------------------------

FAILURE_CODE_TO_TYPE: dict[str, str] = {
    "INSUFFICIENT_FUNDS":      "insufficient_funds",
    "BANK_SERVER_DOWN":        "bank_degradation",
    "VPA_NOT_FOUND":           "vpa_invalid",
    "VPA_DUPLICATE":           "vpa_invalid",
    "AUTH_FAILURE":            "auth_failure",
    "CARD_EXPIRED":            "expired_instrument",
    "MANDATE_EXPIRED":         "mandate_failure",
    "LIMIT_EXCEEDED":          "limit_exceeded",
    "CHECKOUT_ABANDONED":      "unknown",
    "INTL_CARD_BLOCKED":       "auth_failure",
    "UPI_PIN_LOCKED":          "auth_failure",
    "CARD_NETWORK_TIMEOUT":    "bank_degradation",
    "VELOCITY_CHECK_FAILED":   "limit_exceeded",
    "BENEFICIARY_UNREACHABLE": "bank_degradation",
    "U16":  "limit_exceeded",
    "U30":  "vpa_invalid",
    "U69":  "bank_degradation",
    "BR":   "insufficient_funds",
}

ALL_FAILURE_TYPES = [
    "insufficient_funds", "bank_degradation", "vpa_invalid",
    "auth_failure", "limit_exceeded", "expired_instrument",
    "mandate_failure", "unknown",
]


# ---------------------------------------------------------------------------
# Main triage function
# ---------------------------------------------------------------------------

def triage_transaction(txn_data: dict, db) -> dict:
    """
    Classify one transaction through the 3-tier cascade.

    Parameters
    ----------
    txn_data : dict with keys:
        txn_id, failure_code, payment_method, bank, amount,
        opted_out, failure_reason, customer_lifetime_value,
        previous_successful_payments, previous_failed_payments,
        previous_recoveries, inferred_salary_window,
        mandate_expiry, order_notes, preferred_language
    db : SQLAlchemy session (main DB)

    Returns
    -------
    dict: {failure_type, root_cause, confidence, recommended_channel,
           reasoning, source, tier}
    """
    from src.triage import cache as triage_cache
    from src.triage import rules as triage_rules
    from src.triage.llm_triage import triage_with_llm

    fc  = txn_data.get("failure_code", "")
    pm  = txn_data.get("payment_method", "")
    bk  = txn_data.get("bank", "")
    txn_id = str(txn_data.get("txn_id", "unknown"))

    # ── Step 1: Cache ────────────────────────────────────────────────────────
    cached = triage_cache.get(db, fc, pm, bk)
    if cached:
        _write_triage_result(db, txn_data, cached, source="cache")
        return {**cached, "tier": "cache"}

    # ── Step 2: Deterministic rules ──────────────────────────────────────────
    rule = triage_rules.lookup(fc, pm, bk)
    if rule and rule["confidence"] >= triage_rules.RULES_CONFIDENCE_THRESHOLD:
        result = {
            **rule,
            "reasoning": (
                f"Deterministic rule match: {fc} via {pm}. "
                f"Root cause: {rule['root_cause']}. "
                f"Confidence {rule['confidence']:.0%}."
            ),
            "source": "rules",
        }
        _write_triage_result(db, txn_data, result, source="rules")
        triage_cache.write(
            db, fc, pm, bk,
            classification=result["failure_type"],
            recovery_probability=result["confidence"],
            explanation=result["reasoning"],
        )
        return {**result, "tier": "rules"}

    # ── Step 3+4: LLM (Haiku → Sonnet if needed) ────────────────────────────
    llm_output, tier_used = triage_with_llm(
        txn_data=txn_data,
        db=db,
        txn_id=txn_id,
        force_sonnet=False,
    )
    result = {
        "failure_type":        llm_output.failure_type,
        "root_cause":          llm_output.root_cause,
        "confidence":          llm_output.confidence,
        "recommended_channel": llm_output.recommended_channel,
        "reasoning":           llm_output.reasoning,
        "source":              tier_used,
    }
    _write_triage_result(db, txn_data, result, source=tier_used)
    triage_cache.write(
        db, fc, pm, bk,
        classification=result["failure_type"],
        recovery_probability=result["confidence"],
        explanation=result["reasoning"],
    )
    return {**result, "tier": tier_used}


# ---------------------------------------------------------------------------
# DB write helper
# ---------------------------------------------------------------------------

def _write_triage_result(db, txn_data: dict, result: dict, source: str) -> None:
    """Persist the triage result to triage_results table."""
    from src.data.database import TriageResult

    txn_id_raw = txn_data.get("txn_id")
    # Only write for integer txn_ids (training set rows)
    try:
        txn_id_int = int(txn_id_raw)
    except (TypeError, ValueError):
        return   # holdout IDs like "hld_3" — skip main DB write

    row = TriageResult(
        transaction_id=txn_id_int,
        classification=result.get("failure_type", "unknown"),
        recovery_probability=result.get("confidence", 0.0),
        priority=_derive_priority(result),
        reason=result.get("reasoning", ""),
        source=source,
    )
    db.add(row)
    db.commit()


def _derive_priority(result: dict) -> str:
    conf = result.get("confidence", 0.0)
    ft   = result.get("failure_type", "")
    if ft in ("mandate_failure", "expired_instrument"):
        return "HIGH"
    if conf >= 0.90:
        return "HIGH"
    if conf >= 0.70:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Evaluation on holdout
# ---------------------------------------------------------------------------

def run_eval(main_db, gt_db) -> dict:
    """
    Run triage on all 36 holdout records and compute confusion matrix.

    Returns
    -------
    dict: {accuracy, per_class, confusion_matrix_path}
    """
    from src.data.database import EvalHoldout, EvalResult

    holdout_rows = gt_db.query(EvalHoldout).all()
    if not holdout_rows:
        log.error("No holdout records found. Run the generator first.")
        sys.exit(1)

    run_id = str(uuid.uuid4())
    predictions: list[tuple[str, str, str, float]] = []  # (true, pred, tier, conf)

    log.info("Running eval on %d holdout records (run_id=%s) ...",
             len(holdout_rows), run_id[:8])

    for h in holdout_rows:
        txn_data = {
            "txn_id":                       f"hld_{h.id}",
            "failure_code":                 h.failure_code or "",
            "payment_method":               h.payment_method or "",
            "bank":                         h.bank or "",
            "amount":                       h.amount,
            "opted_out":                    h.opt_out_status or False,
            "failure_reason":               h.failure_reason or "",
            "customer_lifetime_value":      h.customer_lifetime_value or 0,
            "previous_successful_payments": h.previous_successful_payments or 0,
            "previous_failed_payments":     h.previous_failed_payments or 0,
            "previous_recoveries":          h.previous_recoveries or 0,
            "inferred_salary_window":       h.inferred_salary_window or "",
            "mandate_expiry":               str(h.mandate_expiry) if h.mandate_expiry else "N/A",
            "order_notes":                  h.order_notes or "none",
        }

        try:
            result = triage_transaction(txn_data, main_db)
        except Exception as exc:
            log.warning("Triage failed for hld_%s: %s", h.id, exc)
            result = {
                "failure_type": "unknown",
                "confidence": 0.0,
                "tier": "error",
            }

        true_label = FAILURE_CODE_TO_TYPE.get(
            (h.failure_code or "").upper(), "unknown"
        )
        pred_label = result.get("failure_type", "unknown")
        conf       = result.get("confidence", 0.0)
        tier       = result.get("tier", "unknown")

        predictions.append((true_label, pred_label, tier, conf))

        # Persist to eval_results
        ev = EvalResult(
            run_id=run_id,
            txn_id=f"hld_{h.id}",
            model_tier=tier,
            true_label=true_label,
            predicted_label=pred_label,
            confidence=conf,
            correct=(true_label == pred_label),
        )
        main_db.add(ev)

    main_db.commit()

    # ── Metrics ──────────────────────────────────────────────────────────────
    return _compute_and_print_metrics(predictions, run_id)


# ---------------------------------------------------------------------------
# Metrics + confusion matrix
# ---------------------------------------------------------------------------

def _compute_and_print_metrics(
    predictions: list[tuple[str, str, str, float]],
    run_id: str,
) -> dict:
    import numpy as np

    labels = ALL_FAILURE_TYPES
    n      = len(labels)
    idx    = {l: i for i, l in enumerate(labels)}

    # Build confusion matrix
    cm = np.zeros((n, n), dtype=int)
    for true, pred, _, _ in predictions:
        ti = idx.get(true, idx["unknown"])
        pi = idx.get(pred, idx["unknown"])
        cm[ti][pi] += 1

    total   = len(predictions)
    correct = sum(1 for t, p, _, _ in predictions if t == p)
    accuracy = correct / total if total else 0.0

    print()
    print("=" * 72)
    print(f"TRIAGE EVAL  run_id={run_id[:8]}  n={total}")
    print("=" * 72)
    print(f"  Overall accuracy: {correct}/{total} = {accuracy:.1%}")
    print()

    # Per-class precision and recall
    print(f"  {'Failure Type':<26} {'Precision':>10} {'Recall':>10} {'F1':>8} {'Support':>8}")
    print("  " + "-" * 64)

    worst_f1 = 1.0
    worst_class = ""
    per_class: dict[str, dict] = {}

    for i, label in enumerate(labels):
        tp = cm[i, i]
        fp = cm[:, i].sum() - tp
        fn = cm[i, :].sum() - tp
        support = int(cm[i, :].sum())

        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec  = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0

        flag = " ← LOWEST" if f1 < worst_f1 and support > 0 else ""
        if f1 < worst_f1 and support > 0:
            worst_f1 = f1
            worst_class = label

        per_class[label] = {"precision": round(prec, 3), "recall": round(rec, 3),
                            "f1": round(f1, 3), "support": support}
        print(f"  {label:<26} {prec:>10.1%} {rec:>10.1%} {f1:>8.3f} {support:>8}{flag}")

    print()
    print(f"  Lowest accuracy class: {worst_class} (F1={worst_f1:.3f}) — review rules for this class")
    print("=" * 72)

    # Tier breakdown
    tiers = {}
    for _, _, tier, _ in predictions:
        tiers[tier] = tiers.get(tier, 0) + 1
    print(f"\n  Tier breakdown: {tiers}")

    # Save confusion matrix PNG
    png_path = _save_confusion_matrix_png(cm, labels, accuracy, run_id)
    print(f"  Confusion matrix saved: {png_path}")
    print()

    return {
        "run_id":      run_id,
        "accuracy":    round(accuracy, 4),
        "total":       total,
        "correct":     correct,
        "per_class":   per_class,
        "worst_class": worst_class,
        "tiers":       tiers,
        "png":         png_path,
    }


def _save_confusion_matrix_png(cm, labels, accuracy: float, run_id: str) -> str:
    """Save confusion matrix as PNG in project root/eval/."""
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend
    import matplotlib.pyplot as plt
    import seaborn as sns
    import numpy as np

    short = [l.replace("_", "\n") for l in labels]
    fig, ax = plt.subplots(figsize=(10, 8))
    sns.heatmap(
        cm,
        annot=True,
        fmt="d",
        cmap="Blues",
        xticklabels=short,
        yticklabels=short,
        ax=ax,
        linewidths=0.5,
    )
    ax.set_xlabel("Predicted", fontsize=12)
    ax.set_ylabel("True",      fontsize=12)
    ax.set_title(
        f"Triage Confusion Matrix  (accuracy={accuracy:.1%}  run={run_id[:8]})",
        fontsize=13,
        pad=15,
    )
    plt.tight_layout()

    out_dir = Path("eval")
    out_dir.mkdir(exist_ok=True)
    png_path = str(out_dir / f"confusion_{run_id[:8]}.png")
    fig.savefig(png_path, dpi=150)
    plt.close(fig)
    return png_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description="Triage Cascade CLI")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--eval",   action="store_true",
                       help="Run evaluation on holdout set")
    group.add_argument("--txn-id", type=int,
                       help="Triage a single training transaction by ID")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    from src.data.database import (
        init_db, SessionLocal, GroundTruthSessionLocal,
    )

    init_db()
    main_db = SessionLocal()
    gt_db   = GroundTruthSessionLocal()

    try:
        if args.eval:
            metrics = run_eval(main_db, gt_db)

        elif args.txn_id:
            from src.data.database import Transaction, Customer
            row = (
                main_db.query(Transaction, Customer)
                .join(Customer, Transaction.customer_id == Customer.id)
                .filter(Transaction.id == args.txn_id)
                .first()
            )
            if row is None:
                print(f"Transaction {args.txn_id} not found.")
                sys.exit(1)

            txn, cust = row
            txn_data = {
                "txn_id":                       txn.id,
                "failure_code":                 txn.failure_code or "",
                "payment_method":               txn.payment_method or "",
                "bank":                         txn.bank or "",
                "amount":                       txn.amount,
                "opted_out":                    cust.opted_out,
                "failure_reason":               txn.failure_reason or "",
                "customer_lifetime_value":      txn.customer_lifetime_value or 0,
                "previous_successful_payments": txn.previous_successful_payments or 0,
                "previous_failed_payments":     txn.previous_failed_payments or 0,
                "previous_recoveries":          txn.previous_recoveries or 0,
                "inferred_salary_window":       txn.inferred_salary_window or "",
                "mandate_expiry":               str(txn.mandate_expiry) if txn.mandate_expiry else "N/A",
                "order_notes":                  txn.order_notes or "none",
            }

            print(f"\nTriaging transaction {args.txn_id} ...")
            result = triage_transaction(txn_data, main_db)
            print(json.dumps(result, indent=2))

    finally:
        main_db.close()
        gt_db.close()
