from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from calendar import monthrange

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from config import Config
from models import (
    Borrower,
    Loan,
    ScheduledCollection,
    Payment,
    PaymentAllocation,
    LoanTransaction,
    LoanAdjustment,
    BalanceEntry,
    db,
    loan_balances,
    week_bounds,
    refresh_collection_statuses,
)

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)


@app.context_processor
def inject_globals():
    return {"now": date.today().isoformat()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def money(value, default="0"):
    """Parse a form value into Decimal money."""
    if value is None or str(value).strip() == "":
        return Decimal(default)
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def parse_date(value, default=None):
    if not value:
        return default
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def add_ledger(loan_id, txn_type, amount, description, txn_date=None, ref_type=None, ref_id=None):
    txn = LoanTransaction(
        loan_id=loan_id,
        transaction_date=txn_date or date.today(),
        transaction_type=txn_type,
        amount=Decimal(amount).quantize(Decimal("0.01")),
        description=description,
        reference_type=ref_type,
        reference_id=ref_id,
    )
    db.session.add(txn)
    return txn


def next_due_after(current, frequency):
    if frequency == "weekly":
        return current + timedelta(days=7)
    if frequency == "monthly":
        year, month = current.year, current.month
        day = current.day
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        last = monthrange(year, month)[1]
        return date(year, month, min(day, last))
    # other: treat as weekly by default
    return current + timedelta(days=7)


def generate_collections(loan, weeks_ahead=12, through_date=None):
    """
    Create upcoming scheduled collections without duplicating existing due dates.
    Accrues scheduled interest in the ledger for each newly created collection.
    """
    if loan.status in ("closed", "paused"):
        return 0

    today = date.today()
    if through_date is None:
        through_date = today + timedelta(weeks=weeks_ahead)

    if loan.end_date and through_date > loan.end_date:
        through_date = loan.end_date

    existing = {
        sc.due_date
        for sc in ScheduledCollection.query.filter_by(loan_id=loan.id).all()
    }

    # Remaining principal for simple_on_outstanding
    bal = loan_balances(loan)
    loan._cached_remaining_principal = bal["remaining_principal"]

    created = 0
    due = loan.first_collection_date

    # Advance past dates that already exist or are before first
    while due <= through_date:
        if due not in existing:
            interest = loan.scheduled_interest_amount()
            # Carry-forward unpaid interest is already on older collections;
            # each new period only schedules that period's interest by default.
            sc = ScheduledCollection(
                loan_id=loan.id,
                due_date=due,
                amount_due=interest,
                amount_paid=Decimal("0.00"),
                interest_portion=interest,
                principal_portion=Decimal("0.00"),
            )
            sc.refresh_status(today)
            db.session.add(sc)
            db.session.flush()

            if interest > 0:
                add_ledger(
                    loan.id,
                    "interest_accrual",
                    interest,
                    f"Scheduled interest for {due.isoformat()}",
                    txn_date=due,
                    ref_type="scheduled_collection",
                    ref_id=sc.id,
                )
            created += 1
            existing.add(due)

        due = next_due_after(due, loan.collection_frequency)
        if loan.end_date and due > loan.end_date:
            break

    return created


def apply_payment_to_collections(loan, payment, amount, prefer_interest=True):
    """
    Allocate payment across overdue/due collections then leftover to principal.
    Returns list of PaymentAllocation objects (already added to session).
    """
    remaining = Decimal(amount)
    allocations = []

    collections = (
        ScheduledCollection.query.filter(
            ScheduledCollection.loan_id == loan.id,
            ScheduledCollection.status.in_(
                ["overdue", "due_today", "partially_paid", "upcoming"]
            ),
        )
        .order_by(ScheduledCollection.due_date.asc())
        .all()
    )

    for sc in collections:
        if remaining <= 0:
            break
        need = sc.remaining
        if need <= 0:
            continue
        take = min(remaining, need)

        # Interest first within the installment, then principal portion
        interest_left = max(
            Decimal(sc.interest_portion or 0)
            - min(Decimal(sc.amount_paid or 0), Decimal(sc.interest_portion or 0)),
            Decimal("0.00"),
        )
        # More accurate: of amount already paid, how much covered interest
        paid_so_far = Decimal(sc.amount_paid or 0)
        interest_covered = min(paid_so_far, Decimal(sc.interest_portion or 0))
        interest_left = Decimal(sc.interest_portion or 0) - interest_covered
        if interest_left < 0:
            interest_left = Decimal("0.00")

        to_interest = min(take, interest_left) if prefer_interest else Decimal("0.00")
        to_principal = take - to_interest

        if to_interest > 0:
            allocations.append(
                PaymentAllocation(
                    payment_id=payment.id,
                    allocation_type="interest",
                    amount=to_interest,
                    scheduled_collection_id=sc.id,
                )
            )
        if to_principal > 0:
            allocations.append(
                PaymentAllocation(
                    payment_id=payment.id,
                    allocation_type="principal",
                    amount=to_principal,
                    scheduled_collection_id=sc.id,
                )
            )

        sc.amount_paid = Decimal(sc.amount_paid or 0) + take
        sc.refresh_status()
        remaining -= take

    # Leftover goes to principal repayment (no collection row)
    if remaining > 0:
        allocations.append(
            PaymentAllocation(
                payment_id=payment.id,
                allocation_type="principal",
                amount=remaining,
                scheduled_collection_id=None,
            )
        )
        remaining = Decimal("0.00")

    for a in allocations:
        db.session.add(a)

    return allocations


def record_payment_side_effects(loan, payment, allocations, payment_date):
    """Write ledger entries for a payment based on its allocations."""
    interest_total = Decimal("0.00")
    principal_total = Decimal("0.00")
    other_total = Decimal("0.00")

    for a in allocations:
        if a.allocation_type in ("interest", "extra_interest", "late_fee"):
            interest_total += Decimal(a.amount)
        elif a.allocation_type == "principal":
            principal_total += Decimal(a.amount)
        else:
            other_total += Decimal(a.amount)

    if interest_total > 0:
        add_ledger(
            loan.id,
            "payment",
            interest_total,
            f"Interest payment #{payment.id}",
            txn_date=payment_date,
            ref_type="payment",
            ref_id=payment.id,
        )
    if principal_total > 0:
        add_ledger(
            loan.id,
            "principal_repayment",
            principal_total,
            f"Principal repayment #{payment.id}",
            txn_date=payment_date,
            ref_type="payment",
            ref_id=payment.id,
        )
    if other_total > 0:
        add_ledger(
            loan.id,
            "payment",
            other_total,
            f"Other charges payment #{payment.id}",
            txn_date=payment_date,
            ref_type="payment",
            ref_id=payment.id,
        )


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter("money")
def money_filter(value):
    try:
        return f"{Decimal(value):,.2f}"
    except Exception:
        return "0.00"


@app.template_filter("status_badge")
def status_badge(status):
    mapping = {
        "active": "success",
        "inactive": "secondary",
        "paused": "warning",
        "closed": "dark",
        "upcoming": "info",
        "due_today": "primary",
        "paid": "success",
        "partially_paid": "warning",
        "overdue": "danger",
        "waived": "secondary",
    }
    return mapping.get(status, "secondary")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    refresh_collection_statuses()

    week_start, week_end = week_bounds()
    active_loans = Loan.query.filter_by(status="active").count()

    loans = Loan.query.filter(Loan.status.in_(["active", "paused"])).all()
    total_capital = Decimal("0.00")
    outstanding_interest = Decimal("0.00")
    for loan in loans:
        bal = loan_balances(loan)
        total_capital += bal["original_principal"]
        outstanding_interest += bal["outstanding_interest"]

    week_collections = ScheduledCollection.query.filter(
        ScheduledCollection.due_date >= week_start,
        ScheduledCollection.due_date <= week_end,
        ScheduledCollection.status != "waived",
    ).all()
    expected_week = sum((Decimal(c.amount_due or 0) for c in week_collections), Decimal("0.00"))
    actual_week = sum((Decimal(c.amount_paid or 0) for c in week_collections), Decimal("0.00"))

    overdue_rows = ScheduledCollection.query.filter(
        ScheduledCollection.status == "overdue"
    ).all()
    total_overdue = sum((c.remaining for c in overdue_rows), Decimal("0.00"))

    stats = {
        "active_loans": active_loans,
        "total_capital": total_capital,
        "outstanding_interest": outstanding_interest,
        "expected_week": expected_week,
        "actual_week": actual_week,
        "total_overdue": total_overdue,
        "week_start": week_start,
        "week_end": week_end,
    }
    return render_template("dashboard.html", stats=stats)


# ---------------------------------------------------------------------------
# Balance (cash calculator)
# ---------------------------------------------------------------------------

@app.route("/balance")
def balance_page():
    # All money collected from borrowers (loan payments)
    payments = (
        Payment.query.order_by(Payment.payment_date.desc(), Payment.id.desc()).all()
    )
    collected = Decimal("0.00")
    payment_rows = []
    for p in payments:
        amt = Decimal(p.amount_received or 0)
        if p.is_reversal:
            collected -= amt
            sign = "+"
            label = "reversal"
        else:
            collected += amt
            sign = "+"
            label = "collected"
        name = p.borrower.name if p.borrower else f"Loan #{p.loan_id}"
        payment_rows.append(
            {
                "date": p.payment_date,
                "sign": "+" if not p.is_reversal else "−",
                "amount": amt,
                "notes": f"{label}: {name}" + (f" — {p.notes}" if p.notes else ""),
                "source": "payment",
                "id": p.id,
            }
        )
    if collected < 0:
        collected = Decimal("0.00")

    entries = (
        BalanceEntry.query.order_by(BalanceEntry.entry_date.desc(), BalanceEntry.id.desc()).all()
    )
    manual_in = Decimal("0.00")
    manual_out = Decimal("0.00")
    entry_rows = []
    for e in entries:
        amt = Decimal(e.amount or 0)
        if e.entry_type == "in":
            manual_in += amt
            entry_rows.append(
                {
                    "date": e.entry_date,
                    "sign": "+",
                    "amount": amt,
                    "notes": e.notes or "Money in",
                    "source": "entry",
                    "id": e.id,
                    "entry_type": "in",
                }
            )
        else:
            manual_out += amt
            entry_rows.append(
                {
                    "date": e.entry_date,
                    "sign": "−",
                    "amount": amt,
                    "notes": e.notes or "Spending",
                    "source": "entry",
                    "id": e.id,
                    "entry_type": "out",
                }
            )

    # Combined calculator lines: manual entries + collections (newest first)
    lines = entry_rows + [
        {
            "date": r["date"],
            "sign": r["sign"],
            "amount": r["amount"],
            "notes": r["notes"],
            "source": "payment",
            "id": r["id"],
        }
        for r in payment_rows
    ]
    lines.sort(key=lambda x: (x["date"], x["id"]), reverse=True)

    balance = collected + manual_in - manual_out

    return render_template(
        "balance.html",
        collected=collected,
        manual_in=manual_in,
        manual_out=manual_out,
        balance=balance,
        lines=lines,
        entries=entries,
        payments=payments,
    )


@app.route("/balance/add", methods=["POST"])
def balance_add():
    entry_type = request.form.get("entry_type", "in")
    if entry_type not in ("in", "out"):
        entry_type = "in"
    amount = money(request.form.get("amount"))
    entry_date = parse_date(request.form.get("entry_date"), date.today())
    notes = request.form.get("notes", "").strip() or None

    if amount <= 0:
        flash("Enter an amount greater than 0.", "danger")
        return redirect(url_for("balance_page"))

    db.session.add(
        BalanceEntry(
            entry_date=entry_date,
            entry_type=entry_type,
            amount=amount,
            notes=notes,
        )
    )
    db.session.commit()
    if entry_type == "in":
        flash(f"Added +{amount} to balance.", "success")
    else:
        flash(f"Added −{amount} spending.", "success")
    return redirect(url_for("balance_page"))


@app.route("/balance/<int:entry_id>/delete", methods=["POST"])
def balance_delete(entry_id):
    entry = BalanceEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("Balance entry deleted.", "success")
    return redirect(url_for("balance_page"))


def _delete_loan_records(loan):
    """Remove a loan (and its person if they have no other loans)."""
    borrower = loan.borrower
    borrower_id = loan.borrower_id
    payments = Payment.query.filter_by(loan_id=loan.id).all()
    for payment in payments:
        PaymentAllocation.query.filter_by(payment_id=payment.id).delete()
        db.session.delete(payment)
    ScheduledCollection.query.filter_by(loan_id=loan.id).delete()
    LoanTransaction.query.filter_by(loan_id=loan.id).delete()
    LoanAdjustment.query.filter_by(loan_id=loan.id).delete()
    db.session.delete(loan)
    db.session.flush()
    # One person = one loan in this simple app
    remaining = Loan.query.filter_by(borrower_id=borrower_id).count()
    if remaining == 0 and borrower:
        Payment.query.filter_by(borrower_id=borrower_id).delete()
        db.session.delete(borrower)


# Old borrower URLs redirect into the single Loans flow
@app.route("/borrowers")
@app.route("/borrowers/add", methods=["GET", "POST"])
def borrowers_redirect():
    if request.method == "POST" or request.path.endswith("/add"):
        return redirect(url_for("loans_add"))
    return redirect(url_for("loans_list"))


@app.route("/borrowers/<int:borrower_id>")
def borrowers_view(borrower_id):
    loan = (
        Loan.query.filter_by(borrower_id=borrower_id)
        .order_by(Loan.id.desc())
        .first()
    )
    if loan:
        return redirect(url_for("loans_view", loan_id=loan.id))
    return redirect(url_for("loans_list"))


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

@app.route("/loans")
@app.route("/loan")
def loans_list():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "all")
    query = Loan.query.join(Borrower)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Borrower.name.like(like), Borrower.phone.like(like)))
    if status in ("active", "paused", "closed"):
        query = query.filter(Loan.status == status)
    loans = query.order_by(Loan.loan_date.desc(), Loan.id.desc()).all()
    return render_template("loans/list.html", loans=loans, q=q, status=status)


