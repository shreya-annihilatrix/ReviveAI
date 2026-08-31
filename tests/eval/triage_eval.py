"""
tests/eval/triage_eval.py
=========================
LLM Regression Eval — runs ONLY on the 36-record eval_holdout in ground_truth.db.
The triage cascade has NEVER seen these records.

Pass criteria (CI enforced):
  overall accuracy  >= 70%
  per-class accuracy >= 50% for every class that has >= 3 support records

Usage:
  python tests/eval/triage_eval.py                       # print full report
  python tests/eval/triage_eval.py --assert-accuracy 0.70  # CI mode, exits 1 on fail
  python tests/eval/triage_eval.py --json                   # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Fix module imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# ─────────────────────────────────────────────
# Paths — override via env vars in CI
# ─────────────────────────────────────────────

GT_DB_PATH   = os.getenv("GT_DB_PATH",   "ground_truth.db")
MAIN_DB_PATH = os.getenv("DB_PATH",      "reviveai.db")

# ─────────────────────────────────────────────
# Failure code → canonical label mapping
# Maps generator uppercase codes → triage output labels
# ─────────────────────────────────────────────

CANONICAL_LABEL: dict[str, str] = {
    "INSUFFICIENT_FUNDS":     "INSUFFICIENT_FUNDS",
    "BANK_SERVER_DOWN":       "BANK_SERVER_DOWN",
    "VPA_NOT_FOUND":          "VPA_NOT_FOUND",
    "AUTH_FAILURE":           "AUTH_FAILURE",
    "CARD_EXPIRED":           "CARD_EXPIRED",
    "MANDATE_EXPIRED":        "MANDATE_EXPIRED",
    "LIMIT_EXCEEDED":         "LIMIT_EXCEEDED",
    "CHECKOUT_ABANDONED":     "CHECKOUT_ABANDONED",
    "VPA_DUPLICATE":          "VPA_NOT_FOUND",       # triage sees it as VPA issue
    "INTL_CARD_BLOCKED":      "AUTH_FAILURE",        # auth variant
    "UPI_PIN_LOCKED":         "AUTH_FAILURE",        # auth variant
    "CARD_NETWORK_TIMEOUT":   "BANK_SERVER_DOWN",    # server-side
    "VELOCITY_CHECK_FAILED":  "AUTH_FAILURE",        # bank-side block
    "BENEFICIARY_UNREACHABLE": "BANK_SERVER_DOWN",   # network
}

# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────

@dataclass
class EvalRecord:
    txn_id:          str
    failure_code:    str
    true_label:      str         # canonical
    payment_method:  str
    amount:          float
    bank:            str
    order_notes:     str | None
    mandate_expiry:  str | None
    opted_out:       bool
    optimal_action:  str
    recoverable:     bool

@dataclass
class ClassMetrics:
    label:     str
    tp:        int = 0
    fp:        int = 0
    fn:        int = 0
    support:   int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        return self.tp / self.support if self.support else 0.0


@dataclass
class EvalReport:
    overall_accuracy:  float
    n_total:           int
    n_correct:         int
    per_class:         dict[str, ClassMetrics]
    failures:          list[dict]            # misclassified records
    passed:            bool
    fail_reason:       str = ""


# ─────────────────────────────────────────────
# Load holdout from ground_truth.db
# ─────────────────────────────────────────────

def load_holdout() -> list[EvalRecord]:
    if not Path(GT_DB_PATH).exists():
        print(f"[eval] ❌  ground_truth.db not found at '{GT_DB_PATH}'")
        print("[eval]     Run: python -m src.data.generator  first")
        sys.exit(1)

    conn = sqlite3.connect(GT_DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            h.razorpay_payment_id  AS txn_id,
            h.failure_code,
            h.payment_method,
            h.amount,
            h.bank,
            h.order_notes,
            h.mandate_expiry,
            h.opt_out_status       AS opted_out,
            h.optimal_action,
            h.recoverable
        FROM eval_holdout h
        ORDER BY h.id
    """).fetchall()
    conn.close()

    records = []
    for r in rows:
        fc = r["failure_code"].upper()
        records.append(EvalRecord(
            txn_id         = r["txn_id"],
            failure_code   = fc,
            true_label     = CANONICAL_LABEL.get(fc, fc),
            payment_method = r["payment_method"],
            amount         = r["amount"],
            bank           = r["bank"],
            order_notes    = r["order_notes"],
            mandate_expiry = r["mandate_expiry"],
            opted_out      = bool(r["opted_out"]),
            optimal_action = r["optimal_action"],
            recoverable    = bool(r["recoverable"]),
        ))

    print(f"[eval] Loaded {len(records)} holdout records from {GT_DB_PATH}")
    return records


