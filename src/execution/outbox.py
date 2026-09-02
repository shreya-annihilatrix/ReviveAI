from datetime import datetime, timezone
import time
import argparse
import sys
from src.data.database import SessionLocal, Outbox, Transaction, init_db

def _now():
    return datetime.now(timezone.utc)


def get_pending_items(db, limit: int = 50):
    """Return up to `limit` PENDING outbox rows in insertion order."""
    return db.query(Outbox).filter_by(status="PENDING").order_by(Outbox.id).limit(limit).all()


def mark_dispatched(db, item) -> None:
    """Mark an outbox item as DISPATCHED."""
    item.status = "DISPATCHED"
    db.commit()


def mark_failed(db, item) -> None:
    """Mark an outbox item as FAILED."""
    item.status = "FAILED"
    db.commit()

def dispatcher_loop():
    from src.execution.state_machine import transition
    from src.execution import razorpay_client
    
    db = SessionLocal()
    print("[Dispatcher] Starting outbox polling loop...")
    try:
        while True:
            pending = db.query(Outbox).filter_by(status='PENDING').limit(10).all()
            for item in pending:
                print(f"[Dispatcher] Processing outbox item {item.id}, action: {item.action_type}")
                try:
                    # Fetch txn for details
                    txn = db.query(Transaction).filter_by(id=item.transaction_id).first()
                    
                    if item.action_type == "payment_link":
                        from src.execution.idempotency import get_or_create_idempotency_key
                        idem_key = get_or_create_idempotency_key(db, txn.id, item.recovery_attempt_id, "payment_link")
                        result = razorpay_client.create_payment_link(txn, idem_key)
                    elif item.action_type == "create_order":
                        from src.execution.idempotency import get_or_create_idempotency_key
                        idem_key = get_or_create_idempotency_key(db, txn.id, item.recovery_attempt_id, "create_order")
                        result = razorpay_client.create_order(txn, idem_key)
                    else:
                        result = {"status": "mocked", "note": "unsupported action in dispatcher for now"}
                        
                    item.status = 'DISPATCHED'
                    item.payload = str(result)
                    db.commit()
                    transition(db, item.transaction_id, "ACTION_PENDING", "ACTION_SENT", metadata="Dispatched")
                except Exception as e:
                    print(f"[Dispatcher] Error: {e}")
                    item.status = 'FAILED'
                    item.payload = str(e)
                    db.commit()
            time.sleep(2)
    except KeyboardInterrupt:
        print("Stopping dispatcher")
    finally:
        db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Run Razorpay outbox demo")
    args = parser.parse_args()
    
    if args.demo:
        init_db()
        db = SessionLocal()
        
        from src.execution.state_machine import transition
        from src.execution import razorpay_client
        from src.execution.idempotency import get_or_create_idempotency_key
        from src.data.database import RecoveryAttempt
        
        print("Creating a dummy transaction and outbox item for demo...")
        
        # Create a dummy transaction
        txn = Transaction(
            merchant_id=1, customer_id=1, amount=100.00, currency="INR",
            status="AT_RISK"
        )
        db.add(txn)
        db.commit()
        
        # Follow the state machine
        transition(db, txn.id, "AT_RISK", "TRIAGED")
        transition(db, txn.id, "TRIAGED", "STRATEGY_SELECTED")
        transition(db, txn.id, "STRATEGY_SELECTED", "ACTION_PENDING")
            
        # Create a recovery attempt (which idempotency needs)
        idem_key = get_or_create_idempotency_key(db, txn.id, 999, "payment_link")
        attempt = db.query(RecoveryAttempt).filter_by(idempotency_key=idem_key).first()
        
        # Add outbox item
        outbox_item = Outbox(
            transaction_id=txn.id,
            recovery_attempt_id=attempt.id,
            action_type="payment_link",
            payload="{}",
            status="PENDING",
            created_at=_now()
        )
        db.add(outbox_item)
        db.commit()
        
        print("Demo Outbox item created. Processing...")
        
        try:
            print(f"Calling Razorpay create_payment_link with idempotency {idem_key}...")
            result = razorpay_client.create_payment_link(txn, idem_key)
            
            outbox_item.status = 'DISPATCHED'
            db.commit()
            transition(db, outbox_item.transaction_id, "ACTION_PENDING", "ACTION_SENT")
            
            print(f"Success! Payment Link ID: {result.get('id')}")
            print(f"Short URL: {result.get('short_url')}")
            print("\nCheckpoint Demo complete. Check your dashboard at dashboard.razorpay.com -> Payment Links")
        except Exception as e:
            print("Razorpay API Error:", e)
        finally:
            db.close()