def _parse_week_rows():
    """Parse manually entered week rows from the create/add-week forms."""
    dates = request.form.getlist("week_date")
    interests = request.form.getlist("week_interest")
    extra_types = request.form.getlist("week_extra_type")
    extra_amounts = request.form.getlist("week_extra_amount")
    rows = []
    for i, raw_date in enumerate(dates):
        due = parse_date(raw_date)
        if not due:
            continue
        interest = money(interests[i] if i < len(interests) else "0")
        extra_type = (extra_types[i] if i < len(extra_types) else "none") or "none"
        extra_amt = money(extra_amounts[i] if i < len(extra_amounts) else "0")
        if extra_type == "none":
            extra_amt = Decimal("0.00")
        rows.append(
            {
                "due_date": due,
                "interest": interest,
                "extra_type": extra_type,
                "extra_amount": extra_amt,
                "amount_due": interest + extra_amt,
            }
        )
    rows.sort(key=lambda r: r["due_date"])
    return rows


def _week_form_values(sc):
    """Split a collection into interest / extra fields for the editable table."""
    amount_due = Decimal(sc.amount_due or 0)
    interest_portion = Decimal(sc.interest_portion or 0)
    notes = (sc.notes or "").lower()
    if "fine" in notes:
        extra_type = "fine"
        form_interest = min(interest_portion, amount_due)
        form_extra = max(amount_due - form_interest, Decimal("0.00"))
    elif "interest" in notes and amount_due > interest_portion:
        extra_type = "interest"
        # interest_portion already includes extra interest; show base as interest_portion - unknown
        form_extra = max(amount_due - interest_portion, Decimal("0.00"))
        form_interest = interest_portion
        if form_extra == 0 and "extra interest" in notes:
            # amount was fully in interest_portion; keep all in interest, extra 0
            form_interest = interest_portion
            form_extra = Decimal("0.00")
            extra_type = "interest"
    elif amount_due > interest_portion:
        extra_type = "fine"
        form_interest = interest_portion
        form_extra = amount_due - interest_portion
    else:
        extra_type = "none"
        form_interest = amount_due if amount_due > 0 else interest_portion
        form_extra = Decimal("0.00")
    return form_interest, form_extra, extra_type


def _week_ui_status(sc):
    if sc.status == "paid":
        return "paid"
    if sc.status == "waived":
        return "maaf"
    return "not_paid"


