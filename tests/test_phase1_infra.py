"""
Phase 1 infrastructure tests.

Every test uses an in-memory SQLite DB via a pytest fixture,
so nothing touches the real reviveai.db / ground_truth.db files.
"""

import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.data.database import (
    Base,
    Merchant,
    Customer,
    Transaction,
    RecoveryAttempt,
)
from src.execution.idempotency import make_idempotency_key
from src.execution.outbox import (
    create_outbox_item,
    get_pending_items,
    mark_dispatched,
    mark_failed,
)
from src.execution.state_machine import (
    validate_transition,
    record_transition,
    ALLOWED_TRANSITIONS,
)
from src.execution.dispatcher import run_dispatch_cycle
from src.execution.razorpay_client import RazorpayClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """Fresh in-memory SQLite session, rolled back after each test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.rollback()
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_db(db):
    """DB session pre-loaded with one merchant, customer, transaction, and attempt."""
    merchant = Merchant(name="Acme", margin_rate=0.02)
    db.add(merchant)
    db.flush()

    customer = Customer(merchant_id=merchant.id, external_id="C001", opted_out=False)
    db.add(customer)
    db.flush()

    txn = Transaction(
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount=1500.0,
        status="AT_RISK",
        failure_code="INSUFFICIENT_FUNDS",
        payment_method="UPI",
        bank="HDFC",
    )
    db.add(txn)
    db.flush()

    key = make_idempotency_key(txn.id, 1, "SEND_LINK")
    attempt = RecoveryAttempt(
        transaction_id=txn.id,
        attempt_no=1,
        action_type="SEND_LINK",
        channel="whatsapp",
        idempotency_key=key,
        status="PENDING",
    )
    db.add(attempt)
    db.flush()

    return {"db": db, "merchant": merchant, "customer": customer, "txn": txn, "attempt": attempt}


@pytest.fixture()
def mock_client():
    """RazorpayClient forced into mock mode."""
    return RazorpayClient(mock=True)


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_key_is_64_chars(self):
        key = make_idempotency_key(1, 1, "SEND_LINK")
        assert len(key) == 64

    def test_key_is_hex(self):
        key = make_idempotency_key(1, 1, "SEND_LINK")
        int(key, 16)  # raises ValueError if not valid hex

    def test_same_inputs_same_key(self):
        a = make_idempotency_key(42, 3, "SEND_SMS")
        b = make_idempotency_key(42, 3, "SEND_SMS")
        assert a == b

    def test_different_inputs_different_keys(self):
        a = make_idempotency_key(1, 1, "SEND_LINK")
        b = make_idempotency_key(1, 2, "SEND_LINK")
        c = make_idempotency_key(1, 1, "SEND_SMS")
        assert a != b
        assert a != c
        assert b != c


# ---------------------------------------------------------------------------
# Outbox tests
# ---------------------------------------------------------------------------

class TestOutbox:
    def test_create_item_is_pending(self, seeded_db):
        d = seeded_db
        item = create_outbox_item(
            d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", '{"phone":"9999999999"}'
        )
        assert item.id is not None
        assert item.status == "PENDING"
        assert item.dispatched_at is None

    def test_get_pending_returns_created_item(self, seeded_db):
        d = seeded_db
        create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        pending = get_pending_items(d["db"])
        assert len(pending) == 1
        assert pending[0].status == "PENDING"

    def test_mark_dispatched(self, seeded_db):
        d = seeded_db
        item = create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        mark_dispatched(d["db"], item)
        assert item.status == "DISPATCHED"
        assert item.dispatched_at is not None

    def test_dispatched_item_not_in_pending(self, seeded_db):
        d = seeded_db
        item = create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        mark_dispatched(d["db"], item)
        pending = get_pending_items(d["db"])
        assert len(pending) == 0

    def test_mark_failed(self, seeded_db):
        d = seeded_db
        item = create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        mark_failed(d["db"], item)
        assert item.status == "FAILED"

    def test_failed_item_not_in_pending(self, seeded_db):
        d = seeded_db
        item = create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        mark_failed(d["db"], item)
        pending = get_pending_items(d["db"])
        assert len(pending) == 0

    def test_pending_limit(self, seeded_db):
        d = seeded_db
        db = d["db"]
        # Create 5 items using distinct idempotency keys
        for i in range(2, 7):
            key = make_idempotency_key(d["txn"].id, i, "SEND_LINK")
            att = RecoveryAttempt(
                transaction_id=d["txn"].id,
                attempt_no=i,
                action_type="SEND_LINK",
                channel="sms",
                idempotency_key=key,
                status="PENDING",
            )
            db.add(att)
            db.flush()
            create_outbox_item(db, d["txn"].id, att.id, "SEND_LINK", "{}")
        pending = get_pending_items(db, limit=3)
        assert len(pending) == 3


# ---------------------------------------------------------------------------
# State machine tests
# ---------------------------------------------------------------------------

class TestStateMachine:
    LEGAL = [
        ("AT_RISK", "TRIAGED"),
        ("TRIAGED", "STRATEGY_SELECTED"),
        ("STRATEGY_SELECTED", "ACTION_PENDING"),
        ("ACTION_PENDING", "ACTION_SENT"),
        ("ACTION_SENT", "AWAITING_OUTCOME"),
        ("AWAITING_OUTCOME", "RECOVERED"),
        ("AWAITING_OUTCOME", "FAILED"),
        ("AWAITING_OUTCOME", "ESCALATED"),
        ("AWAITING_OUTCOME", "ABANDONED"),
    ]

    ILLEGAL = [
        ("AT_RISK", "RECOVERED"),
        ("AT_RISK", "ACTION_SENT"),
        ("TRIAGED", "AT_RISK"),
        ("RECOVERED", "AT_RISK"),
        ("FAILED", "TRIAGED"),
    ]

    @pytest.mark.parametrize("frm,to", LEGAL)
    def test_legal_transitions(self, frm, to):
        assert validate_transition(frm, to) is True

    @pytest.mark.parametrize("frm,to", ILLEGAL)
    def test_illegal_transitions_raise(self, frm, to):
        with pytest.raises(ValueError):
            validate_transition(frm, to)

    def test_terminal_states_are_locked(self):
        for terminal in ("RECOVERED", "FAILED", "ESCALATED", "ABANDONED"):
            assert ALLOWED_TRANSITIONS[terminal] == set()

    def test_record_transition_inserts_row(self, seeded_db):
        d = seeded_db
        state = record_transition(
            d["db"], d["txn"].id, "AT_RISK", "TRIAGED", reason="triage complete"
        )
        assert state.id is not None
        assert state.state == "TRIAGED"
        assert state.previous_state == "AT_RISK"
        assert state.trace_id is not None

    def test_record_transition_auto_generates_trace_id(self, seeded_db):
        d = seeded_db
        state = record_transition(d["db"], d["txn"].id, "AT_RISK", "TRIAGED")
        assert len(state.trace_id) == 36  # UUID4 format

    def test_record_transition_uses_provided_trace_id(self, seeded_db):
        d = seeded_db
        state = record_transition(
            d["db"], d["txn"].id, "AT_RISK", "TRIAGED", trace_id="my-trace-123"
        )
        assert state.trace_id == "my-trace-123"

    def test_record_transition_rejects_illegal(self, seeded_db):
        d = seeded_db
        with pytest.raises(ValueError):
            record_transition(d["db"], d["txn"].id, "AT_RISK", "RECOVERED")


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_dispatch_cycle_processes_pending(self, seeded_db, mock_client):
        d = seeded_db
        create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        results = run_dispatch_cycle(d["db"], client=mock_client)
        assert results["processed"] == 1
        assert results["dispatched"] == 1
        assert results["failed"] == 0

    def test_dispatch_marks_outbox_dispatched(self, seeded_db, mock_client):
        d = seeded_db
        item = create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        run_dispatch_cycle(d["db"], client=mock_client)
        d["db"].refresh(item)
        assert item.status == "DISPATCHED"
        assert item.dispatched_at is not None

    def test_dispatch_marks_attempt_dispatched(self, seeded_db, mock_client):
        d = seeded_db
        create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        run_dispatch_cycle(d["db"], client=mock_client)
        d["db"].refresh(d["attempt"])
        assert d["attempt"].status == "DISPATCHED"
        assert d["attempt"].completed_at is not None

    def test_empty_outbox_returns_zero_counts(self, seeded_db, mock_client):
        results = run_dispatch_cycle(seeded_db["db"], client=mock_client)
        assert results == {"processed": 0, "dispatched": 0, "failed": 0}

    def test_failing_client_marks_failed(self, seeded_db):
        """A client that always fails should mark items FAILED."""
        class AlwaysFailClient(RazorpayClient):
            def dispatch(self, action_type, payload, idempotency_key):
                return {"success": False, "error": "network error", "razorpay_response": {}}

        d = seeded_db
        item = create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        results = run_dispatch_cycle(d["db"], client=AlwaysFailClient(mock=True))
        assert results["failed"] == 1
        d["db"].refresh(item)
        assert item.status == "FAILED"

    def test_idempotency_key_used_in_dispatch(self, seeded_db):
        """Verify the dispatcher passes the pre-stored idempotency key to the client."""
        received_keys = []

        class CapturingClient(RazorpayClient):
            def dispatch(self, action_type, payload, idempotency_key):
                received_keys.append(idempotency_key)
                return {"success": True, "razorpay_response": {}}

        d = seeded_db
        expected_key = d["attempt"].idempotency_key
        create_outbox_item(d["db"], d["txn"].id, d["attempt"].id, "SEND_LINK", "{}")
        run_dispatch_cycle(d["db"], client=CapturingClient(mock=True))
        assert received_keys == [expected_key]
