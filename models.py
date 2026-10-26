from datetime import date, datetime, timedelta
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint

db = SQLAlchemy()


class Borrower(db.Model):
    __tablename__ = "borrowers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=True, index=True)
    address = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    date_added = db.Column(db.Date, nullable=False, default=date.today)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    # active | inactive

    loans = db.relationship("Loan", back_populates="borrower", lazy="dynamic")
    payments = db.relationship("Payment", back_populates="borrower", lazy="dynamic")

    def __repr__(self):
        return f"<Borrower {self.id} {self.name}>"


class Loan(db.Model):
    __tablename__ = "loans"

    id = db.Column(db.Integer, primary_key=True)
    borrower_id = db.Column(
        db.Integer, db.ForeignKey("borrowers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    principal_amount = db.Column(db.Numeric(14, 2), nullable=False)
    loan_date = db.Column(db.Date, nullable=False, default=date.today)

    # weekly | monthly | other
    collection_frequency = db.Column(db.String(20), nullable=False, default="weekly")
    # For weekly: Mon-Sun; for monthly: day of month 1-28; for other: free text
    collection_day = db.Column(db.String(20), nullable=True)
    first_collection_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)

    # fixed_weekly | percentage | monthly | simple_on_outstanding
    interest_method = db.Column(db.String(40), nullable=False, default="fixed_weekly")
    interest_amount = db.Column(db.Numeric(14, 2), nullable=True, default=Decimal("0.00"))
    interest_percentage = db.Column(db.Numeric(8, 4), nullable=True, default=Decimal("0.0000"))

    # active | paused | closed
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    borrower = db.relationship("Borrower", back_populates="loans")
    scheduled_collections = db.relationship(
        "ScheduledCollection", back_populates="loan", lazy="dynamic", cascade="all, delete-orphan"
    )
    payments = db.relationship("Payment", back_populates="loan", lazy="dynamic")
    transactions = db.relationship(
        "LoanTransaction", back_populates="loan", lazy="dynamic", cascade="all, delete-orphan"
    )
    adjustments = db.relationship(
        "LoanAdjustment", back_populates="loan", lazy="dynamic", cascade="all, delete-orphan"
    )

    def scheduled_interest_amount(self):
        """Interest due per collection period based on current loan settings."""
        method = self.interest_method
        principal = Decimal(self.principal_amount or 0)
        if method == "fixed_weekly":
            return Decimal(self.interest_amount or 0)
        if method == "monthly":
            return Decimal(self.interest_amount or 0)
        if method == "percentage":
            pct = Decimal(self.interest_percentage or 0)
            return (principal * pct / Decimal("100")).quantize(Decimal("0.01"))
        if method == "simple_on_outstanding":
            # Uses remaining principal from ledger when available; falls back to original
            remaining = getattr(self, "_cached_remaining_principal", None)
            if remaining is None:
                remaining = principal
            pct = Decimal(self.interest_percentage or 0)
            if pct > 0:
                return (Decimal(remaining) * pct / Decimal("100")).quantize(Decimal("0.01"))
            return Decimal(self.interest_amount or 0)
        return Decimal(self.interest_amount or 0)

    def __repr__(self):
        return f"<Loan {self.id} borrower={self.borrower_id}>"


class ScheduledCollection(db.Model):
    __tablename__ = "scheduled_collections"
    __table_args__ = (
        UniqueConstraint("loan_id", "due_date", name="uq_loan_due_date"),
        Index("ix_collections_due_status", "due_date", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(
        db.Integer, db.ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    due_date = db.Column(db.Date, nullable=False, index=True)
    amount_due = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    amount_paid = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    interest_portion = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    principal_portion = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    # upcoming | due_today | paid | partially_paid | overdue | waived
    status = db.Column(db.String(20), nullable=False, default="upcoming", index=True)
    notes = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    loan = db.relationship("Loan", back_populates="scheduled_collections")
    allocations = db.relationship("PaymentAllocation", back_populates="scheduled_collection")

    @property
    def remaining(self):
        return max(Decimal(self.amount_due or 0) - Decimal(self.amount_paid or 0), Decimal("0.00"))

    def refresh_status(self, today=None):
        today = today or date.today()
        if self.status == "waived":
            return
        due = Decimal(self.amount_due or 0)
        paid = Decimal(self.amount_paid or 0)
        if paid >= due and due > 0:
            self.status = "paid"
        elif paid > 0 and paid < due:
            self.status = "partially_paid"
        elif paid == 0 or paid < due:
            if self.due_date < today:
                self.status = "overdue"
            elif self.due_date == today:
                self.status = "due_today"
            else:
                self.status = "upcoming"

    def __repr__(self):
        return f"<ScheduledCollection {self.id} loan={self.loan_id} {self.due_date}>"


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    borrower_id = db.Column(
        db.Integer, db.ForeignKey("borrowers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    loan_id = db.Column(
        db.Integer, db.ForeignKey("loans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_date = db.Column(db.Date, nullable=False, index=True)
    amount_received = db.Column(db.Numeric(14, 2), nullable=False)
    payment_method = db.Column(db.String(40), nullable=False, default="cash")
    notes = db.Column(db.Text, nullable=True)
    is_reversal = db.Column(db.Boolean, nullable=False, default=False)
    reverses_payment_id = db.Column(
        db.Integer, db.ForeignKey("payments.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    borrower = db.relationship("Borrower", back_populates="payments")
    loan = db.relationship("Loan", back_populates="payments")
    allocations = db.relationship(
        "PaymentAllocation", back_populates="payment", cascade="all, delete-orphan"
    )
    reverses = db.relationship("Payment", remote_side=[id], uselist=False)

    def __repr__(self):
        return f"<Payment {self.id} amount={self.amount_received}>"


class PaymentAllocation(db.Model):
    __tablename__ = "payment_allocations"

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(
        db.Integer, db.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # interest | principal | late_fee | other | extra_interest
    allocation_type = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    scheduled_collection_id = db.Column(
        db.Integer,
        db.ForeignKey("scheduled_collections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    payment = db.relationship("Payment", back_populates="allocations")
    scheduled_collection = db.relationship("ScheduledCollection", back_populates="allocations")

    def __repr__(self):
        return f"<PaymentAllocation {self.id} {self.allocation_type}={self.amount}>"


class LoanTransaction(db.Model):
    __tablename__ = "loan_transactions"
    __table_args__ = (Index("ix_txn_loan_date", "loan_id", "transaction_date"),)

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(
        db.Integer, db.ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transaction_date = db.Column(db.Date, nullable=False, index=True)
    # disbursement | interest_accrual | extra_interest | late_fee | payment |
    # principal_repayment | adjustment_credit | adjustment_debit | waive_interest |
    # collection_waive | reversal | note
    transaction_type = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    description = db.Column(db.String(255), nullable=True)
    reference_type = db.Column(db.String(40), nullable=True)
    reference_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    loan = db.relationship("Loan", back_populates="transactions")

    def __repr__(self):
        return f"<LoanTransaction {self.id} {self.transaction_type}>"


class LoanAdjustment(db.Model):
    __tablename__ = "loan_adjustments"

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(
        db.Integer, db.ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    adjustment_date = db.Column(db.Date, nullable=False, index=True)
    # extra_interest | late_fee | waive_interest | credit | debit | future_interest_change | note
    adjustment_type = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    reason = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    loan = db.relationship("Loan", back_populates="adjustments")

    def __repr__(self):
        return f"<LoanAdjustment {self.id} {self.adjustment_type}>"


# ---------------------------------------------------------------------------
# Balance helpers (ledger is source of truth)
# ---------------------------------------------------------------------------

def _sum_txns(loan_id, types):
    total = (
        db.session.query(db.func.coalesce(db.func.sum(LoanTransaction.amount), 0))
        .filter(LoanTransaction.loan_id == loan_id, LoanTransaction.transaction_type.in_(types))
        .scalar()
    )
    return Decimal(str(total)).quantize(Decimal("0.01"))


def loan_balances(loan):
    """Compute financial balances from the transaction ledger and allocations."""
    loan_id = loan.id if hasattr(loan, "id") else loan

    principal = Decimal(loan.principal_amount)
    principal_repaid = _sum_txns(loan_id, ["principal_repayment"])
    # Credits that reduce principal (rare) / debits that increase
    principal_credit = _sum_txns(loan_id, ["adjustment_credit"])
    principal_debit = _sum_txns(loan_id, ["adjustment_debit"])

    remaining_principal = principal - principal_repaid - principal_credit + principal_debit
    if remaining_principal < 0:
        remaining_principal = Decimal("0.00")

    interest_accrued = _sum_txns(
        loan_id, ["interest_accrual", "extra_interest", "late_fee"]
    )
    interest_waived = _sum_txns(loan_id, ["waive_interest", "collection_waive"])
    interest_paid = _sum_txns(loan_id, ["payment"])  # interest portion tracked separately below

    # Prefer allocation-based interest/principal paid figures
    interest_paid_alloc = (
        db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0))
        .join(Payment)
        .filter(
            Payment.loan_id == loan_id,
            Payment.is_reversal.is_(False),
            PaymentAllocation.allocation_type.in_(["interest", "extra_interest", "late_fee"]),
        )
        .scalar()
    )
    interest_paid_alloc = Decimal(str(interest_paid_alloc)).quantize(Decimal("0.01"))

    # Subtract reversed interest allocations
    reversed_interest = (
        db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0))
        .join(Payment)
        .filter(
            Payment.loan_id == loan_id,
            Payment.is_reversal.is_(True),
            PaymentAllocation.allocation_type.in_(["interest", "extra_interest", "late_fee"]),
        )
        .scalar()
    )
    reversed_interest = Decimal(str(reversed_interest)).quantize(Decimal("0.01"))
    interest_paid_net = interest_paid_alloc - reversed_interest
    if interest_paid_net < 0:
        interest_paid_net = Decimal("0.00")

    principal_paid_alloc = (
        db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0))
        .join(Payment)
        .filter(
            Payment.loan_id == loan_id,
            Payment.is_reversal.is_(False),
            PaymentAllocation.allocation_type == "principal",
        )
        .scalar()
    )
    principal_paid_alloc = Decimal(str(principal_paid_alloc)).quantize(Decimal("0.01"))
    reversed_principal = (
        db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0))
        .join(Payment)
        .filter(
            Payment.loan_id == loan_id,
            Payment.is_reversal.is_(True),
            PaymentAllocation.allocation_type == "principal",
        )
        .scalar()
    )
    reversed_principal = Decimal(str(reversed_principal)).quantize(Decimal("0.01"))
    principal_repaid_net = principal_paid_alloc - reversed_principal
    if principal_repaid_net < 0:
        principal_repaid_net = Decimal("0.00")

    remaining_principal = principal - principal_repaid_net - principal_credit + principal_debit
    if remaining_principal < 0:
        remaining_principal = Decimal("0.00")

    outstanding_interest = interest_accrued - interest_waived - interest_paid_net
    if outstanding_interest < 0:
        outstanding_interest = Decimal("0.00")

    extra_charges = _sum_txns(loan_id, ["extra_interest", "late_fee"])
    total_payments = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount_received), 0))
        .filter(Payment.loan_id == loan_id, Payment.is_reversal.is_(False))
        .scalar()
    )
    total_payments = Decimal(str(total_payments)).quantize(Decimal("0.01"))
    total_reversals = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount_received), 0))
        .filter(Payment.loan_id == loan_id, Payment.is_reversal.is_(True))
        .scalar()
    )
    total_reversals = Decimal(str(total_reversals)).quantize(Decimal("0.01"))
    total_payments_net = total_payments - total_reversals

    total_outstanding = remaining_principal + outstanding_interest

    next_due = (
        ScheduledCollection.query.filter(
            ScheduledCollection.loan_id == loan_id,
            ScheduledCollection.status.in_(
                ["upcoming", "due_today", "partially_paid", "overdue"]
            ),
        )
        .order_by(ScheduledCollection.due_date.asc())
        .first()
    )

    return {
        "original_principal": principal,
        "principal_repaid": principal_repaid_net,
        "remaining_principal": remaining_principal,
        "interest_accrued": interest_accrued,
        "interest_paid": interest_paid_net,
        "interest_waived": interest_waived,
        "outstanding_interest": outstanding_interest,
        "extra_charges": extra_charges,
        "total_payments": total_payments_net,
        "total_outstanding": total_outstanding,
        "next_due_date": next_due.due_date if next_due else None,
        "next_due_amount": next_due.remaining if next_due else Decimal("0.00"),
    }


def week_bounds(ref=None):
    """Monday–Sunday week containing ref (default today)."""
    ref = ref or date.today()
    start = ref - timedelta(days=ref.weekday())
    end = start + timedelta(days=6)
    return start, end


def refresh_collection_statuses(loan_id=None):
    """Update status flags based on due dates and amounts paid."""
    today = date.today()
    q = ScheduledCollection.query.filter(ScheduledCollection.status != "waived")
    if loan_id:
        q = q.filter(ScheduledCollection.loan_id == loan_id)
    for sc in q.all():
        sc.refresh_status(today)