# ─────────────────────────────────────────────
# Run triage cascade on a single record
# Imports your actual Phase-05 implementation
# ─────────────────────────────────────────────

def run_triage(record: EvalRecord) -> str:
    """
    Returns the predicted failure_type label (uppercase, matching CANONICAL_LABEL values).
    """
    try:
        from src.triage.cascade import triage_transaction
    except ImportError as e:
        print(f"[eval] X Cannot import triage_transaction: {e}")
        print("[eval] You must complete Phase 05 before running this eval.")
        sys.exit(1)

    txn_dict = {
        "txn_id":          record.txn_id,
        "failure_code":    record.failure_code,
        "payment_method":  record.payment_method,
        "amount":          record.amount,
        "bank":            record.bank,
        "order_notes":     record.order_notes or "",
        "mandate_expiry":  record.mandate_expiry,
        "opt_out_status":  record.opted_out,
        "opted_out":       record.opted_out,
        "failure_reason":  record.failure_code
    }

    from src.data.database import SessionLocal
    db = SessionLocal()
    try:
        res = triage_transaction(txn_dict, db)
        predicted = res.get("failure_type", "unknown").upper().replace(" ", "_")
        return CANONICAL_LABEL.get(predicted, predicted)
    finally:
        db.close()


# ─────────────────────────────────────────────
# Compute metrics
# ─────────────────────────────────────────────

def compute_report(
    records:    list[EvalRecord],
    predicted:  list[str],
    min_overall: float,
    min_class:  float,
) -> EvalReport:

    labels = sorted(set(r.true_label for r in records))
    per_class: dict[str, ClassMetrics] = {l: ClassMetrics(label=l) for l in labels}

    correct  = 0
    failures = []

    for rec, pred in zip(records, predicted):
        true = rec.true_label
        per_class[true].support += 1

        if pred == true:
            per_class[true].tp += 1
            correct += 1
        else:
            per_class[true].fn += 1
            if pred in per_class:
                per_class[pred].fp += 1
            else:
                per_class[pred] = ClassMetrics(label=pred)
                per_class[pred].fp += 1

            failures.append({
                "txn_id":     rec.txn_id,
                "true":       true,
                "predicted":  pred,
                "amount":     rec.amount,
                "payment_method": rec.payment_method,
            })

    overall_acc = correct / len(records) if records else 0.0

    # Pass/fail logic
    passed     = True
    fail_reason = ""

    if overall_acc < min_overall:
        passed      = False
        fail_reason = f"Overall accuracy {overall_acc:.1%} < threshold {min_overall:.1%}"

    for label, m in per_class.items():
        if m.support >= 3 and m.accuracy < min_class:
            passed      = False
            fail_reason += (
                f"\nClass '{label}' accuracy {m.accuracy:.1%} < "
                f"threshold {min_class:.1%} (support={m.support})"
            )

    return EvalReport(
        overall_accuracy = overall_acc,
        n_total          = len(records),
        n_correct        = correct,
        per_class        = per_class,
        failures         = failures,
        passed           = passed,
        fail_reason      = fail_reason.strip(),
    )


# ─────────────────────────────────────────────
# Print helpers
# ─────────────────────────────────────────────