def _update_week_collection(sc, due, interest, extra_type, extra_amt):
    """Apply inline edits to an unpaid week and sync ledger."""
    if Decimal(sc.amount_paid or 0) > 0:
        return False
    if sc.status == "waived":
        # Still allow amount/date edits while maaf
        pass
    if extra_type == "none":
        extra_amt = Decimal("0.00")
    new_due_amount = interest + extra_amt

    clash = ScheduledCollection.query.filter(
        ScheduledCollection.loan_id == sc.loan_id,
        ScheduledCollection.due_date == due,
        ScheduledCollection.id != sc.id,
    ).first()
    if clash:
        return False

    old_interest = Decimal(sc.interest_portion or 0)
    old_amount = Decimal(sc.amount_due or 0)

    interest_portion = interest
    if extra_type == "interest":
        interest_portion += extra_amt

    sc.due_date = due
    sc.amount_due = new_due_amount
    sc.interest_portion = interest_portion
    sc.notes = (
        f"Extra {extra_type}: {extra_amt}"
        if extra_type in ("interest", "fine") and extra_amt > 0
        else None
    )
    if sc.status != "waived":
        sc.refresh_status()

    interest_diff = interest_portion - old_interest
    if interest_diff > 0:
        add_ledger(
            sc.loan_id,
            "interest_accrual",
            interest_diff,
            f"Week updated (+interest) for {due.isoformat()}",
            ref_type="scheduled_collection",
            ref_id=sc.id,
        )
    elif interest_diff < 0:
        add_ledger(
            sc.loan_id,
            "waive_interest",
            abs(interest_diff),
            f"Week updated (−interest) for {due.isoformat()}",
            ref_type="scheduled_collection",
            ref_id=sc.id,
        )

    if extra_type == "fine":
        old_extra = max(old_amount - old_interest, Decimal("0.00"))
        fine_diff = extra_amt - old_extra
        if fine_diff > 0:
            add_ledger(
                sc.loan_id,
                "late_fee",
                fine_diff,
                f"Fine updated for {due.isoformat()}",
                ref_type="scheduled_collection",
                ref_id=sc.id,
            )
        elif fine_diff < 0:
            add_ledger(
                sc.loan_id,
                "waive_interest",
                abs(fine_diff),
                f"Fine reduced for {due.isoformat()}",
                ref_type="scheduled_collection",
                ref_id=sc.id,
            )
    return True


def _set_week_paid(sc, loan):
    """Mark week paid (no commit)."""
    if sc.status == "waived":
        sc.status = "upcoming"
    remaining = sc.remaining
    if remaining <= 0:
        sc.amount_paid = Decimal(sc.amount_due or 0)
        sc.status = "paid"
        return True

    payment = Payment(
        borrower_id=loan.borrower_id,
        loan_id=loan.id,
        payment_date=date.today(),
        amount_received=remaining,
        payment_method="cash",
        notes=f"Marked paid for week {sc.due_date.isoformat()}",
    )
    db.session.add(payment)
    db.session.flush()

    paid_so_far = Decimal(sc.amount_paid or 0)
    interest_covered = min(paid_so_far, Decimal(sc.interest_portion or 0))
    interest_left = max(Decimal(sc.interest_portion or 0) - interest_covered, Decimal("0.00"))
    to_interest = min(remaining, interest_left)
    to_principal = remaining - to_interest

    allocations = []
    if to_interest > 0:
        allocations.append(
            PaymentAllocation(
                payment_id=payment.id,
                allocation_type="interest",
                amount=to_interest,
                scheduled_collection_id=sc.id,
            )
        )
    if to_principal > 0:
        allocations.append(
            PaymentAllocation(
                payment_id=payment.id,
                allocation_type="principal",
                amount=to_principal,
                scheduled_collection_id=sc.id,
            )
        )
    for a in allocations:
        db.session.add(a)

    sc.amount_paid = Decimal(sc.amount_due or 0)
    sc.status = "paid"
    record_payment_side_effects(loan, payment, allocations, payment.payment_date)
    return True


def _set_week_unpaid(sc, loan):
    """Mark week not paid (no commit)."""
    if sc.status == "waived" and Decimal(sc.amount_paid or 0) <= 0:
        sc.status = "upcoming"
        sc.amount_paid = Decimal("0.00")
        sc.refresh_status()
        return True

    if Decimal(sc.amount_paid or 0) <= 0:
        if sc.status == "waived":
            sc.status = "upcoming"
        sc.refresh_status()
        return True

    allocs = (
        PaymentAllocation.query.join(Payment)
        .filter(
            PaymentAllocation.scheduled_collection_id == sc.id,
            Payment.is_reversal.is_(False),
        )
        .all()
    )
    payment_ids = {a.payment_id for a in allocs}
    for pid in payment_ids:
        original = Payment.query.get(pid)
        if not original:
            continue
        already = Payment.query.filter_by(
            reverses_payment_id=original.id, is_reversal=True
        ).first()
        if already:
            continue
        other = (
            PaymentAllocation.query.filter(
                PaymentAllocation.payment_id == original.id,
                PaymentAllocation.scheduled_collection_id != sc.id,
            ).count()
        )
        marked = (original.notes or "").startswith("Marked paid")
        if other > 0 and not marked:
            for a in allocs:
                if a.payment_id != original.id:
                    continue
                rev = Payment(
                    borrower_id=original.borrower_id,
                    loan_id=original.loan_id,
                    payment_date=date.today(),
                    amount_received=a.amount,
                    payment_method=original.payment_method,
                    notes=f"REVERSAL (mark unpaid) of allocation from payment #{original.id}",
                    is_reversal=True,
                    reverses_payment_id=original.id,
                )
                db.session.add(rev)
                db.session.flush()
                db.session.add(
                    PaymentAllocation(
                        payment_id=rev.id,
                        allocation_type=a.allocation_type,
                        amount=a.amount,
                        scheduled_collection_id=sc.id,
                    )
                )
                add_ledger(
                    loan.id,
                    "reversal",
                    a.amount,
                    f"Mark unpaid week {sc.due_date.isoformat()} (from payment #{original.id})",
                    ref_type="payment",
                    ref_id=rev.id,
                )
        else:
            rev = Payment(
                borrower_id=original.borrower_id,
                loan_id=original.loan_id,
                payment_date=date.today(),
                amount_received=original.amount_received,
                payment_method=original.payment_method,
                notes=f"REVERSAL (mark unpaid) of payment #{original.id}",
                is_reversal=True,
                reverses_payment_id=original.id,
            )
            db.session.add(rev)
            db.session.flush()
            for a in original.allocations:
                db.session.add(
                    PaymentAllocation(
                        payment_id=rev.id,
                        allocation_type=a.allocation_type,
                        amount=a.amount,
                        scheduled_collection_id=a.scheduled_collection_id,
                    )
                )
            add_ledger(
                loan.id,
                "reversal",
                original.amount_received,
                f"Mark unpaid week {sc.due_date.isoformat()}: reversed payment #{original.id}",
                ref_type="payment",
                ref_id=rev.id,
            )

    sc.amount_paid = Decimal("0.00")
    sc.status = "upcoming"
    sc.refresh_status()
    return True


def _set_week_maaf(sc):
    """Mark week as maaf / waived (no commit)."""
    remaining = sc.remaining
    sc.status = "waived"
    if remaining > 0:
        add_ledger(
            sc.loan_id,
            "collection_waive",
            remaining,
            f"Maaf / waived for {sc.due_date.isoformat()}",
            ref_type="scheduled_collection",
            ref_id=sc.id,
        )
    return True


def _create_week_collection(loan, row, today=None):
    """Create one scheduled collection + ledger from a week row. Returns None if duplicate."""
    today = today or date.today()
    exists = ScheduledCollection.query.filter_by(
        loan_id=loan.id, due_date=row["due_date"]
    ).first()
    if exists:
        return None

    interest = Decimal(row["interest"])
    extra_amt = Decimal(row["extra_amount"])
    extra_type = row["extra_type"]
    amount_due = Decimal(row["amount_due"])

    interest_portion = interest
    if extra_type == "interest":
        interest_portion += extra_amt

    sc = ScheduledCollection(
        loan_id=loan.id,
        due_date=row["due_date"],
        amount_due=amount_due,
        amount_paid=Decimal("0.00"),
        interest_portion=interest_portion,
        principal_portion=Decimal("0.00"),
        notes=(
            f"Extra {extra_type}: {extra_amt}"
            if extra_type in ("interest", "fine") and extra_amt > 0
            else None
        ),
    )
    sc.refresh_status(today)
    db.session.add(sc)
    db.session.flush()

    if interest > 0:
        add_ledger(
            loan.id,
            "interest_accrual",
            interest,
            f"Week interest for {row['due_date'].isoformat()}",
            txn_date=row["due_date"],
            ref_type="scheduled_collection",
            ref_id=sc.id,
        )
    if extra_type == "interest" and extra_amt > 0:
        add_ledger(
            loan.id,
            "extra_interest",
            extra_amt,
            f"Extra interest on {row['due_date'].isoformat()}",
            txn_date=row["due_date"],
            ref_type="scheduled_collection",
            ref_id=sc.id,
        )
    if extra_type == "fine" and extra_amt > 0:
        add_ledger(
            loan.id,
            "late_fee",
            extra_amt,
            f"Fine on {row['due_date'].isoformat()}",
            txn_date=row["due_date"],
            ref_type="scheduled_collection",
            ref_id=sc.id,
        )
    return sc


