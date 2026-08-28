import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "reviveai.db")

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)

Base = declarative_base()


# --------------------------------------------------
# Separate ground-truth database
# --------------------------------------------------

GROUND_TRUTH_DB_PATH = os.getenv(
    "GROUND_TRUTH_DB_PATH",
    "ground_truth.db",
)

GROUND_TRUTH_DATABASE_URL = (
    f"sqlite:///{GROUND_TRUTH_DB_PATH}"
)

ground_truth_engine = create_engine(
    GROUND_TRUTH_DATABASE_URL,
    connect_args={"check_same_thread": False},
)

GroundTruthBase = declarative_base()

GroundTruthSessionLocal = sessionmaker(
    bind=ground_truth_engine,
    autoflush=False,
    autocommit=False,
)


# --------------------------------------------------
# Merchants
# --------------------------------------------------

class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    margin_rate = Column(Float, default=0.0)
    channel_preferences = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# --------------------------------------------------
# Customers
# --------------------------------------------------

class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)

    merchant_id = Column(
        Integer,
        ForeignKey("merchants.id"),
        nullable=False,
    )

    external_id = Column(
        String(200),
        nullable=False,
    )

    payment_dna = Column(Text)
    salary_window = Column(String(100))

    opted_out = Column(
        Boolean,
        default=False,
        nullable=False,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "external_id",
            name="uq_customer_merchant_external",
        ),
    )


# --------------------------------------------------
# Transactions
# --------------------------------------------------

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)

    merchant_id = Column(
        Integer,
        ForeignKey("merchants.id"),
        nullable=False,
    )

    customer_id = Column(
        Integer,
        ForeignKey("customers.id"),
        nullable=False,
    )

    razorpay_payment_id = Column(
        String(200),
        unique=True,
    )

    amount = Column(
        Float,
        nullable=False,
    )

    currency = Column(
        String(10),
        default="INR",
    )

    payment_method = Column(String(50))
    bank = Column(String(100))

    failure_code = Column(String(100))
    failure_reason = Column(Text)

    status = Column(
        String(50),
        default="AT_RISK",
        nullable=False,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    # JSON blob — internal adversarial test metadata. Agent must NOT read this.
    notes = Column(Text)

    # Phase 3 enrichment fields — every gate and triage agent reads these
    customer_lifetime_value = Column(Float)          # ₹ total spend history
    previous_successful_payments = Column(Integer, default=0)
    previous_failed_payments = Column(Integer, default=0)
    previous_recoveries = Column(Integer, default=0)
    inferred_salary_window = Column(String(30))      # "1st-5th", "25th-31st", "6th-24th"
    mandate_expiry = Column(DateTime)                # None unless subscription/mandate txn
    order_notes = Column(Text)                       # customer-supplied — injection vector
    preferred_language = Column(String(10))          # 'hi','en','ta','te','mr','bn'


# --------------------------------------------------
# Triage results
# --------------------------------------------------

class TriageResult(Base):
    __tablename__ = "triage_results"

    id = Column(Integer, primary_key=True)

    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=False,
    )

    classification = Column(
        String(100),
        nullable=False,
    )

    recovery_probability = Column(Float)

    priority = Column(String(30))

    reason = Column(Text)

    source = Column(String(30))

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


# --------------------------------------------------
# Triage cache
# --------------------------------------------------

class TriageCache(Base):
    __tablename__ = "triage_cache"

    id = Column(Integer, primary_key=True)

    failure_code = Column(
        String(100),
        nullable=False,
    )

    payment_method = Column(
        String(50),
        nullable=False,
    )

    bank = Column(
        String(100),
        nullable=False,
    )

    classification = Column(
        String(100),
        nullable=False,
    )

    recovery_probability = Column(Float)

    explanation = Column(Text)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "failure_code",
            "payment_method",
            "bank",
            name="uq_triage_cache_key",
        ),
    )


# --------------------------------------------------
# Recovery attempts
# --------------------------------------------------

class RecoveryAttempt(Base):
    __tablename__ = "recovery_attempts"

    id = Column(Integer, primary_key=True)

    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=False,
    )

    attempt_no = Column(
        Integer,
        nullable=False,
    )

    action_type = Column(
        String(100),
        nullable=False,
    )

    channel = Column(String(50))

    idempotency_key = Column(
        String(128),
        nullable=False,
        unique=True,
    )

    status = Column(
        String(50),
        default="PENDING",
    )

    response = Column(Text)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    completed_at = Column(DateTime)


# --------------------------------------------------
# Transaction state history
# --------------------------------------------------

class TransactionState(Base):
    __tablename__ = "transaction_states"

    id = Column(Integer, primary_key=True)

    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=False,
    )

    previous_state = Column(String(50))

    state = Column(
        String(50),
        nullable=False,
    )

    trace_id = Column(
        String(100),
        nullable=False,
    )

    reason = Column(Text)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


# --------------------------------------------------
# Outbox
# --------------------------------------------------

class Outbox(Base):
    __tablename__ = "outbox"

    id = Column(Integer, primary_key=True)

    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=False,
    )

    recovery_attempt_id = Column(
        Integer,
        ForeignKey("recovery_attempts.id"),
        nullable=False,
    )

    action_type = Column(
        String(100),
        nullable=False,
    )

    payload = Column(Text)

    status = Column(
        String(30),
        default="PENDING",
        nullable=False,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    dispatched_at = Column(DateTime)


# --------------------------------------------------
# Razorpay webhook events
# --------------------------------------------------

class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True)

    razorpay_event_id = Column(
        String(200),
        nullable=False,
        unique=True,
    )

    event_type = Column(
        String(100),
        nullable=False,
    )

    payload = Column(
        Text,
        nullable=False,
    )

    processed = Column(
        Boolean,
        default=False,
        nullable=False,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


# --------------------------------------------------
# Audit log
# --------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)

    trace_id = Column(
        String(100),
        nullable=False,
    )

    transaction_id = Column(Integer)

    component = Column(
        String(100),
        nullable=False,
    )

    action = Column(
        String(100),
        nullable=False,
    )

    decision = Column(Text)

    metadata_json = Column(Text)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


# --------------------------------------------------
# Experiment arms
# --------------------------------------------------

class ExperimentArm(Base):
    __tablename__ = "experiment_arms"

    id = Column(Integer, primary_key=True)

    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=False,
    )

    arm = Column(
        String(20),
        nullable=False,
    )

    assigned_at = Column(
        DateTime,
        default=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "transaction_id",
            name="uq_experiment_transaction",
        ),
    )


