"""Insert sample borrowers, loans, weeks, and payments. Safe to run once."""

from datetime import date, timedelta
from decimal import Decimal

from app import (
    _create_week_collection,
    _set_week_maaf,
    add_ledger,
    app,
    apply_payment_to_collections,
    record_payment_side_effects,
)
from models import BalanceEntry, Borrower, Loan, Payment, ScheduledCollection, db, refresh_collection_statuses


def D(value):
    return Decimal(value).quantize(Decimal("0.01"))


def week_row(due, interest, extra_type="none", extra_amount="0"):
    interest = D(interest)
    extra = D(extra_amount)
    if extra_type == "none":
        extra = D("0")
    return {
        "due_date": due,
        "interest": interest,
        "extra_type": extra_type,
        "extra_amount": extra,
        "amount_due": interest + extra,
    }


def thursdays(today, past, future):
    return [today - timedelta(weeks=n) for n in range(past, 0, -1)] + [
        today + timedelta(weeks=n) for n in range(future + 1)
    ]


def add_person(name, phone, address, notes, added, status="active"):
    person = Borrower(
        name=name,
        phone=phone,
        address=address,
        notes=notes,
        date_added=added,
        status=status,
    )
    db.session.add(person)
    db.session.flush()
    return person


def add_loan(person, principal, interest, loan_date, first_due, last_due, notes, status="active"):
    loan = Loan(
        borrower_id=person.id,
        principal_amount=D(principal),
        loan_date=loan_date,
        collection_frequency="weekly",
        first_collection_date=first_due,
        end_date=last_due,
        interest_method="fixed_weekly",
        interest_amount=D(interest),
        interest_percentage=D("0"),
        status=status,
        notes=notes,
    )
    db.session.add(loan)
    db.session.flush()
    add_ledger(
        loan.id,
        "disbursement",
        D(principal),
        "Loan principal disbursed",
        txn_date=loan_date,
        ref_type="loan",
        ref_id=loan.id,
    )
    return loan


def add_weeks(loan, dates, interest, fines=None):
    fines = fines or {}
    for due in dates:
        extra_type, extra_amount = fines.get(due, ("none", "0"))
        _create_week_collection(loan, week_row(due, interest, extra_type, extra_amount))


def pay(loan, amount, on_date, notes, method="cash"):
    payment = Payment(
        borrower_id=loan.borrower_id,
        loan_id=loan.id,
        payment_date=on_date,
        amount_received=D(amount),
        payment_method=method,
        notes=notes,
    )
    db.session.add(payment)
    db.session.flush()
    allocations = apply_payment_to_collections(loan, payment, D(amount))
    record_payment_side_effects(loan, payment, allocations, on_date)
    return payment


