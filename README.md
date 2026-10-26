# Loan Counter

Simple loan management website for a small finance company.

**Stack:** Python, Flask, MySQL, Flask-SQLAlchemy, Bootstrap 5, Jinja2, vanilla JavaScript.

**Important:** There is no login. Use this only as a local tool, or put it behind VPN / reverse-proxy authentication before storing real borrower data.

## Features

- Live dashboard statistics from MySQL
- Borrowers: add, search, edit, deactivate, profile with loans and payments
- Loans: flexible interest methods, pause/resume/close, per-loan schedules
- Weekly / daily / overdue collection tracker
- Payment recording with partial payments, allocations, and reversals
- Adjustments: extra interest, late fees, waivers, credits/debits
- Transaction ledger as the financial source of truth

## Project structure

```
loan-counter/
  app.py
  config.py
  models.py
  requirements.txt
  .env.example
  templates/
  static/
  README.md
```

## Windows setup (PowerShell)

### 1. Install prerequisites

- Python 3.10+ from [python.org](https://www.python.org/downloads/)
- MySQL 8.x (or MariaDB) with a local server running

### 2. Create the database

In MySQL (Workbench, CLI, or PowerShell if `mysql` is on PATH):

```sql
CREATE DATABASE loan_counter CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Optional dedicated user:

```sql
CREATE USER 'loanapp'@'localhost' IDENTIFIED BY 'your_strong_password';
GRANT ALL PRIVILEGES ON loan_counter.* TO 'loanapp'@'localhost';
FLUSH PRIVILEGES;
```

### 3. Create virtual environment and install packages

```powershell
cd D:\Loan\loan-counter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If script execution is blocked:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 4. Configure environment

```powershell
Copy-Item .env.example .env
notepad .env
```

Set at least:

```
FLASK_SECRET_KEY=some-long-random-string
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=loan_counter
```

### 5. Create tables and run

Tables are created automatically on first run:

```powershell
python app.py
```

Or explicitly:

```powershell
flask --app app init-db
python app.py
```

Open **http://127.0.0.1:5000**

## Quick test checklist

1. Add a borrower
2. Create a loan with fixed weekly interest and a first collection date
3. Open **Collections** → confirm this week’s rows
4. Record a full payment and a partial payment
5. Leave a collection unpaid → status becomes overdue; unpaid amount remains
6. Add extra interest / late fee / waiver from the loan page
7. Record a principal repayment (allocate to principal or overpay)
8. Close a loan (force-close if balance remains)
9. Confirm dashboard numbers update
10. Run **Generate more collections** twice — no duplicate due dates

## Ubuntu server deployment (later)

Treat this as an internal app. Do **not** expose port 5000 publicly without access control.

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip mysql-server nginx
```

### 2. Database

```bash
sudo mysql -e "CREATE DATABASE loan_counter CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER 'loanapp'@'localhost' IDENTIFIED BY 'your_strong_password';"
sudo mysql -e "GRANT ALL PRIVILEGES ON loan_counter.* TO 'loanapp'@'localhost'; FLUSH PRIVILEGES;"
```

### 3. App directory

```bash
sudo mkdir -p /opt/loan-counter
sudo chown $USER:$USER /opt/loan-counter
# copy project files into /opt/loan-counter
cd /opt/loan-counter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install gunicorn
cp .env.example .env
nano .env
```

### 4. systemd service

`/etc/systemd/system/loan-counter.service`:

```ini
[Unit]
Description=Loan Counter
After=network.target mysql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/loan-counter
Environment="PATH=/opt/loan-counter/.venv/bin"
ExecStart=/opt/loan-counter/.venv/bin/gunicorn -b 127.0.0.1:8000 -w 2 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now loan-counter
```

### 5. Nginx reverse proxy + access restriction

Example: allow only your office IP, or put HTTP basic auth / VPN in front.

```nginx
server {
    listen 80;
    server_name loan.example.com;

    # Example IP allow-list (adjust):
    # allow 203.0.113.10;
    # deny all;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Then enable the site and reload Nginx.

## Interest methods

| Method | Behaviour |
|--------|-----------|
| `fixed_weekly` | Uses **Interest amount** each weekly period |
| `monthly` | Uses **Interest amount** each monthly period |
| `percentage` | Percentage of original principal each period |
| `simple_on_outstanding` | Percentage (or amount) on remaining principal — not compounded into principal unless you explicitly adjust |

Unpaid interest is kept on the collection row and carried as overdue; it is **not** auto-added to principal.

## Database tables

- `borrowers`
- `loans`
- `scheduled_collections` (unique on `loan_id` + `due_date`)
- `payments`
- `payment_allocations`
- `loan_transactions`
- `loan_adjustments`

Money fields use `DECIMAL`. Payments run inside a DB transaction so allocations and ledger stay consistent. Payments are never deleted; use **Reverse / correct** instead.