# --------------------------------------------------
# Bandit posteriors
# --------------------------------------------------

class BanditPosterior(Base):
    __tablename__ = "bandit_posteriors"

    id = Column(Integer, primary_key=True)

    failure_class = Column(
        String(100),
        nullable=False,
    )

    arm = Column(
        String(20),
        nullable=False,
    )

    alpha = Column(
        Float,
        default=1.0,
        nullable=False,
    )

    beta = Column(
        Float,
        default=1.0,
        nullable=False,
    )

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "failure_class",
            "arm",
            name="uq_bandit_failure_arm",
        ),
    )


# --------------------------------------------------
# Compliance log
# --------------------------------------------------

class ComplianceLog(Base):
    __tablename__ = "compliance_log"

    id = Column(Integer, primary_key=True)

    transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=False,
    )

    action_type = Column(String(100))

    channel = Column(String(50))

    decision = Column(
        String(50),
        nullable=False,
    )

    reason = Column(Text)

    amount_forgone = Column(
        Float,
        default=0.0,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


# --------------------------------------------------
# Eval results (triage confusion matrix storage)
# --------------------------------------------------

class EvalResult(Base):
    __tablename__ = "eval_results"

    id = Column(Integer, primary_key=True)

    run_id = Column(String(100), nullable=False)    # UUID per eval run
    txn_id = Column(String(100), nullable=False)    # str — may be "hld_N"
    model_tier = Column(String(20))                 # "rules", "haiku", "sonnet"
    true_label = Column(String(50), nullable=False)
    predicted_label = Column(String(50), nullable=False)
    confidence = Column(Float)
    correct = Column(Boolean, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


# --------------------------------------------------
# Cost log (LLM spend per call)
# --------------------------------------------------

class CostLog(Base):
    __tablename__ = "cost_log"

    id = Column(Integer, primary_key=True)

    txn_id = Column(String(100))
    model_tier = Column(String(20), nullable=False)   # "haiku", "sonnet"
    model_name = Column(String(100), nullable=False)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    latency_ms = Column(Float, default=0.0)

    created_at = Column(DateTime, default=datetime.utcnow)


# --------------------------------------------------
# Contextual Bandit Posteriors
# --------------------------------------------------

class BanditPosterior(Base):
    __tablename__ = "bandit_posteriors"

    id = Column(Integer, primary_key=True)

    failure_class = Column(String(50), nullable=False)
    arm = Column(String(100), nullable=False)
    alpha = Column(Float, default=1.0, nullable=False)
    beta = Column(Float, default=1.0, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# --------------------------------------------------
# Ground truth
# --------------------------------------------------

class GroundTruth(GroundTruthBase):
    __tablename__ = "ground_truth"

    id = Column(Integer, primary_key=True)

    transaction_id = Column(
        Integer,
        nullable=False,
        unique=True,
    )

    recoverable = Column(
        Boolean,
        nullable=False,
    )

    recovery_probability = Column(
        Float,
        nullable=False,
    )

    actual_outcome = Column(String(50))

    reason = Column(Text)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )


# --------------------------------------------------
# Eval holdout (30% split — lives in ground_truth.db)
# Mirrors Transaction fields so eval harness can process
# holdout records identically. Agent modules never see this.
# --------------------------------------------------

class EvalHoldout(GroundTruthBase):
    __tablename__ = "eval_holdout"

    id = Column(Integer, primary_key=True)

    # Original IDs (denormalised — no FK across engines)
    merchant_id = Column(Integer, nullable=False)
    customer_id = Column(Integer, nullable=False)

    razorpay_payment_id = Column(String(200), unique=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="INR")
    payment_method = Column(String(50))
    bank = Column(String(100))
    failure_code = Column(String(100))
    failure_reason = Column(Text)
    status = Column(String(50), default="AT_RISK")

    # Phase 3 enrichment fields (same as Transaction)
    customer_lifetime_value = Column(Float)
    previous_successful_payments = Column(Integer, default=0)
    previous_failed_payments = Column(Integer, default=0)
    previous_recoveries = Column(Integer, default=0)
    inferred_salary_window = Column(String(30))
    mandate_expiry = Column(DateTime)
    order_notes = Column(Text)
    preferred_language = Column(String(10))
    opt_out_status = Column(Boolean, default=False)
    margin_rate = Column(Float)

    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Ground-truth oracle fields (pre-computed at generation time)
    recoverable = Column(Boolean)
    recovery_probability = Column(Float)
    optimal_action = Column(String(50))



# --------------------------------------------------
# Database initialization
# --------------------------------------------------

def init_db():
    Base.metadata.create_all(bind=engine)

    GroundTruthBase.metadata.create_all(
        bind=ground_truth_engine
    )


# --------------------------------------------------
# Main database session
# --------------------------------------------------

def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------
# Ground truth database session
# --------------------------------------------------

def get_ground_truth_db():
    db = GroundTruthSessionLocal()

    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------
# Run directly
# --------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("ReviveAI database initialized.")