def seed():
    if Borrower.query.filter_by(name="Ramesh Kumar").first():
        print("Dummy data already present.")
        return

    today = date.today()
    monday = today - timedelta(days=today.weekday())

    # Ramesh: current. Past weeks paid, today still due.
    ramesh_dates = thursdays(today, past=6, future=3)
    ramesh = add_person(
        "Ramesh Kumar",
        "9876543210",
        "14 Gandhi Nagar, Pune",
        "Shopkeeper. Pays most weeks on time.",
        ramesh_dates[0] - timedelta(days=3),
    )
    ramesh_loan = add_loan(
        ramesh, "50000", "1500", ramesh.date_added, ramesh_dates[0], ramesh_dates[-1],
        "Weekly interest, capital still out.",
    )
    add_weeks(ramesh_loan, ramesh_dates, "1500")
    for due in ramesh_dates:
        if due < today:
            pay(ramesh_loan, "1500", due, f"Weekly interest {due.isoformat()}")

    # Sita: several overdue weeks, including today.
    sita_dates = thursdays(today, past=5, future=2)
    sita = add_person(
        "Sita Devi",
        "9123456780",
        "88 Station Road, Nashik",
        "Missed the last few weeks.",
        sita_dates[0] - timedelta(days=2),
    )
    sita_loan = add_loan(
        sita, "25000", "800", sita.date_added, sita_dates[0], sita_dates[-1],
        "Call before visiting.",
    )
    add_weeks(sita_loan, sita_dates, "800")
    for due in sita_dates:
        if due <= today - timedelta(weeks=3):
            pay(sita_loan, "800", due, f"Weekly interest {due.isoformat()}", method="upi")

    # Imran: one partial overdue week, and a partial payment today.
    imran_dates = thursdays(today, past=4, future=3)
    imran = add_person(
        "Imran Sheikh",
        "9988776655",
        "3 Market Yard, Aurangabad",
        "Pays in parts when the shop is slow.",
        imran_dates[0] - timedelta(days=4),
    )
    imran_loan = add_loan(
        imran, "80000", "2000", imran.date_added, imran_dates[0], imran_dates[-1],
        "Largest open loan.",
    )
    add_weeks(imran_loan, imran_dates, "2000")
    for due in imran_dates:
        if due < today - timedelta(weeks=1):
            pay(imran_loan, "2000", due, f"Weekly interest {due.isoformat()}")
    pay(imran_loan, "700", today - timedelta(weeks=1), "Partial week payment", method="upi")
    pay(imran_loan, "500", today, "Partial payment today", method="cash")

    # Lakshmi: this Monday is already collected. One older week was waived.
    lakshmi_dates = [monday - timedelta(weeks=n) for n in range(4, 0, -1)] + [
        monday,
        monday + timedelta(weeks=1),
        monday + timedelta(weeks=2),
    ]
    lakshmi = add_person(
        "Lakshmi Nair",
        "9000011122",
        "22 Temple Street, Kolhapur",
        "Collects on Monday.",
        lakshmi_dates[0] - timedelta(days=1),
    )
    lakshmi_loan = add_loan(
        lakshmi, "15000", "600", lakshmi.date_added, lakshmi_dates[0], lakshmi_dates[-1],
        "Monday collection.",
    )
    add_weeks(lakshmi_loan, lakshmi_dates, "600")
    waived = ScheduledCollection.query.filter_by(
        loan_id=lakshmi_loan.id, due_date=lakshmi_dates[1]
    ).first()
    _set_week_maaf(waived)
    for due in lakshmi_dates:
        if due == lakshmi_dates[1] or due > monday:
            continue
        pay(lakshmi_loan, "600", due, f"Monday collection {due.isoformat()}")

    # Arjun: paused, with overdue weeks left open.
    arjun_dates = thursdays(today, past=4, future=1)
    arjun = add_person(
        "Arjun Patel",
        "9811122233",
        "7 Ring Road, Nagpur",
        "Asked to pause while travelling.",
        arjun_dates[0] - timedelta(days=5),
    )
    arjun_loan = add_loan(
        arjun, "40000", "1000", arjun.date_added, arjun_dates[0], arjun_dates[-1],
        "Paused at the borrower's request.",
        status="paused",
    )
    add_weeks(arjun_loan, arjun_dates, "1000")
    pay(arjun_loan, "1000", arjun_dates[0], f"Weekly interest {arjun_dates[0].isoformat()}")

    # Meena: closed. Every week was paid.
    meena_dates = [today - timedelta(weeks=n) for n in range(8, 4, -1)]
    meena = add_person(
        "Meena Joshi",
        "9765432109",
        "51 College Road, Pune",
        "Loan closed last month.",
        meena_dates[0] - timedelta(days=2),
        status="inactive",
    )
    meena_loan = add_loan(
        meena, "10000", "400", meena.date_added, meena_dates[0], meena_dates[-1],
        "Closed after the last week.",
        status="closed",
    )
    add_weeks(meena_loan, meena_dates, "400")
    for due in meena_dates:
        pay(meena_loan, "400", due, f"Weekly interest {due.isoformat()}")

    # Kavita: overdue week with a fine, and today still due.
    kavita_dates = thursdays(today, past=2, future=2)
    kavita = add_person(
        "Kavita Singh",
        "9090909090",
        "19 Lake View, Satara",
        "Fine added on the missed week.",
        kavita_dates[0] - timedelta(days=2),
    )
    kavita_loan = add_loan(
        kavita, "20000", "750", kavita.date_added, kavita_dates[0], kavita_dates[-1],
        "New loan.",
    )
    fine_due = today - timedelta(weeks=1)
    add_weeks(kavita_loan, kavita_dates, "750", fines={fine_due: ("fine", "150")})
    pay(kavita_loan, "750", kavita_dates[0], f"Weekly interest {kavita_dates[0].isoformat()}")

    # New loan that has not reached its first collection yet.
    farooq_dates = [today + timedelta(weeks=n) for n in range(1, 5)]
    farooq = add_person(
        "Farooq Ali",
        "9345678901",
        "6 Mill Road, Solapur",
        "First collection is next week.",
        today - timedelta(days=6),
    )
    farooq_loan = add_loan(
        farooq, "12000", "500", farooq.date_added, farooq_dates[0], farooq_dates[-1],
        "Nothing due yet.",
    )
    add_weeks(farooq_loan, farooq_dates, "500")

    db.session.add_all(
        [
            BalanceEntry(entry_date=today - timedelta(days=10), entry_type="in", amount=D("25000"), notes="Opening cash"),
            BalanceEntry(entry_date=today - timedelta(days=3), entry_type="in", amount=D("4500"), notes="Collections deposited"),
            BalanceEntry(entry_date=today - timedelta(days=2), entry_type="out", amount=D("800"), notes="Petrol"),
            BalanceEntry(entry_date=today - timedelta(days=1), entry_type="out", amount=D("350"), notes="Tea and stationery"),
            BalanceEntry(entry_date=today, entry_type="in", amount=D("1100"), notes="Partial collections today"),
        ]
    )

    refresh_collection_statuses()
    db.session.commit()
    print(f"Seeded dummy data for {today.isoformat()}.")


if __name__ == "__main__":
    with app.app_context():
        seed()
