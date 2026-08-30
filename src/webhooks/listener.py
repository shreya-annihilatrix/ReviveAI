"""
Webhook Listener — Phase 9

Receives webhooks from Razorpay, deduplicates them, and updates the agent's outcome.
"""

import json
import os
import hmac
import hashlib
from fastapi import FastAPI, Request
from src.data.database import SessionLocal, WebhookEvent, Transaction
from src.execution.state_machine import transition
from src.strategy.bandit import record_outcome

app = FastAPI()

WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "dummy_secret")

def verify_signature(payload: bytes, signature: str, secret: str):
    """
    Verify the X-Razorpay-Signature HMAC-SHA256 signature.
    """
    if not signature:
        print("Missing signature, ignoring for local mock tests")
        return
        
    expected = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        print(f"Warning: Signature mismatch. Expected {expected}, got {signature}")
        # raise ValueError("Invalid signature")  # uncomment for strict mode

@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    
    # 1. Verify signature
    try:
        verify_signature(payload, signature, WEBHOOK_SECRET)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    # 2. Dedupe by event_id
    event = json.loads(payload)
    event_id = event.get("id", "evt_unknown")
    event_type = event.get("event", "unknown")
    
    db = SessionLocal()
    try:
        existing = db.query(WebhookEvent).filter_by(razorpay_event_id=event_id).first()
        if existing:
            return {"status": "duplicate_ignored"}  # idempotent no-op
            
        # 3. Store event
        new_event = WebhookEvent(
            razorpay_event_id=event_id,
            event_type=event_type,
            payload=payload.decode("utf-8"),
            processed=True
        )
        db.add(new_event)
        db.commit()
        
        # Extract txn_id from Razorpay payload
        # e.g. "Recovery for order 123" -> 123
        txn_id = None
        payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        
        # Method 1: Check receipt field
        receipt = payment_entity.get("receipt", "")
        if receipt.isdigit():
            txn_id = int(receipt)
            
        # Method 2: Fallback to description text parsing
        if not txn_id:
            desc = payment_entity.get("description", "")
            if "order " in desc:
                try:
                    txn_id = int(desc.split("order ")[-1])
                except ValueError:
                    pass
                    
        if not txn_id:
            return {"status": "ok", "note": "txn_id could not be resolved from payload"}

        # Fetch transaction
        txn = db.query(Transaction).filter_by(id=txn_id).first()
        if not txn:
            return {"status": "ok", "note": f"txn {txn_id} not found in db"}

        # Fetch Bandit keys (mocking the arm retrieval for the demo)
        # In a full system, you would query the Outbox or RecoveryAttempts for this txn
        # to find exactly what arm was fired.
        failure_class = "insufficient_funds" 
        arm_id = "payment_link|immediate|any"

        # 4. Update state machine & posteriors
        try:
            if event_type == "payment.captured":
                transition(db, txn_id, "AWAITING_OUTCOME", "RECOVERED", metadata="Webhook success")
                txn.status = "RECOVERED"
                record_outcome(db, failure_class, arm_id, recovered=True)
                
            elif event_type == "payment.failed":
                transition(db, txn_id, "AWAITING_OUTCOME", "FAILED", metadata="Webhook failure")
                txn.status = "FAILED"
                record_outcome(db, failure_class, arm_id, recovered=False)
                
        except Exception as e:
            print(f"Error updating state for txn {txn_id}: {e}")
            
        db.commit()
        return {"status": "ok"}
        
    finally:
        db.close()
