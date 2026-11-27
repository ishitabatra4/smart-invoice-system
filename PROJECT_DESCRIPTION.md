# Smart Invoice System — Project Description

**Official title:** Invoice Management System
**Type:** B.Tech (Computer Science and Engineering) final-year project, Banasthali Vidyapith, 2026–2027
**Author:** Ishita Batra
**Stack:** Python 3.10 · Django 5.2 · SQLite · Tesseract OCR · ReportLab · openpyxl · JavaScript
**Repository:** https://github.com/ishitabatra4/smart-invoice-system

---

## One-line summary

A Django web application that turns uploaded invoices into verified accounting records through local OCR and mandatory human review. It also manages GST-correct invoicing, payments, expenses, credit/debit notes, tax working papers and GSTR-2B reconciliation, and keeps a tamper-evident audit trail.

## The problem

Small Indian businesses usually manage invoices with spreadsheets, paper files or generic accounting tools. Typing purchase and expense bills in by hand is slow and error-prone. A single wrong GSTIN, tax rate or total quietly distorts input-tax credit, outstanding balances and returns. Fully automatic "AI extraction" tools often hide uncertainty and post unreviewed numbers. Small businesses also rarely get a clear record of who changed what.

## What the system does

1. **Captures invoices** by manual entry or by uploading PDF/PNG/JPEG files. It reads embedded PDF text where available and falls back to local Tesseract OCR.
2. **Extracts and checks** invoice number, dates, GSTINs, parties, line items, taxable value and CGST/SGST/IGST. It cross-checks totals and flags duplicates, invalid GSTINs, date conflicts and unreconciled amounts.
3. **Requires a person to review and verify** each extracted invoice before it affects any total. Warnings are grouped and labelled *Critical* or *Needs review*.
4. **Manages the invoice lifecycle**: finalized A4 PDFs, email delivery, payments (full/partial), outstanding and overdue tracking, and credit/debit notes linked to invoices.
5. **Handles commercial documents**: quotations and proforma invoices that convert to invoices.
6. **Manages expenses**: categories, private receipts, recurring schedules, ITC eligibility review and CSV/Excel/PDF reports.
7. **Prepares tax working papers**: GST and income summaries for a period, plus reconciliation of purchase records against GSTR-2B files.
8. **Reports** sales, purchases, expenses, GST components, receivables and payables on a dashboard with date ranges.
9. **Secures and audits** everything: email-verified accounts, server-side sessions with idle and absolute timeouts, strict per-business data isolation, and a hash-chained audit log with security alerts and session revocation.

## Key features

| Area | Highlights |
|---|---|
| Invoice management | Sales/purchase/expense invoices, line-level discounts, intra/inter-state GST, one-page A4 PDFs for short invoices, batch upload and Excel export |
| OCR extraction | pdfplumber + PDFium + Tesseract, field confidence and evidence, duplicate detection, line-item reconciliation, mandatory review |
| Parties | Customers, suppliers and dual-role parties; GSTIN validation; party ledger exports |
| Adjustments | Issued/received credit and debit notes reflected in balances and GST |
| Finance | Payments, outstanding dashboard, expenses, recurring schedules, ITC review |
| Taxation | GST/income working papers with review status and exports; GSTR-2B reconciliation with automatic and manual matching |
| Reporting | Analytics dashboard, expense reports, CSV/Excel/PDF exports with formula-injection protection |
| Security | Session security hardening, CSRF, rate limiting, generic login errors, no-store caching, owner-only file downloads |
| Audit | Before/after audit events, SHA-256 hash chain, deterministic security alerts, verification command |

## What makes it different

* **Human-in-the-loop by design.** Extraction never posts numbers on its own. Unverified values are clearly marked and kept out of financial totals, and critical warnings cannot be hidden.
* **Exact money.** All calculations use Python `Decimal` with explicit GST rounding. Financial outputs were reconciled to the paisa against an independently calculated baseline.
* **Evidence-first extraction.** Each extracted field keeps the text it came from, so a reviewer can see *why* a value was chosen.
* **Tamper-evident audit.** Security-relevant events are sealed into a SHA-256 hash chain and can be checked with a management command, with alert rules for suspicious activity.
* **Privacy-conscious local processing.** OCR runs locally (no cloud document AI). Uploaded documents are never public, and session keys are stored only as digests.
* **Thoroughly tested.** 987 Django tests and 55 JavaScript tests, with documented acceptance evidence, verified backups and restoration checks.

## Architecture (summary)

A server-rendered Django application with six apps: `accounts`, `invoices`, `expenses`, `taxation`, `analytics` and `audit`. It has 48 database tables. A layered middleware chain handles security headers, sessions, CSRF, authentication, revoked-session rejection, cache control and session expiry. Domain services handle GST calculation, the OCR pipeline, ledgers, reporting and audit sealing. Every view is limited to the signed-in user's own business.

## Results

| Measure | Result |
|---|---|
| Automated tests (final snapshot) | Django 987 (986 passed, 1 opt-in browser test skipped); JavaScript 55/55 |
| Demonstration workspace | 60 invoices (20 sales, 20 purchase, 20 expense), 19 parties, 10 quotations, 8 proformas, 12 credit/debit notes, 28 payments, 8 recurring schedules, 6 GSTR-2B months |
| Financial accuracy | Dashboard totals for Jun–Nov 2026 match the independent baseline exactly (sales ₹87,663.50; purchases ₹68,861.50; expenses ₹32,069.73; receivables ₹17,292.00; payables ₹64,007.53) |
| Data integrity | SQLite integrity ok, 0 foreign-key violations, audit chain verified, original documents unchanged by hash |
| Performance example | Expense report reduced from 150 to 8 database queries with identical output |

## Skills demonstrated

Full-stack web development with Django · relational database design and migrations · document processing and OCR · financial and GST domain modelling with exact arithmetic · application security (session management, CSRF, access control, audit logging) · automated testing at scale · query optimisation · technical documentation and verified backup/restore procedures.

## Resume / portfolio summary

> **Smart Invoice System** (Django, Python, SQLite, Tesseract OCR). Built a GST-aware invoice and expense management platform with OCR-assisted extraction and mandatory human verification, payments and credit/debit notes, GST working papers and GSTR-2B reconciliation. Implemented server-side session security (idle/absolute expiry, session revocation, CSRF, rate limiting) and a SHA-256 hash-chained audit log. Covered by 987 Django and 55 JavaScript tests. Cut a reporting endpoint from 150 to 8 queries without changing any output.

## Scope and limitations

This is an academic project and has not been deployed to production. Tax outputs are preparation aids, not filings. OCR accuracy depends on document quality, and review is always required. The full list is in `README.md` §15.