def print_report(report: EvalReport) -> None:
    status = "PASS" if report.passed else f"FAIL ({report.fail_reason})"

    print("=" * 60)
    print(f"  Triage Eval Report  |  {status}")
    print("=" * 60)
    print(f"  Overall accuracy : {report.overall_accuracy:.1%}  "
          f"({report.n_correct}/{report.n_total} correct)")

    if not report.passed:
        print(f"\n  Fail reason: {report.fail_reason}")

    print(f"\n{'-'*60}")
    print(f"  {'Class':<22} | {'Acc':>6} | {'Prec':>6} | {'Rec':>6} | {'F1':>6} | {'n':>3}")
    print(f"  {'-'*60}")

    for label, m in sorted(report.per_class.items()):
        if m.support == 0:
            continue
        flag = " !" if m.support >= 3 and m.accuracy < 0.50 else ""
        print(
            f"  {label:<28} "
            f"{m.accuracy:>5.1%}  "
            f"{m.precision:>5.1%}  "
            f"{m.recall:>5.1%}  "
            f"{m.f1:>5.1%}  "
            f"{m.support:>4}"
            f"{flag}"
        )

    print(f"  {'-'*60}")

    if report.failures:
        print(f"\n  Misclassified Records (first 10 of {len(report.failures)}):")
        print(f"  {'-'*60}")
        print(f"  Misclassified ({len(report.failures)} records):")
        for f in report.failures[:10]:   # show first 10
            print(f"    {f['txn_id']:<20} true={f['true']:<25} pred={f['predicted']}")
        if len(report.failures) > 10:
            print(f"    ... and {len(report.failures)-10} more")

    print(f"{'='*60}\n")


def print_confusion_matrix(records: list[EvalRecord], predicted: list[str]) -> None:
    labels = sorted(set(r.true_label for r in records) | set(predicted))
    # Only show classes that actually appear in true labels
    labels = [l for l in labels if any(r.true_label == l for r in records)]

    print(f"\n  Confusion Matrix (rows=true, cols=predicted)")
    header = "  " + " " * 28
    for l in labels:
        header += f"  {l[:6]:>6}"
    print(header)

    for true_l in labels:
        row = f"  {true_l:<28}"
        for pred_l in labels:
            count = sum(
                1 for r, p in zip(records, predicted)
                if r.true_label == true_l and p == pred_l
            )
            row += f"  {count:>6}"
        print(row)
    print()


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Triage cascade eval harness")
    parser.add_argument(
        "--assert-accuracy", type=float, default=0.70,
        help="Minimum overall accuracy to pass (default 0.70)",
    )
    parser.add_argument(
        "--min-class-accuracy", type=float, default=0.50,
        help="Minimum per-class accuracy for classes with >= 3 support (default 0.50)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output metrics as JSON (machine-readable, for CI artifacts)",
    )
    parser.add_argument(
        "--confusion-matrix", action="store_true",
        help="Print confusion matrix",
    )
    args = parser.parse_args()

    records   = load_holdout()
    predicted = []

    print(f"[eval] Running triage cascade on {len(records)} holdout records …")
    for i, rec in enumerate(records, 1):
        pred = run_triage(rec)
        predicted.append(pred)
        if i % 10 == 0:
            print(f"[eval]   {i}/{len(records)} classified …")

    report = compute_report(
        records, predicted,
        min_overall = args.assert_accuracy,
        min_class   = args.min_class_accuracy,
    )

    if args.json:
        output = {
            "overall_accuracy": report.overall_accuracy,
            "n_total":          report.n_total,
            "n_correct":        report.n_correct,
            "passed":           report.passed,
            "fail_reason":      report.fail_reason,
            "per_class":        {
                label: {
                    "accuracy":  m.accuracy,
                    "precision": m.precision,
                    "recall":    m.recall,
                    "f1":        m.f1,
                    "support":   m.support,
                }
                for label, m in report.per_class.items()
                if m.support > 0
            },
            "misclassified": report.failures,
        }
        print(json.dumps(output, indent=2))
    else:
        print_report(report)
        if args.confusion_matrix:
            print_confusion_matrix(records, predicted)

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