@app.route("/loans/add", methods=["GET", "POST"])
def loans_add():
    """Add borrower + loan together (same person)."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip() or None
        principal = money(request.form.get("principal_amount"))
        loan_date = parse_date(request.form.get("loan_date"), date.today())
        interest_amount = money(request.form.get("interest_amount"))
        weeks = _parse_week_rows()

        if not name or principal <= 0:
            flash("Name and principal are required.", "danger")
            return render_template("loans/form.html")

        borrower = Borrower(
            name=name,
            phone=phone,
            notes=request.form.get("notes", "").strip() or None,
            status="active",
        )
        db.session.add(borrower)
        db.session.flush()

        first_due = weeks[0]["due_date"] if weeks else loan_date
        last_due = weeks[-1]["due_date"] if weeks else None

        loan = Loan(
            borrower_id=borrower.id,
            principal_amount=principal,
            loan_date=loan_date,
            collection_frequency="weekly",
            collection_day=None,
            first_collection_date=first_due,
            end_date=last_due,
            interest_method="fixed_weekly",
            interest_amount=interest_amount,
            interest_percentage=Decimal("0.00"),
            status="active",
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(loan)
        db.session.flush()

        add_ledger(
            loan.id,
            "disbursement",
            principal,
            "Loan principal disbursed",
            txn_date=loan_date,
            ref_type="loan",
            ref_id=loan.id,
        )

        created = 0
        for row in weeks:
            if _create_week_collection(loan, row):
                created += 1

        db.session.commit()
        if created:
            flash(f"Saved {name} with {created} week(s).", "success")
        else:
            flash(f"Saved {name}. You can add weeks on the next page.", "success")
        return redirect(url_for("loans_view", loan_id=loan.id))

    return render_template("loans/form.html")


@app.route("/loans/<int:loan_id>")
def loans_view(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    refresh_collection_statuses(loan.id)
    bal = loan_balances(loan)
    schedule = (
        ScheduledCollection.query.filter_by(loan_id=loan.id)
        .order_by(ScheduledCollection.due_date.asc())
        .all()
    )
    week_forms = []
    total_weeks = 0
    paid_weeks = 0
    not_paid_weeks = 0
    maaf_weeks = 0
    total_interest = Decimal("0.00")
    total_due = Decimal("0.00")
    total_paid = Decimal("0.00")
    remaining_to_pay = Decimal("0.00")

    for sc in schedule:
        interest, extra, extra_type = _week_form_values(sc)
        pay_status = _week_ui_status(sc)
        week_forms.append(
            {
                "sc": sc,
                "interest": interest,
                "extra": extra,
                "extra_type": extra_type,
                "pay_status": pay_status,
            }
        )
        total_weeks += 1
        if pay_status == "paid":
            paid_weeks += 1
        elif pay_status == "maaf":
            maaf_weeks += 1
        else:
            not_paid_weeks += 1

        if pay_status != "maaf":
            total_interest += Decimal(sc.interest_portion or 0)
            total_due += Decimal(sc.amount_due or 0)
            total_paid += Decimal(sc.amount_paid or 0)
            remaining_to_pay += sc.remaining

    principal = Decimal(loan.principal_amount or 0)
    interest_left = max(total_interest - min(total_paid, total_interest), Decimal("0.00"))
    interest_plus_principal = remaining_to_pay + principal

    # Payments received (so Pay entries always show up)
    payments = (
        Payment.query.filter_by(loan_id=loan.id)
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
        .all()
    )
    payments_received = Decimal("0.00")
    for p in payments:
        if p.is_reversal:
            payments_received -= Decimal(p.amount_received or 0)
        else:
            payments_received += Decimal(p.amount_received or 0)
    if payments_received < 0:
        payments_received = Decimal("0.00")

    summary = {
        "total_weeks": total_weeks,
        "paid_weeks": paid_weeks,
        "not_paid_weeks": not_paid_weeks,
        "maaf_weeks": maaf_weeks,
        "total_interest": total_interest,
        "total_due": total_due,
        "total_paid": total_paid,
        "payments_received": payments_received,
        "remaining_to_pay": remaining_to_pay,
        "principal": principal,
        "interest_plus_principal": interest_plus_principal,
        "interest_left": interest_left,
    }
    topups = (
        LoanAdjustment.query.filter(
            LoanAdjustment.loan_id == loan.id,
            LoanAdjustment.adjustment_type.in_(["capital_topup", "interest_topup"]),
        )
        .order_by(LoanAdjustment.adjustment_date.desc(), LoanAdjustment.id.desc())
        .all()
    )

    return render_template(
        "loans/view.html",
        loan=loan,
        bal=bal,
        schedule=schedule,
        week_forms=week_forms,
        summary=summary,
        payments=payments,
        topups=topups,
    )


@app.route("/loans/<int:loan_id>/topup", methods=["POST"])
def loans_topup(loan_id):
    """Add more capital and/or weekly interest. Old weeks stay the same; new weeks use new total interest."""
    loan = Loan.query.get_or_404(loan_id)
    if loan.status == "closed":
        flash("This loan is closed.", "warning")
        return redirect(url_for("loans_view", loan_id=loan.id))

    add_capital = money(request.form.get("add_capital"))
    add_interest = money(request.form.get("add_interest"))
    topup_date = parse_date(request.form.get("topup_date"), date.today())
    reason = request.form.get("topup_reason", "").strip() or "Extra capital / interest"

    if add_capital <= 0 and add_interest <= 0:
        flash("Enter more capital and/or more weekly interest.", "danger")
        return redirect(url_for("loans_view", loan_id=loan.id))

    old_principal = Decimal(loan.principal_amount or 0)
    old_interest = Decimal(loan.interest_amount or 0)

    if add_capital > 0:
        loan.principal_amount = old_principal + add_capital
        db.session.add(
            LoanAdjustment(
                loan_id=loan.id,
                adjustment_date=topup_date,
                adjustment_type="capital_topup",
                amount=add_capital,
                reason=reason,
            )
        )
        add_ledger(
            loan.id,
            "disbursement",
            add_capital,
            f"More capital added (+{add_capital}). Total principal now {loan.principal_amount}. {reason}",
            txn_date=topup_date,
            ref_type="adjustment",
        )

    if add_interest > 0:
        loan.interest_amount = old_interest + add_interest
        db.session.add(
            LoanAdjustment(
                loan_id=loan.id,
                adjustment_date=topup_date,
                adjustment_type="interest_topup",
                amount=add_interest,
                reason=reason,
            )
        )
        add_ledger(
            loan.id,
            "note",
            add_interest,
            f"Weekly interest +{add_interest} (was {old_interest}, now {loan.interest_amount}). Old weeks unchanged. {reason}",
            txn_date=topup_date,
            ref_type="adjustment",
        )

    db.session.commit()
    parts = []
    if add_capital > 0:
        parts.append(f"capital +{add_capital} (now {loan.principal_amount})")
    if add_interest > 0:
        parts.append(
            f"weekly interest +{add_interest} (now {loan.interest_amount}/week for new weeks)"
        )
    flash("Top-up saved: " + ", ".join(parts) + ".", "success")
    return redirect(url_for("loans_view", loan_id=loan.id))


@app.route("/loans/<int:loan_id>/pay", methods=["POST"])
def loans_pay(loan_id):
    """Record a payment amount and apply it to oldest unpaid weeks first."""
    loan = Loan.query.get_or_404(loan_id)
    if loan.status == "closed":
        flash("This loan is closed.", "warning")
        return redirect(url_for("loans_view", loan_id=loan.id))

    amount = money(request.form.get("pay_amount"))
    pay_date = parse_date(request.form.get("pay_date"), date.today())
    notes = request.form.get("pay_notes", "").strip() or None

    if amount <= 0:
        flash("Enter a payment amount greater than 0.", "danger")
        return redirect(url_for("loans_view", loan_id=loan.id))

    try:
        payment = Payment(
            borrower_id=loan.borrower_id,
            loan_id=loan.id,
            payment_date=pay_date,
            amount_received=amount,
            payment_method=request.form.get("pay_method", "cash") or "cash",
            notes=notes or f"Payment of {amount}",
        )
        db.session.add(payment)
        db.session.flush()

        allocations = apply_payment_to_collections(loan, payment, amount)
        record_payment_side_effects(loan, payment, allocations, pay_date)
        db.session.commit()

        # How much still left after this payment
        refresh_collection_statuses(loan.id)
        left = Decimal("0.00")
        for sc in ScheduledCollection.query.filter(
            ScheduledCollection.loan_id == loan.id,
            ScheduledCollection.status != "waived",
        ).all():
            left += sc.remaining

        flash(
            f"Payment {amount} saved. Remaining on weeks: {left}.",
            "success",
        )
    except Exception as exc:
        db.session.rollback()
        flash(f"Payment failed: {exc}", "danger")

    return redirect(url_for("loans_view", loan_id=loan.id))


@app.route("/loans/<int:loan_id>/edit", methods=["GET", "POST"])
def loans_edit(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    borrowers = Borrower.query.order_by(Borrower.name).all()
    if request.method == "POST":
        # Only loan-level defaults / notes. Week rows are edited individually.
        loan.interest_method = request.form.get("interest_method", loan.interest_method)
        loan.interest_amount = money(request.form.get("interest_amount"), str(loan.interest_amount or 0))
        loan.interest_percentage = money(
            request.form.get("interest_percentage"), str(loan.interest_percentage or 0)
        )
        loan.end_date = parse_date(request.form.get("end_date"))
        loan.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        flash("Loan settings updated. Edit individual weeks to change dates or amounts.", "success")
        return redirect(url_for("loans_view", loan_id=loan.id))

    return render_template(
        "loans/form.html", loan=loan, borrowers=borrowers, title="Edit Loan", preselect=None
    )


@app.route("/loans/<int:loan_id>/add-week", methods=["GET", "POST"])
def loans_add_week(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    if loan.status == "closed":
        flash("This loan is closed.", "warning")
        return redirect(url_for("loans_view", loan_id=loan.id))

    if request.method == "GET":
        return redirect(url_for("loans_view", loan_id=loan.id))

    # Update person + interest on save
    name = request.form.get("name", "").strip()
    if name:
        loan.borrower.name = name
    if "phone" in request.form:
        loan.borrower.phone = request.form.get("phone", "").strip() or None
    if request.form.get("interest_amount") is not None:
        loan.interest_amount = money(
            request.form.get("interest_amount"), str(loan.interest_amount or 0)
        )

    # Save inline edits + status (paid / not paid / maaf)
    edit_ids = request.form.getlist("edit_week_id")
    edit_dates = request.form.getlist("edit_week_date")
    edit_interests = request.form.getlist("edit_week_interest")
    edit_extra_types = request.form.getlist("edit_week_extra_type")
    edit_extra_amounts = request.form.getlist("edit_week_extra_amount")
    edit_statuses = request.form.getlist("edit_week_status")
    updated = 0
    for i, raw_id in enumerate(edit_ids):
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            continue
        sc = ScheduledCollection.query.filter_by(id=cid, loan_id=loan.id).first()
        if not sc:
            continue
        due = parse_date(edit_dates[i] if i < len(edit_dates) else None)
        if not due:
            continue
        interest = money(edit_interests[i] if i < len(edit_interests) else "0")
        extra_type = (edit_extra_types[i] if i < len(edit_extra_types) else "none") or "none"
        extra_amt = money(edit_extra_amounts[i] if i < len(edit_extra_amounts) else "0")
        desired = (edit_statuses[i] if i < len(edit_statuses) else "not_paid") or "not_paid"
        if desired not in ("paid", "not_paid", "maaf"):
            desired = "not_paid"
        current = _week_ui_status(sc)

        # Ensure unpaid before editing amounts or switching away from paid
        if desired in ("not_paid", "maaf") and current == "paid":
            _set_week_unpaid(sc, loan)
            current = "not_paid"

        if desired != "paid":
            _update_week_collection(sc, due, interest, extra_type, extra_amt)

        if desired == "paid" and current != "paid":
            if current == "maaf":
                sc.status = "upcoming"
                sc.amount_paid = Decimal("0.00")
            _update_week_collection(sc, due, interest, extra_type, extra_amt)
            _set_week_paid(sc, loan)
        elif desired == "maaf" and current != "maaf":
            if Decimal(sc.amount_paid or 0) > 0:
                _set_week_unpaid(sc, loan)
            _update_week_collection(sc, due, interest, extra_type, extra_amt)
            _set_week_maaf(sc)
        elif desired == "not_paid" and current == "maaf":
            sc.status = "upcoming"
            sc.amount_paid = Decimal("0.00")
            sc.refresh_status()

        updated += 1

    # Add brand-new weeks (default status: not paid)
    weeks = _parse_week_rows()
    new_statuses = request.form.getlist("week_status")
    created = 0
    skipped = 0
    for idx, row in enumerate(weeks):
        sc = _create_week_collection(loan, row)
        if sc:
            created += 1
            if not loan.end_date or row["due_date"] > loan.end_date:
                loan.end_date = row["due_date"]
            desired = (new_statuses[idx] if idx < len(new_statuses) else "not_paid") or "not_paid"
            if desired == "paid":
                _set_week_paid(sc, loan)
            elif desired == "maaf":
                _set_week_maaf(sc)
        else:
            skipped += 1

    db.session.commit()
    parts = ["Saved."]
    if updated:
        parts.append(f"Updated {updated} week(s).")
    if created:
        parts.append(f"Added {created} week(s).")
    flash(" ".join(parts), "success")
    if skipped:
        flash(f"Skipped {skipped} duplicate date(s).", "warning")
    return redirect(url_for("loans_view", loan_id=loan.id))


@app.route("/collections/<int:collection_id>/edit", methods=["GET", "POST"])
def collection_edit(collection_id):
    sc = ScheduledCollection.query.get_or_404(collection_id)
    loan = sc.loan

    if request.method == "POST":
        if Decimal(sc.amount_paid or 0) > 0:
            flash("This week already has payments. Use adjustments instead of rewriting paid weeks.", "warning")
            return redirect(url_for("loans_view", loan_id=loan.id))

        new_date = parse_date(request.form.get("due_date"), sc.due_date)
        interest = money(request.form.get("interest"))
        extra_type = request.form.get("extra_type", "none")
        extra_amt = money(request.form.get("extra_amount"))
        if extra_type == "none":
            extra_amt = Decimal("0.00")
        new_due_amount = interest + extra_amt

        # Prevent duplicate dates on same loan
        clash = ScheduledCollection.query.filter(
            ScheduledCollection.loan_id == loan.id,
            ScheduledCollection.due_date == new_date,
            ScheduledCollection.id != sc.id,
        ).first()
        if clash:
            flash("Another week already uses that date.", "danger")
            return render_template("loans/edit_week.html", loan=loan, sc=sc)

        old_amount = Decimal(sc.amount_due or 0)
        old_interest = Decimal(sc.interest_portion or 0)

        sc.due_date = new_date
        sc.amount_due = new_due_amount
        interest_portion = interest
        if extra_type == "interest":
            interest_portion += extra_amt
        sc.interest_portion = interest_portion
        sc.notes = (
            f"Extra {extra_type}: {extra_amt}"
            if extra_type in ("interest", "fine") and extra_amt > 0
            else None
        )
        sc.refresh_status()

        # Ledger: adjust interest accrual difference for unpaid week rewrite
        interest_diff = interest_portion - old_interest
        if interest_diff > 0:
            add_ledger(
                loan.id,
                "interest_accrual",
                interest_diff,
                f"Week updated (+interest) for {new_date.isoformat()}",
                ref_type="scheduled_collection",
                ref_id=sc.id,
            )
        elif interest_diff < 0:
            add_ledger(
                loan.id,
                "waive_interest",
                abs(interest_diff),
                f"Week updated (−interest) for {new_date.isoformat()}",
                ref_type="scheduled_collection",
                ref_id=sc.id,
            )

        # Fine portion: if new extra is fine, post late_fee for fine amount when total grew beyond interest
        if extra_type == "fine" and extra_amt > 0:
            # Only post the fine part relative to previous non-interest portion
            old_extra = max(old_amount - old_interest, Decimal("0.00"))
            fine_diff = extra_amt - old_extra
            if fine_diff > 0:
                add_ledger(
                    loan.id,
                    "late_fee",
                    fine_diff,
                    f"Fine updated for {new_date.isoformat()}",
                    ref_type="scheduled_collection",
                    ref_id=sc.id,
                )
            elif fine_diff < 0:
                add_ledger(
                    loan.id,
                    "waive_interest",
                    abs(fine_diff),
                    f"Fine reduced for {new_date.isoformat()}",
                    ref_type="scheduled_collection",
                    ref_id=sc.id,
                )

        db.session.commit()
        flash("Week updated.", "success")
        return redirect(url_for("loans_view", loan_id=loan.id))

    # Infer current extra for form
    interest_shown = Decimal(sc.interest_portion or 0)
    amount_due = Decimal(sc.amount_due or 0)
    extra_shown = max(amount_due - interest_shown, Decimal("0.00"))
    # If notes say fine, treat extra as fine; else if extra > 0 as interest baked into interest_portion
    extra_type = "none"
    form_interest = interest_shown
    form_extra = Decimal("0.00")
    if sc.notes and "fine" in (sc.notes or "").lower():
        extra_type = "fine"
        # interest_portion may not include fine
        form_interest = min(interest_shown, amount_due)
        form_extra = max(amount_due - form_interest, Decimal("0.00"))
    elif extra_shown > 0:
        extra_type = "fine"
        form_extra = extra_shown
    elif sc.notes and "interest" in (sc.notes or "").lower():
        extra_type = "interest"
        # split roughly not available; keep interest as interest_portion, extra 0 editable
        form_extra = Decimal("0.00")

    return render_template(
        "loans/edit_week.html",
        loan=loan,
        sc=sc,
        form_interest=form_interest,
        form_extra=form_extra,
        extra_type=extra_type,
    )


@app.route("/loans/<int:loan_id>/pause", methods=["POST"])
def loans_pause(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    if loan.status != "active":
        flash("Only active loans can be paused.", "warning")
    else:
        loan.status = "paused"
        db.session.commit()
        flash("Collections paused.", "success")
    return redirect(url_for("loans_view", loan_id=loan.id))


@app.route("/loans/<int:loan_id>/resume", methods=["POST"])
def loans_resume(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    if loan.status != "paused":
        flash("Only paused loans can be resumed.", "warning")
    else:
        loan.status = "active"
        generate_collections(loan)
        db.session.commit()
        flash("Collections resumed.", "success")
    return redirect(url_for("loans_view", loan_id=loan.id))


@app.route("/loans/<int:loan_id>/close", methods=["POST"])
def loans_close(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    bal = loan_balances(loan)
    if bal["total_outstanding"] > Decimal("0.01"):
        confirm = request.form.get("force") == "1"
        if not confirm:
            flash(
                f"Loan still has outstanding {bal['total_outstanding']}. "
                "Resubmit with confirmation to close anyway.",
                "warning",
            )
            return redirect(url_for("loans_view", loan_id=loan.id))
    loan.status = "closed"
    db.session.commit()
    flash("Loan closed.", "success")
    return redirect(url_for("loans_list"))


@app.route("/loans/<int:loan_id>/delete", methods=["POST"])
def loans_delete(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    try:
        _delete_loan_records(loan)
        db.session.commit()
        flash("Deleted.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not delete: {exc}", "danger")
        return redirect(url_for("loans_view", loan_id=loan_id))
    return redirect(url_for("loans_list"))


@app.route("/loans/<int:loan_id>/generate-schedule", methods=["POST"])
def loans_generate_schedule(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    created = generate_collections(loan)
    db.session.commit()
    flash(f"Generated {created} new collection(s). Existing dates were skipped.", "success")
    return redirect(url_for("loans_view", loan_id=loan.id))


# ---------------------------------------------------------------------------
# Adjustments / interest actions
# ---------------------------------------------------------------------------

@app.route("/loans/<int:loan_id>/adjust", methods=["GET", "POST"])
def loans_adjust(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    adj_type = request.args.get("type", "extra_interest")
    if request.method == "POST":
        adj_type = request.form.get("adjustment_type", adj_type)
        amount = money(request.form.get("amount"))
        reason = request.form.get("reason", "").strip()
        adj_date = parse_date(request.form.get("adjustment_date"), date.today())

        if adj_type == "note":
            amount = Decimal("0.00")
        elif amount <= 0 and adj_type != "future_interest_change":
            flash("Amount must be greater than zero.", "danger")
            return render_template("loans/adjust.html", loan=loan, adj_type=adj_type)

        if not reason:
            flash("Reason is required.", "danger")
            return render_template("loans/adjust.html", loan=loan, adj_type=adj_type)

        adj = LoanAdjustment(
            loan_id=loan.id,
            adjustment_date=adj_date,
            adjustment_type=adj_type,
            amount=amount,
            reason=reason,
        )
        db.session.add(adj)
        db.session.flush()

        type_map = {
            "extra_interest": ("extra_interest", "Extra interest added"),
            "late_fee": ("late_fee", "Late fee added"),
            "waive_interest": ("waive_interest", "Interest waived/reduced"),
            "credit": ("adjustment_credit", "Credit adjustment"),
            "debit": ("adjustment_debit", "Debit adjustment"),
            "note": ("note", "Note"),
        }

        if adj_type == "future_interest_change":
            # Update loan interest settings for future collections
            method = request.form.get("interest_method")
            if method:
                loan.interest_method = method
            if request.form.get("interest_amount") is not None:
                loan.interest_amount = money(request.form.get("interest_amount"))
            if request.form.get("interest_percentage") is not None:
                loan.interest_percentage = money(request.form.get("interest_percentage"))
            add_ledger(
                loan.id,
                "note",
                Decimal("0.00"),
                f"Future interest settings changed: {reason}",
                txn_date=adj_date,
                ref_type="adjustment",
                ref_id=adj.id,
            )
            # Refresh future unpaid amounts
            bal = loan_balances(loan)
            loan._cached_remaining_principal = bal["remaining_principal"]
            new_interest = loan.scheduled_interest_amount()
            today = date.today()
            for sc in ScheduledCollection.query.filter(
                ScheduledCollection.loan_id == loan.id,
                ScheduledCollection.due_date >= today,
                ScheduledCollection.amount_paid == 0,
                ScheduledCollection.status.in_(["upcoming", "due_today"]),
            ).all():
                old = Decimal(sc.amount_due or 0)
                sc.amount_due = new_interest
                sc.interest_portion = new_interest
                diff = new_interest - old
                if diff > 0:
                    add_ledger(
                        loan.id, "interest_accrual", diff,
                        f"Interest increase for {sc.due_date}",
                        txn_date=adj_date, ref_type="scheduled_collection", ref_id=sc.id,
                    )
                elif diff < 0:
                    add_ledger(
                        loan.id, "waive_interest", abs(diff),
                        f"Interest decrease for {sc.due_date}",
                        txn_date=adj_date, ref_type="scheduled_collection", ref_id=sc.id,
                    )
        elif adj_type in type_map:
            txn_type, label = type_map[adj_type]
            add_ledger(
                loan.id,
                txn_type,
                amount,
                f"{label}: {reason}",
                txn_date=adj_date,
                ref_type="adjustment",
                ref_id=adj.id,
            )
            # Extra interest / late fee also increases the next open collection amount
            if adj_type in ("extra_interest", "late_fee") and amount > 0:
                target = (
                    ScheduledCollection.query.filter(
                        ScheduledCollection.loan_id == loan.id,
                        ScheduledCollection.status.in_(
                            ["overdue", "due_today", "partially_paid", "upcoming"]
                        ),
                    )
                    .order_by(ScheduledCollection.due_date.asc())
                    .first()
                )
                if target:
                    target.amount_due = Decimal(target.amount_due or 0) + amount
                    if adj_type == "extra_interest":
                        target.interest_portion = Decimal(target.interest_portion or 0) + amount
                    target.refresh_status()

        db.session.commit()
        flash("Adjustment saved.", "success")
        return redirect(url_for("loans_view", loan_id=loan.id))

    return render_template("loans/adjust.html", loan=loan, adj_type=adj_type)


@app.route("/collections/<int:collection_id>/delete", methods=["POST"])
def collection_delete(collection_id):
    sc = ScheduledCollection.query.get_or_404(collection_id)
    loan_id = sc.loan_id
    if Decimal(sc.amount_paid or 0) > 0:
        flash("Cannot delete a week that already has payments. Mark not paid first, or leave it.", "warning")
        return redirect(url_for("loans_view", loan_id=loan_id))

    # Clear allocation links then delete week
    PaymentAllocation.query.filter_by(scheduled_collection_id=sc.id).update(
        {"scheduled_collection_id": None}
    )
    LoanTransaction.query.filter_by(
        reference_type="scheduled_collection", reference_id=sc.id
    ).delete()
    db.session.delete(sc)
    db.session.commit()
    flash("Week deleted.", "success")
    return redirect(url_for("loans_view", loan_id=loan_id))


@app.route("/collections/<int:collection_id>/waive", methods=["POST"])
def collection_waive(collection_id):
    sc = ScheduledCollection.query.get_or_404(collection_id)
    remaining = sc.remaining
    sc.status = "waived"
    if remaining > 0:
        add_ledger(
            sc.loan_id,
            "collection_waive",
            remaining,
            f"Collection waived for {sc.due_date.isoformat()}",
            ref_type="scheduled_collection",
            ref_id=sc.id,
        )
    db.session.commit()
    flash("Collection marked as waived.", "success")
    return redirect(request.referrer or url_for("loans_view", loan_id=sc.loan_id))


@app.route("/collections/<int:collection_id>/mark-paid", methods=["POST"])
def collection_mark_paid(collection_id):
    """Quick mark: record full remaining amount as paid for this week."""
    sc = ScheduledCollection.query.get_or_404(collection_id)
    loan = sc.loan
    if sc.status == "waived":
        flash("This week is waived.", "warning")
        return redirect(request.referrer or url_for("loans_view", loan_id=loan.id))

    remaining = sc.remaining
    if remaining <= 0:
        sc.status = "paid"
        sc.refresh_status()
        db.session.commit()
        flash("Already marked paid.", "success")
        return redirect(request.referrer or url_for("loans_view", loan_id=loan.id))

    try:
        payment = Payment(
            borrower_id=loan.borrower_id,
            loan_id=loan.id,
            payment_date=date.today(),
            amount_received=remaining,
            payment_method="cash",
            notes=f"Marked paid for week {sc.due_date.isoformat()}",
        )
        db.session.add(payment)
        db.session.flush()

        # Interest first, then principal portion of this installment
        paid_so_far = Decimal(sc.amount_paid or 0)
        interest_covered = min(paid_so_far, Decimal(sc.interest_portion or 0))
        interest_left = max(Decimal(sc.interest_portion or 0) - interest_covered, Decimal("0.00"))
        to_interest = min(remaining, interest_left)
        to_principal = remaining - to_interest

        allocations = []
        if to_interest > 0:
            allocations.append(
                PaymentAllocation(
                    payment_id=payment.id,
                    allocation_type="interest",
                    amount=to_interest,
                    scheduled_collection_id=sc.id,
                )
            )
        if to_principal > 0:
            allocations.append(
                PaymentAllocation(
                    payment_id=payment.id,
                    allocation_type="principal",
                    amount=to_principal,
                    scheduled_collection_id=sc.id,
                )
            )
        for a in allocations:
            db.session.add(a)

        sc.amount_paid = Decimal(sc.amount_due or 0)
        sc.status = "paid"
        record_payment_side_effects(loan, payment, allocations, payment.payment_date)
        db.session.commit()
        flash(f"Week {sc.due_date} marked as paid.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not mark paid: {exc}", "danger")
    return redirect(request.referrer or url_for("loans_view", loan_id=loan.id))


@app.route("/collections/<int:collection_id>/mark-unpaid", methods=["POST"])
def collection_mark_unpaid(collection_id):
    """Mark week as not paid: clear paid amount and reverse linked mark/payments for this week."""
    sc = ScheduledCollection.query.get_or_404(collection_id)
    loan = sc.loan

    if sc.status == "waived":
        # Un-waive back to unpaid
        sc.status = "upcoming"
        sc.amount_paid = Decimal("0.00")
        sc.refresh_status()
        db.session.commit()
        flash(f"Week {sc.due_date} marked as not paid.", "success")
        return redirect(request.referrer or url_for("loans_view", loan_id=loan.id))

    if Decimal(sc.amount_paid or 0) <= 0:
        sc.refresh_status()
        db.session.commit()
        flash("Already marked not paid.", "success")
        return redirect(request.referrer or url_for("loans_view", loan_id=loan.id))

    try:
        # Reverse payments that allocated to this collection (keep audit trail)
        allocs = (
            PaymentAllocation.query.join(Payment)
            .filter(
                PaymentAllocation.scheduled_collection_id == sc.id,
                Payment.is_reversal.is_(False),
            )
            .all()
        )
        payment_ids = {a.payment_id for a in allocs}
        for pid in payment_ids:
            original = Payment.query.get(pid)
            if not original:
                continue
            already = Payment.query.filter_by(
                reverses_payment_id=original.id, is_reversal=True
            ).first()
            if already:
                continue

            # Only auto-reverse if this payment was solely for this week, or was a "Marked paid" note
            other = (
                PaymentAllocation.query.filter(
                    PaymentAllocation.payment_id == original.id,
                    PaymentAllocation.scheduled_collection_id != sc.id,
                ).count()
            )
            marked = (original.notes or "").startswith("Marked paid")
            if other > 0 and not marked:
                # Partial: only undo allocations for this collection
                for a in allocs:
                    if a.payment_id != original.id:
                        continue
                    # Create a small reversing payment for this allocation only
                    rev = Payment(
                        borrower_id=original.borrower_id,
                        loan_id=original.loan_id,
                        payment_date=date.today(),
                        amount_received=a.amount,
                        payment_method=original.payment_method,
                        notes=f"REVERSAL (mark unpaid) of allocation from payment #{original.id}",
                        is_reversal=True,
                        reverses_payment_id=original.id,
                    )
                    db.session.add(rev)
                    db.session.flush()
                    db.session.add(
                        PaymentAllocation(
                            payment_id=rev.id,
                            allocation_type=a.allocation_type,
                            amount=a.amount,
                            scheduled_collection_id=sc.id,
                        )
                    )
                    add_ledger(
                        loan.id,
                        "reversal",
                        a.amount,
                        f"Mark unpaid week {sc.due_date.isoformat()} (from payment #{original.id})",
                        ref_type="payment",
                        ref_id=rev.id,
                    )
            else:
                rev = Payment(
                    borrower_id=original.borrower_id,
                    loan_id=original.loan_id,
                    payment_date=date.today(),
                    amount_received=original.amount_received,
                    payment_method=original.payment_method,
                    notes=f"REVERSAL (mark unpaid) of payment #{original.id}",
                    is_reversal=True,
                    reverses_payment_id=original.id,
                )
                db.session.add(rev)
                db.session.flush()
                for a in original.allocations:
                    db.session.add(
                        PaymentAllocation(
                            payment_id=rev.id,
                            allocation_type=a.allocation_type,
                            amount=a.amount,
                            scheduled_collection_id=a.scheduled_collection_id,
                        )
                    )
                add_ledger(
                    loan.id,
                    "reversal",
                    original.amount_received,
                    f"Mark unpaid week {sc.due_date.isoformat()}: reversed payment #{original.id}",
                    ref_type="payment",
                    ref_id=rev.id,
                )

        sc.amount_paid = Decimal("0.00")
        if sc.status == "waived":
            sc.status = "upcoming"
        sc.refresh_status()
        db.session.commit()
        flash(f"Week {sc.due_date} marked as not paid.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not mark unpaid: {exc}", "danger")
    return redirect(request.referrer or url_for("loans_view", loan_id=loan.id))


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

@app.route("/collections")
def collections_list():
    refresh_collection_statuses()
    view = request.args.get("view", "week")  # week | day | overdue
    today = date.today()
    week_start, week_end = week_bounds(today)

    query = ScheduledCollection.query.join(Loan).join(Borrower)

    if view == "day":
        day = parse_date(request.args.get("date"), today)
        query = query.filter(ScheduledCollection.due_date == day)
        title = f"Collections for {day.isoformat()}"
        period = (day, day)
    elif view == "overdue":
        query = query.filter(ScheduledCollection.status == "overdue")
        title = "Overdue Collections"
        period = (None, None)
        day = today
    else:
        query = query.filter(
            ScheduledCollection.due_date >= week_start,
            ScheduledCollection.due_date <= week_end,
        )
        title = f"Weekly Collections ({week_start.isoformat()} – {week_end.isoformat()})"
        period = (week_start, week_end)
        day = today

    status_filter = request.args.get("status")
    if status_filter:
        query = query.filter(ScheduledCollection.status == status_filter)

    rows = query.order_by(ScheduledCollection.due_date.asc(), Borrower.name.asc()).all()
    return render_template(
        "collections/list.html",
        rows=rows,
        view=view,
        title=title,
        period=period,
        day=day,
        week_start=week_start,
        week_end=week_end,
        status_filter=status_filter,
    )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@app.route("/payments")
def payments_list():
    q = request.args.get("q", "").strip()
    query = Payment.query.join(Borrower).join(Loan)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Borrower.name.like(like), Borrower.phone.like(like)))
    payments = query.order_by(Payment.payment_date.desc(), Payment.id.desc()).limit(200).all()
    return render_template("payments/list.html", payments=payments, q=q)


@app.route("/payments/add", methods=["GET", "POST"])
def payments_add():
    borrowers = Borrower.query.filter_by(status="active").order_by(Borrower.name).all()
    loans = Loan.query.filter(Loan.status.in_(["active", "paused"])).order_by(Loan.id.desc()).all()
    pre_loan = request.args.get("loan_id", type=int)
    pre_borrower = request.args.get("borrower_id", type=int)
    pre_collection = request.args.get("collection_id", type=int)

    if request.method == "POST":
        loan_id = request.form.get("loan_id", type=int)
        amount = money(request.form.get("amount_received"))
        payment_date = parse_date(request.form.get("payment_date"), date.today())
        method = request.form.get("payment_method", "cash")
        notes = request.form.get("notes", "").strip() or None

        loan = Loan.query.get(loan_id) if loan_id else None
        if not loan or amount <= 0:
            flash("Loan and a positive amount are required.", "danger")
            return render_template(
                "payments/form.html",
                borrowers=borrowers,
                loans=loans,
                pre_loan=pre_loan,
                pre_borrower=pre_borrower,
                pre_collection=pre_collection,
            )

        # Manual allocation overrides (optional)
        alloc_interest = money(request.form.get("alloc_interest"), "0")
        alloc_principal = money(request.form.get("alloc_principal"), "0")
        alloc_other = money(request.form.get("alloc_other"), "0")
        manual = alloc_interest + alloc_principal + alloc_other

        try:
            payment = Payment(
                borrower_id=loan.borrower_id,
                loan_id=loan.id,
                payment_date=payment_date,
                amount_received=amount,
                payment_method=method,
                notes=notes,
            )
            db.session.add(payment)
            db.session.flush()

            if manual > 0:
                if manual != amount:
                    flash("Allocation total must equal amount received.", "danger")
                    db.session.rollback()
                    return render_template(
                        "payments/form.html",
                        borrowers=borrowers,
                        loans=loans,
                        pre_loan=loan.id,
                        pre_borrower=loan.borrower_id,
                        pre_collection=pre_collection,
                    )
                allocations = []
                # Apply interest allocation to oldest open collections first
                leftover_interest = alloc_interest
                leftover_principal = alloc_principal
                collections = (
                    ScheduledCollection.query.filter(
                        ScheduledCollection.loan_id == loan.id,
                        ScheduledCollection.status.in_(
                            ["overdue", "due_today", "partially_paid", "upcoming"]
                        ),
                    )
                    .order_by(ScheduledCollection.due_date.asc())
                    .all()
                )
                for sc in collections:
                    if leftover_interest <= 0 and leftover_principal <= 0:
                        break
                    need = sc.remaining
                    if need <= 0:
                        continue
                    take_i = min(leftover_interest, need)
                    if take_i > 0:
                        allocations.append(
                            PaymentAllocation(
                                payment_id=payment.id,
                                allocation_type="interest",
                                amount=take_i,
                                scheduled_collection_id=sc.id,
                            )
                        )
                        sc.amount_paid = Decimal(sc.amount_paid or 0) + take_i
                        leftover_interest -= take_i
                        need -= take_i
                    take_p = min(leftover_principal, need)
                    if take_p > 0:
                        allocations.append(
                            PaymentAllocation(
                                payment_id=payment.id,
                                allocation_type="principal",
                                amount=take_p,
                                scheduled_collection_id=sc.id,
                            )
                        )
                        sc.amount_paid = Decimal(sc.amount_paid or 0) + take_p
                        leftover_principal -= take_p
                    sc.refresh_status()

                if leftover_interest > 0:
                    allocations.append(
                        PaymentAllocation(
                            payment_id=payment.id,
                            allocation_type="interest",
                            amount=leftover_interest,
                        )
                    )
                if leftover_principal > 0:
                    allocations.append(
                        PaymentAllocation(
                            payment_id=payment.id,
                            allocation_type="principal",
                            amount=leftover_principal,
                        )
                    )
                if alloc_other > 0:
                    allocations.append(
                        PaymentAllocation(
                            payment_id=payment.id,
                            allocation_type="other",
                            amount=alloc_other,
                        )
                    )
                for a in allocations:
                    db.session.add(a)
            else:
                allocations = apply_payment_to_collections(loan, payment, amount)

            record_payment_side_effects(loan, payment, allocations, payment_date)
            db.session.commit()
            flash("Payment recorded.", "success")
            return redirect(url_for("loans_view", loan_id=loan.id))
        except Exception as exc:
            db.session.rollback()
            flash(f"Payment failed: {exc}", "danger")

    return render_template(
        "payments/form.html",
        borrowers=borrowers,
        loans=loans,
        pre_loan=pre_loan,
        pre_borrower=pre_borrower,
        pre_collection=pre_collection,
    )


@app.route("/payments/<int:payment_id>")
def payments_view(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    return render_template("payments/view.html", payment=payment)


@app.route("/payments/<int:payment_id>/reverse", methods=["POST"])
def payments_reverse(payment_id):
    original = Payment.query.get_or_404(payment_id)
    if original.is_reversal:
        flash("Cannot reverse a reversal entry.", "danger")
        return redirect(url_for("payments_view", payment_id=original.id))

    existing = Payment.query.filter_by(reverses_payment_id=original.id, is_reversal=True).first()
    if existing:
        flash("This payment was already reversed.", "warning")
        return redirect(url_for("payments_view", payment_id=original.id))

    reason = request.form.get("reason", "").strip() or "Payment correction/reversal"

    try:
        reversal = Payment(
            borrower_id=original.borrower_id,
            loan_id=original.loan_id,
            payment_date=date.today(),
            amount_received=original.amount_received,
            payment_method=original.payment_method,
            notes=f"REVERSAL of payment #{original.id}: {reason}",
            is_reversal=True,
            reverses_payment_id=original.id,
        )
        db.session.add(reversal)
        db.session.flush()

        # Mirror allocations and undo collection amounts.
        # Balances come from allocations (reversals are subtracted), so we do not
        # re-accrue interest or re-debit principal in the ledger.
        for a in original.allocations:
            db.session.add(
                PaymentAllocation(
                    payment_id=reversal.id,
                    allocation_type=a.allocation_type,
                    amount=a.amount,
                    scheduled_collection_id=a.scheduled_collection_id,
                )
            )
            if a.scheduled_collection_id:
                sc = ScheduledCollection.query.get(a.scheduled_collection_id)
                if sc:
                    sc.amount_paid = max(
                        Decimal(sc.amount_paid or 0) - Decimal(a.amount), Decimal("0.00")
                    )
                    if sc.status != "waived":
                        sc.refresh_status()

        add_ledger(
            original.loan_id,
            "reversal",
            original.amount_received,
            f"Payment #{original.id} reversed: {reason}",
            ref_type="payment",
            ref_id=reversal.id,
        )
        db.session.commit()
        flash("Payment reversed. Original record kept for audit.", "success")
        return redirect(url_for("loans_view", loan_id=original.loan_id))
    except Exception as exc:
        db.session.rollback()
        flash(f"Reversal failed: {exc}", "danger")
        return redirect(url_for("payments_view", payment_id=original.id))


# ---------------------------------------------------------------------------
# CLI / startup
# ---------------------------------------------------------------------------

@app.cli.command("init-db")
def init_db():
    """Create all tables."""
    db.create_all()
    print("Database tables created.")


def create_tables():
    with app.app_context():
        db.create_all()


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000, debug=Config.DEBUG)
