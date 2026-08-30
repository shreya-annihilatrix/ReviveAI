"""
Main Entrypoint — Phase 9 (Crash-Resume Test)
"""

import argparse
import time
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from src.data.database import SessionLocal, Transaction, init_db
from src.execution.idempotency import get_or_create_idempotency_key
from src.execution.state_machine import transition, IllegalTransitionError

def run_batch(count: int):
    init_db()
    db = SessionLocal()
    
    # Ensure there are transactions to process
    txns = db.query(Transaction).order_by(Transaction.id).limit(count).all()
    if not txns:
        print("No transactions found in DB. Did you generate data?")
        return

    print(f"Starting batch run of {len(txns)} transactions...")
    processed_count = 0

    for i, txn in enumerate(txns):
        print(f"Processing transaction {i+1}/{len(txns)} (ID: {txn.id})")
        
        # Determine what state we are in
        # We can look at the latest transaction state
        if txn.status in ("RECOVERED", "FAILED", "ESCALATED", "ABANDONED"):
            print(f"Txn {txn.id} already finished (status={txn.status}). Skipping.")
            processed_count += 1
            continue
            
        print(f"Resuming from transaction {i+1}/{len(txns)}. Checking idempotency...")
        
        try:
            # Triage -> Strategy -> Action Pending -> Action Sent
            # (In a real system, these would be separate worker loops. For the demo, we do it inline)
            if txn.status == "AT_RISK":
                transition(db, txn.id, "AT_RISK", "TRIAGED", metadata="Triage complete")
                txn.status = "TRIAGED"
                db.commit()
                
            if txn.status == "TRIAGED":
                transition(db, txn.id, "TRIAGED", "STRATEGY_SELECTED", metadata="Strategy selected")
                txn.status = "STRATEGY_SELECTED"
                db.commit()
                
            if txn.status == "STRATEGY_SELECTED":
                # Ensure idempotency before doing action
                idem_key = get_or_create_idempotency_key(db, txn.id, 1, "mock_action")
                
                transition(db, txn.id, "STRATEGY_SELECTED", "ACTION_PENDING", metadata="Outbox pending")
                txn.status = "ACTION_PENDING"
                db.commit()
                
            if txn.status == "ACTION_PENDING":
                transition(db, txn.id, "ACTION_PENDING", "ACTION_SENT", metadata="Mock dispatched")
                txn.status = "ACTION_SENT"
                db.commit()
                
            if txn.status == "ACTION_SENT":
                transition(db, txn.id, "ACTION_SENT", "AWAITING_OUTCOME", metadata="Waiting for webhook")
                txn.status = "AWAITING_OUTCOME"
                db.commit()
                
        except IllegalTransitionError as e:
            print(f"State transition error for txn {txn.id}: {e}")
            
        processed_count += 1
        
        # Artificial delay to allow user to Ctrl+C
        time.sleep(0.5)

    print("Batch run complete.")
    db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-batch", action="store_true")
    parser.add_argument("--count", type=int, default=120)
    args = parser.parse_args()
    
    if args.run_batch:
        run_batch(args.count)
