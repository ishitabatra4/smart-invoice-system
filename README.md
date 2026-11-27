# Smart Invoice System

**Invoice Management System with OCR-assisted extraction, GST working papers and audited financial records**
B.Tech (Computer Science and Engineering) final-year project · Django 5.2 · Python 3.10 · SQLite

Smart Invoice System is a web application for small Indian businesses. It handles sales, purchase and expense invoices from creation or upload through to payment, tax preparation and audit. Uploaded PDFs and images are read with local OCR and rule-based extraction. A person then reviews every extracted figure before it can affect the books. All money is calculated with exact decimal arithmetic, and every important action is recorded in a tamper-evident audit trail.

> **Academic project.** GST and income-tax outputs are *preparation aids*, not official returns or tax advice. OCR extraction is assistive and always requires human review. The application has not been deployed to production or security-certified.

---

## Contents

1. [Objectives](#1-objectives)
2. [Key features and modules](#2-key-features-and-modules)
3. [Technology stack](#3-technology-stack)
4. [System architecture](#4-system-architecture)
5. [Prerequisites](#5-prerequisites)
6. [Fresh installation](#6-fresh-installation)
7. [Environment configuration](#7-environment-configuration)
8. [Database migrations](#8-database-migrations)
9. [Running the application](#9-running-the-application)
10. [Demonstration environment](#10-demonstration-environment)
11. [Session security configuration](#11-session-security-configuration)
12. [Testing](#12-testing)
13. [Backup and restoration](#13-backup-and-restoration)
14. [Project structure](#14-project-structure)
15. [Known limitations and future scope](#15-known-limitations-and-future-scope)
16. [Documentation index](#16-documentation-index)

---

## 1. Objectives

* Replace manual invoice entry with **upload → extract → review → verify**, where a person always approves the numbers.
* Keep **GST-correct invoices** (CGST/SGST for intra-state, IGST for inter-state supplies) with exact rupee-and-paise arithmetic.
* Track **payments, receivables and payables** per customer and supplier, including partial payments and credit/debit notes.
* Prepare **GST and income working papers** and **reconcile purchases with GSTR-2B** data.
* Keep every business's data strictly separate, and record changes in an **audit log that can be verified**.

## 2. Key features and modules

| Module (Django app) | Features |
|---|---|
| **Accounts** (`accounts`) | Email + password registration with email verification; password reset and change; business profile (GSTIN, PAN, state, financial year); login rate limiting; server-side session security (idle/absolute timeout, safe redirects) |
| **Invoice management** (`invoices`) | Manual sales/purchase invoices with line items, discounts and GST; finalized-invoice PDFs (A4) and email delivery; upload of PDF/PNG/JPEG invoices with OCR extraction, confidence and warnings, duplicate detection and a mandatory review/verification step; batch upload with Excel export; extraction history |
| **Parties** (`invoices`) | Customers and suppliers (including dual-role parties), GSTIN validation, party ledger with CSV/Excel export |
| **Commercial documents** (`invoices`) | Quotations and proforma invoices with PDFs, issue/cancel lifecycle and conversion to invoices |
| **Adjustments** (`invoices`) | Issued and received credit and debit notes linked to invoices, reflected in outstanding balances and reports |
| **Finance** (`invoices`, `expenses`) | Payments (full and partial), outstanding dashboard, overdue tracking; expenses with categories and private receipt attachments; recurring expense schedules; ITC eligibility review; expense reports (CSV, Excel, PDF) |
| **Taxation** (`taxation`) | GST and income-tax preparation working papers for a period, with preview, review status and exports; GSTR-2B reconciliation (Excel/JSON upload, automatic and manual matching, mismatch classification) |
| **Reporting** (`analytics`) | Dashboard with date ranges: sales, purchases, expenses, GST components, receivables, payables and monthly charts |
| **Administration** (`audit`) | Audit log with before/after values and a hash-chained security record; security monitor with deterministic alert rules (failed-login bursts, access-denial bursts, export bursts); active session list with password-confirmed revocation; audit and security exports |

The sidebar groups these as **Overview · Invoice Management · Party Management · Commercial Documents · Adjustments · Finance · Taxation · Reporting · Administration**.

## 3. Technology stack

| Layer | Technology |
|---|---|
| Language / framework | Python 3.10, Django 5.2.19 (server-rendered templates) |
| Database | SQLite 3 (Django ORM, foreign keys and constraints enforced) |
| Frontend | Django templates, Bootstrap 5.3 (CDN), vanilla JavaScript, Chart.js-based dashboards |
| OCR and documents | Tesseract 5 via `pytesseract`, `pdfplumber` / `pdfminer.six`, `pypdfium2` (PDF rasterisation), Pillow |
| Outputs | ReportLab (PDF), openpyxl (Excel), CSV |
| Security | Django session authentication, CSRF protection, PBKDF2 password hashing, HMAC-keyed audit identities, SHA-256 hash chain |
| Testing | Django `TestCase` (987 tests), Node.js built-in test runner for JavaScript (55 tests) |

## 4. System architecture

```
Browser (HTML forms + small JS enhancements)
   │  HTTPS in production · CSRF token on every POST · HttpOnly session cookie
   ▼
Django middleware
   SecurityMiddleware → SessionMiddleware → CSRF → Authentication
   → AuditForbiddenResponseMiddleware (revoked sessions, denial monitoring)
   → SecurityBoundaryMiddleware (no-store cache headers)
   → Messages → SessionExpiryMiddleware (30 min idle / 12 h absolute)
   ▼
Views (login_required, every query scoped to the signed-in user's business)
   ├── accounts   ├── invoices (+ extraction, PDFs, parties, payments, notes, commercial)
   ├── expenses   ├── taxation  ├── analytics  └── audit (events, security, sessions)
   ▼
Services: Decimal GST calculation · OCR/extraction pipeline · ledger and outstanding
          · reporting · audit record_event → seal_event (hash chain) → alert rules
   ▼
SQLite database (48 tables)   +   private file storage (media/, private_expense_files/)
```

Uploaded documents are never served from a public URL. Every download goes through an owner-checked Django view.

## 5. Prerequisites

| Requirement | Version used | Notes |
|---|---|---|
| Python | **3.10.x** | Pinned packages are tested on 3.10.21 |
| pip / venv | bundled with Python | — |
| Tesseract OCR | 5.x with the `eng` language (and `osd`) | Needed only for OCR of scanned uploads and the 3 OCR tests; text-based PDFs work without it |
| Node.js | 18+ | Only for running the JavaScript tests |
| Git | any | — |

Install Tesseract:

```bash
# macOS (Homebrew)
brew install tesseract
# Ubuntu / Debian
sudo apt-get install tesseract-ocr tesseract-ocr-eng
# Windows: install the UB Mannheim build and add its folder to PATH
tesseract --version
tesseract --list-langs
```

`pytesseract` finds the `tesseract` executable on your `PATH`. If it is installed somewhere else, add that folder to `PATH` before starting Django.

## 6. Fresh installation

```bash
git clone https://github.com/ishitabatra4/smart-invoice-system.git
cd smart-invoice-system

python3.10 -m venv env
source env/bin/activate            # Windows: env\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip check

cp .env.example .env               # then edit .env (section 7)
python manage.py migrate
python manage.py check
python manage.py createsuperuser   # optional: Django admin access
python manage.py runserver
```

Open http://127.0.0.1:8000/, choose **Create an account** and verify the email address. With the local `locmem` email backend, verification emails are not delivered, so either configure SMTP (section 7) or create the user with `createsuperuser` and sign in with that email address.

## 7. Environment configuration

Settings are read from the process environment, then from a local `.env` file (literal `KEY=value`, no shell expansion). **Never commit `.env`.** `.env.example` contains placeholders only.

| Variable | Default | Purpose |
|---|---|---|
| `DEBUG` | `true` | `false` in production, which enables Secure cookies, HTTPS redirect and HSTS |
| `SECRET_KEY` | random per run in DEBUG | **Required** when `DEBUG=false`. Generate one with `python -c "import secrets;print(secrets.token_urlsafe(64))"` |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` (DEBUG) | Required in production |
| `CSRF_TRUSTED_ORIGINS` | empty | HTTPS origins in production |
| `SQLITE_PATH` | `./db.sqlite3` | Absolute path to use a different database file |
| `EMAIL_BACKEND` | `locmem` if unset | Use `django.core.mail.backends.smtp.EmailBackend` with `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (for Gmail, an **App Password**), `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` |
| `PASSWORD_RESET_TIMEOUT` / `EMAIL_VERIFICATION_TIMEOUT` | 3600 / 86400 s | Link validity |
| `TRUST_PROXY_HTTPS` | `false` | Enable only behind a proxy that sets `X-Forwarded-Proto` |
| `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` | 1 year (prod) / false / false | HSTS policy |
| `GST_ALLOWED_RATES` | `0,5,12,18,28,40` | Selectable GST percentages |
| `SESSION_*` | see section 11 | Session lifetime |

## 8. Database migrations

```bash
python manage.py migrate                         # apply all migrations
python manage.py showmigrations                  # list applied migrations
python manage.py makemigrations --check --dry-run   # confirm models and migrations agree
```

A fresh database has 48 tables. Make a backup before migrating an existing database (section 13).

## 9. Running the application

```bash
source env/bin/activate
python manage.py runserver            # http://127.0.0.1:8000/
```

Main areas: `/analytics/` (dashboard), `/invoices/`, `/invoices/upload/`, `/invoices/parties/`, `/invoices/commercial/`, `/invoices/adjustments/`, `/expenses/`, `/tax/prepare/`, `/tax/reconciliation/`, `/audit/`, `/audit/monitor/`, `/audit/monitor/sessions/`, `/admin/`.

## 10. Demonstration environment

The project has a fully **isolated, fictional** demo that never touches the development database. It uses its own settings, a separate database at `.demo/demo.sqlite3`, separate storage, separate cookies and in-memory email:

```bash
python -B manage.py setup_demo --settings=config.demo
python -B manage.py setup_demo --set-password lumen-owner --settings=config.demo        # hidden prompt
python -B manage.py setup_demo --set-password cobalt-owner --settings=config.demo
python -B manage.py setup_demo --set-password security-reviewer --settings=config.demo
python -B manage.py runserver 127.0.0.1:8001 --noreload --settings=config.demo
python -B manage.py verify_demo --settings=config.demo                                  # route/isolation checks
```

Sign in at http://127.0.0.1:8001/accounts/login/ as `lumen-owner@example.test`, `cobalt-owner@example.test` (two separate businesses) or `security-reviewer@example.test` (security staff). Initial demo accounts have unusable passwords until you set them with `--set-password`, and `verify_demo` may end existing demo sessions, so run it before browsing. The fictional upload library is in `sample_data/` (see `sample_data/README.md`).

*The author's own SmartKart Traders demonstration workspace lives in the local, unpublished database and is not part of the repository.*

## 11. Session security configuration

The app uses Django's **server-side session authentication**. There is no JWT or API token login.

| Variable | Default | Effect |
|---|---|---|
| `SESSION_IDLE_TIMEOUT` | `1800` (30 min) | Signs the user out after inactivity (allowed 60 s – 24 h) |
| `SESSION_ABSOLUTE_TIMEOUT` | `43200` (12 h) | Maximum session length; also sets the cookie and server row lifetime |
| `SESSION_ACTIVITY_UPDATE_INTERVAL` | `60` | Activity is saved at most once per minute |
| `SESSION_WARNING_SECONDS` | `120` | On-page warning before expiry, with **Stay signed in** |
| `SESSION_EXPIRE_AT_BROWSER_CLOSE` | `false` | Set `true` on shared computers |

Behaviour: a new session ID on every login; generic login errors; at most 10 login attempts per 15 minutes per IP; POST-only CSRF-protected logout that deletes the server session; `Cache-Control: no-store` on signed-in pages; background (`X-Session-Activity: passive`) requests never extend a session; expired sessions go to the login page with a message; users can list and revoke their own sessions (password required); a password change signs out other sessions and a reset signs out all. Cookies are `HttpOnly` and `SameSite=Lax` everywhere and `Secure` when `DEBUG=false`. Local `http://127.0.0.1:8000` keeps working because Secure cookies are only enabled in production mode.

To see the timeout quickly during testing:

```bash
SESSION_IDLE_TIMEOUT=120 SESSION_WARNING_SECONDS=60 python manage.py runserver
```

Automated coverage: `accounts/test_session_security.py` (25 tests) and `accounts/session_timeout.test.cjs` (4 tests).

## 12. Testing

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test                                   # complete Django suite
python manage.py test accounts.test_session_security    # session security only
node --test $(find . -name "*.test.cjs" -not -path "./env/*" -not -path "./node_modules/*")
```

While `manage.py test` runs, uploads go to a temporary folder (not `media/`), and tests use a separate test database.

**Verified results (27 November 2026, final source snapshot):**

| Suite | Discovered | Passed | Failed | Skipped |
|---|---|---|---|---|
| Django | 987 | 986 | 0 | 1 — opt-in Safari browser test (`RUN_SAFARI_ACCEPTANCE=1`) |
| JavaScript | 55 | 55 | 0 | 0 |

Also verified: `check` (no issues), no pending migrations, SQLite integrity `ok`, 0 foreign-key violations, and the audit hash chain `ok`. Tests marked as needing Tesseract require it on `PATH`.

## 13. Backup and restoration

The verified backup tool copies the database with SQLite's online backup API (the live database is opened read-only), copies stored documents and a source snapshot, and writes a SHA-256 manifest. Secrets (`.env`), the virtualenv and older backups are never copied.

```bash
# create  → .backups/<label>-<timestamp>-IST/
python scripts/backup_verify.py create my-backup
# verify  → restores into a temporary folder and checks integrity, foreign keys,
#           table counts, every file hash, and recomputes the financial baseline
python scripts/backup_verify.py verify .backups/my-backup-<timestamp>-IST
```

The financial-baseline check in `verify` is specific to the author's SmartKart demonstration workspace. For other data, rely on the integrity, table-count and hash checks.

**Restore (stop the server first):**

```bash
cp db.sqlite3 db.sqlite3.before-restore                 # keep the current copy
cp .backups/<backup>/db.sqlite3 db.sqlite3
rsync -a .backups/<backup>/media/ media/
rsync -a .backups/<backup>/private_expense_files/ private_expense_files/
python manage.py check && python manage.py migrate --check
python manage.py verify_audit_security --database-path db.sqlite3
```

`.backups/`, databases and uploaded files are excluded from Git.

## 14. Project structure

```
smart-invoice-system/
├── accounts/        authentication, registration, business profile, session policy, demo commands
├── invoices/        invoices, extraction/OCR, PDFs, parties, payments, credit/debit notes, quotations
├── expenses/        expenses, receipts, recurring schedules, ITC review, expense reports
├── taxation/        GST/income working papers, GSTR-2B reconciliation
├── analytics/       dashboard and reporting services
├── audit/           audit log, hash chain, security monitor, session observations
├── config/          settings, URLs, middleware, environment loader, demo settings
├── templates/       server-rendered HTML
├── static/          CSS and JavaScript
├── sample_data/     fictional sample invoices, GSTR-2B files and expected values
├── scripts/         backup_verify.py (verified backup and isolated restore check)
├── PROJECT_DESCRIPTION.md
├── requirements.txt
├── .env.example
└── manage.py
```

## 15. Known limitations and future scope

**Limitations**

* OCR and extraction are rule-based and assistive. Accuracy depends on document quality, and human review is mandatory.
* GST/ITC and income-tax outputs are working papers, not filings, and need professional review. No GSTN/e-invoice/e-way-bill integration.
* SQLite and a single process: login rate limiting uses a per-process cache, and multi-worker deployment needs a shared cache and a server database.
* Pillow 12.3.0 has published advisories fixed only in Pillow 13, which needs Python ≥ 3.11. Image uploads are limited to PNG/JPEG decoders as a mitigation.
* When a session expires, unsaved form input is lost. There is no multi-factor authentication.
* Not deployed; HSTS subdomain/preload are opt-in. Browser and physical-print acceptance of the final session features is pending.

**Future scope**

* Upgrade to Python 3.11+ and Pillow 13; PostgreSQL; a shared cache; background workers for OCR.
* Trained layout models for extraction; multi-language OCR.
* GSTN / e-invoice (IRN) integration, bank-statement reconciliation, payment gateway links.
* Multi-user businesses with roles (accountant, auditor), MFA, and a REST API for mobile clients.

## 16. Documentation index

| Document | Content |
|---|---|
| `README.md` | Setup, configuration, testing, backup and limitations (this file) |
| `PROJECT_DESCRIPTION.md` | Project summary for submission and portfolio use |
| `sample_data/README.md` | Fictional sample invoices, GSTR-2B files and expected values |

The academic report, phase design notes and acceptance evidence are kept outside this repository.

---

Author: Ishita Batra · B.Tech CSE (2026–2027) · Banasthali Vidyapith
