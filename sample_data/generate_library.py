"""Create the fictional, parser-compatible sample library without DB imports."""

import csv
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


ROOT = Path(__file__).resolve().parent
CENT = Decimal("0.01")
README = """# Smart Invoice sample data library

All organizations, GSTINs, people, invoices, payment references and transactions in this folder are fictional. These files are for local/demo testing only; they are not tax documents, evidence of supply, or advice about GST/ITC eligibility. No data is imported into the application database by generating this library.

## Contents

- `invoices/purchase_invoices/`, `sales_invoices/`, `expense_invoices/`: parser-compatible text PDFs. Expense invoices use the existing invoice workflow because the project has no separate expense model.
- `invoices/duplicate_invoice_samples/`: a lower-case duplicate of purchase invoice `PINE-P-2026-001` for upload duplicate detection.
- `invoices/mismatch_invoice_samples/`: one invoice total mismatch and one line quantity-times-rate discrepancy. Keep these in review; they are intentionally not valid/verified examples.
- `gst_reconciliation/gstr2b_samples/`: one `.xlsx` and one JSON file for each return period. Each file is its own upload example; use either format per run.
- `party_ledger/`: fictional supplier/customer master reference CSV files.
- `payments/`: full/partial payment examples and a deliberately invalid overpayment example. Do not record the negative test payment.
- `outstanding_payments/`, `analytics_dashboard/`: source/reference CSVs and expected values to compare with application screens.
- `reports/expected_results/`: compact expected reconciliation classifications and per-invoice balances.
- `generate_library.py`: recreates the library using Python and `openpyxl`; it refuses to overwrite any existing file.

## Business and parties

Use one demo business profile named **Northstar Demo Trading Co**, GSTIN `27ZZZAA1234B1Z1`. Supplier/customer GSTINs and names are in `party_ledger/party_master_data/party_master.csv`. Keep a single authenticated demo user/business for the sample invoices and reconciliation runs. The app scopes records by the logged-in user/business; this library intentionally does not provide user accounts or database fixtures.

### Invoice file index

| Invoice number | Type | Party | Date | Notes |
|---|---|---|---|---|
| `PINE-P-2026-001` | Purchase | Pine & Pixel Supplies | 5 Jun | Intra-state 18%; exact June GSTR sample; fully paid |
| `PINE-P-2026-002` | Purchase | Pine & Pixel Supplies | 15 Jul | Intra-state 12%; within-tolerance July GSTR sample; partial |
| `PINE-P-2026-003-MISMATCH` | Purchase review sample | Pine & Pixel Supplies | 20 Aug | Intentional grand total error; do not verify |
| `PINE-P-2026-004` | Purchase | Pine & Pixel Supplies | 10 Sep | Intra-state 28%; unpaid; absent from September GSTR sample |
| `PINE-P-2026-005` | Purchase | Pine & Pixel Supplies | 5 Aug | Intra-state 18%; partial; August GSTR amount mismatch |
| `METRO-P-2026-001` | Purchase | Metro Office Mart | 25 Jul | Two line items at 18% and 12%, plus INR 250 other charges; paid |
| `RIV-2026-007` | Purchase | Harbor Components | 22 Aug | Inter-state 12%; partial; ambiguous normalized GSTR candidate |
| `RIV2026/007` | Purchase | Harbor Components | 22 Aug | Inter-state 12%; unpaid; second candidate for manual review |
| `SALE-AC-2026-001` | Sale | Acme Corner Stores | 10 Jun | Intra-state 18%; two receipts, fully paid |
| `SALE-AC-2026-002` | Sale | Acme Corner Stores | 12 Jul | Intra-state 5%; unpaid |
| `SALE-NV-2026-003` | Sale | Nova Home Retail | 8 Aug | Inter-state 12%; fully paid |
| `SALE-NV-2026-004` | Sale | Nova Home Retail | 5 Sep | Inter-state 18%; partial |
| `SALE-CD-2026-005` | Sale | Cedar Grove Market | 15 Oct | Inter-state 28%, plus INR 400 other charges; fully paid |
| `EXP-2026-001` | Expense | CityNet Utilities | 20 Jun | Intra-state 18% |
| `EXP-2026-002` | Expense | CityNet Utilities | 30 Aug | Intra-state 5% |
| `EXP-2026-003` | Expense | CityNet Utilities | 2 Oct | Intra-state 18% |
| `SALE-AC-2026-009-LINE-MISMATCH` | Sales review sample | Acme Corner Stores | 28 Sep | Intentional line quantity/rate discrepancy; do not verify |

There is also `PINE-P-2026-001-lowercase-duplicate.pdf`, whose document invoice number is lower-case `pine-p-2026-001` and whose supplier GSTIN is the same. The upload flow should identify it as a duplicate after the original is uploaded.

## Suggested setup and upload order

1. Create/sign in to a local demo account and set up the business profile above.
2. Upload the PDFs from `invoices/purchase_invoices/` and `invoices/sales_invoices/`. Upload the three `expense_invoices/` PDFs using the project's expense invoice type, if available.
3. Review each invoice's extracted details and line items. Add due dates from the PDF in the edit screen if the app does not extract due dates. Correct any parser differences, save, and verify only invoices with no validation issues. Exclude the intentional mismatch PDFs from verification.
4. Upload payment examples against their matching verified invoices from `payments/payment_records/payment_records.csv`. Record each row once; references beginning `SAMP-` identify sample actions. The duplicate detection sample should be tried only after its matching purchase invoice is stored.
5. Upload one GSTR file per reconciliation run, selecting the matching return period: June through September 2026. `.xlsx` and `.json` siblings contain the same rows, so don't upload both unless deliberately testing separate runs.
6. For August, compare the `RIV-2026-007` GSTR row with the two Harbor invoice candidates. The two purchase numbers `RIV-2026-007` and `RIV2026/007` normalize to the same candidate key in reconciliation; the application should require manual review. Use the confirmation screen to choose explicitly. `PINE-P-2026-005-ALT` is intentionally unmatched due to its different normalized number.
7. Review `reports/expected_results/` and the analytics/outstanding CSV references. Results in the app depend on successfully creating and verifying the corresponding records.

## Expected invoice/payment figures

Assuming all listed valid sales/purchases/expenses are uploaded and verified, and the payment CSV rows are recorded exactly once:

- Sales invoice totals: **INR 82,460.00**.
- Purchase totals (excluding the intentional review-only mismatch invoice): **INR 65,330.00**.
- Expense invoice totals: **INR 12,210.00**.
- Payment receipts: **INR 68,060.00**; payments made: **INR 29,090.00**; net cash flow: **INR 38,970.00**.
- Receivables: **INR 14,400.00**; payables: **INR 36,240.00**.

Expected party-level ledger balances after verification and the payment rows: Acme Corner Stores receivable INR 10,500.00; Nova Home Retail receivable INR 3,900.00; Cedar Grove Market INR 0.00. Pine & Pixel Supplies payable INR 27,720.00; Metro Office Mart INR 0.00; Harbor Components payable INR 8,520.00. CityNet expense documents appear in expense/analytics reporting; the current party ledger covers verified sales and purchase invoices only. Payment history: Acme `SALE-AC-2026-001` receives INR 10,000 UPI then INR 19,500 bank transfer; Nova `SALE-NV-2026-003` receives INR 20,160 bank transfer; Nova `SALE-NV-2026-004` receives INR 2,000 cheque; Cedar receives INR 16,400 bank transfer. Supplier payments are listed in `payments/payment_records/payment_records.csv` (bank transfer, UPI, cheque and cash).

These totals assume app analytics include verified invoices and only actual payment records in cash-flow values. Review-status and cancelled invoices should not contribute. Reconciliation is separate from payment and does not establish legal ITC eligibility.

## Expected reconciliation cases

- `2026-06`: exact match for `PINE-P-2026-001`.
- `2026-07`: within INR 1.00 tolerance for `PINE-P-2026-002`; duplicate source row is deduplicated within the run.
- `2026-08`: amount mismatch for `PINE-P-2026-005`; unmatched alternate invoice number; manual review for the two normalized Harbor candidates.
- `2026-09`: GSTR invoice `HARBO-2026-009` has no book candidate; verified purchase `PINE-P-2026-004` is missing from the GSTR sample.

The July differences are taxable `+0.50`, CGST `+0.30`, SGST `+0.20`. The August PINE row deliberately has CGST/SGST 552.00 rather than the book amounts 540.00 each. The Harbor candidate comparison is intentionally ambiguous; do not treat a user-confirmed manual match as an exact match unless its amounts and date also agree.

Expected supported-invoice GST totals for the complete date range are output GST INR 11,560.00 and input GST INR 9,080.00, calculated from verified sample sales and purchases. If one file is uploaded for each return period, imported GSTR-2B total tax is INR 8,124.50 across the four runs. These are calculated invoice-book and uploaded-data figures. Neither represents a claim about officially reported tax or determines ITC eligibility. The per-invoice source values and dates are in the PDF files, and GSTR data files are grouped by return period in `gst_reconciliation/gstr2b_samples/`.

## Parser and file notes

The invoice PDFs are text PDFs laid out for this project's current `invoices.extraction.extract_invoice_data` patterns: `TAX INVOICE`, invoice/date labels, supplier and buyer GSTINs, `Bill To`, supported line-item columns, and labeled totals. This helps exercise the current parser but does not guarantee every business invoice layout will parse. GSTR files use the current reconciliation parser's accepted columns and common `data.docdata.b2b` JSON shape. Monetary values are two-decimal strings in reference CSVs; Excel stores numeric cells and dates as Excel date values.

The existing invoice upload flow accepts PDFs. CSV files here are supporting reference data, not bulk-import formats for invoices, parties, payments, or analytics. Enter those values through the relevant application screens. Do not import `negative_overpayment_test.csv`; its amount exceeds the remaining balance by INR 0.01 and is provided only to verify rejection.

## Local validation

Run `python manage.py check` from the project root. To verify file parsing without database writes, the project parser functions can be called on these PDFs and GSTR `.xlsx`/`.json` files. `python sample_data/generate_library.py` is safe only when the output files do not already exist; it fails instead of overwriting them.
"""

BUSINESS = {"name": "Northstar Demo Trading Co", "gstin": "27ZZZAA1234B1Z1"}
PARTIES = {
    "Pine & Pixel Supplies": {"gstin": "27PINEA1234B1Z2", "type": "supplier", "state": "Maharashtra"},
    "Harbor Components": {"gstin": "29HARBO1234C1Z3", "type": "supplier", "state": "Karnataka"},
    "Metro Office Mart": {"gstin": "27METRO1234D1Z4", "type": "supplier", "state": "Maharashtra"},
    "CityNet Utilities": {"gstin": "27CITYN1234H1Z8", "type": "supplier", "state": "Maharashtra"},
    "Acme Corner Stores": {"gstin": "27ACMEX1234F1Z6", "type": "customer", "state": "Maharashtra"},
    "Nova Home Retail": {"gstin": "29NOVAA1234E1Z5", "type": "customer", "state": "Karnataka"},
    "Cedar Grove Market": {"gstin": "24CEDAR1234G1Z7", "type": "customer", "state": "Gujarat"},
}


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def line(description, hsn, quantity, rate, tax_rate):
    quantity, rate, tax_rate = Decimal(str(quantity)), money(rate), Decimal(str(tax_rate))
    taxable = money(quantity * rate)
    tax = money(taxable * tax_rate / Decimal("100"))
    return {
        "description": description, "hsn": str(hsn), "quantity": quantity,
        "rate": rate, "taxable": taxable, "tax_rate": tax_rate, "tax": tax,
    }


def invoice(number, date, invoice_type, supplier, buyer, lines, due, interstate=False, charges="0.00", total_override=None, folder=None):
    lines = [dict(item) for item in lines]
    taxable = sum((item["taxable"] for item in lines), Decimal("0.00"))
    tax = sum((item["tax"] for item in lines), Decimal("0.00"))
    igst = tax if interstate else Decimal("0.00")
    cgst = Decimal("0.00") if interstate else money(tax / Decimal("2"))
    sgst = Decimal("0.00") if interstate else tax - cgst
    charges = money(charges)
    calculated_total = taxable + tax + charges
    return {
        "number": number, "date": date, "due": due, "type": invoice_type,
        "supplier": supplier, "buyer": buyer, "lines": lines,
        "taxable": taxable, "cgst": cgst, "sgst": sgst, "igst": igst,
        "charges": charges,
        "total": money(total_override) if total_override is not None else calculated_total,
        "calculated_total": calculated_total, "folder": folder,
    }


def build_invoices():
    purchase, sales, expenses, mismatch = [], [], [], []
    purchase.append(invoice("PINE-P-2026-001", "05/06/2026", "purchase", "Pine & Pixel Supplies", BUSINESS["name"], [line("Network storage units", 8471, 10, 1000, 18)], "30/06/2026"))
    purchase.append(invoice("PINE-P-2026-002", "15/07/2026", "purchase", "Pine & Pixel Supplies", BUSINESS["name"], [line("Business software licences", 8523, 20, 1000, 12)], "31/07/2026"))
    bad_total = invoice("PINE-P-2026-003-MISMATCH", "20/08/2026", "purchase", "Pine & Pixel Supplies", BUSINESS["name"], [line("LED work lights", 9405, 30, 500, 5)], "20/09/2026", total_override="15950.00")
    bad_total["folder"] = "invoice_total_mismatch"
    mismatch.append(bad_total)
    purchase.append(invoice("PINE-P-2026-004", "10/09/2026", "purchase", "Pine & Pixel Supplies", BUSINESS["name"], [line("Industrial control panels", 8537, 10, 800, 28)], "10/10/2026"))
    purchase.append(invoice("PINE-P-2026-005", "05/08/2026", "purchase", "Pine & Pixel Supplies", BUSINESS["name"], [line("Safety equipment set", 9020, 12, 500, 18)], "30/08/2026"))
    purchase.append(invoice("METRO-P-2026-001", "25/07/2026", "purchase", "Metro Office Mart", BUSINESS["name"], [
        line("Ergonomic office chairs", 9401, 2, 1000, 18),
        line("Desk organisers", 3926, 10, 150, 12),
    ], "10/08/2026", charges="250.00"))
    purchase.append(invoice("RIV-2026-007", "22/08/2026", "purchase", "Harbor Components", BUSINESS["name"], [line("Control relay modules", 8536, 20, 200, 12)], "22/09/2026", interstate=True))
    purchase.append(invoice("RIV2026/007", "22/08/2026", "purchase", "Harbor Components", BUSINESS["name"], [line("Switching relay modules", 8536, 15, 300, 12)], "22/09/2026", interstate=True))

    sales.append(invoice("SALE-AC-2026-001", "10/06/2026", "sales", BUSINESS["name"], "Acme Corner Stores", [line("Retail router bundle", 8517, 25, 1000, 18)], "25/06/2026"))
    sales.append(invoice("SALE-AC-2026-002", "12/07/2026", "sales", BUSINESS["name"], "Acme Corner Stores", [line("Wireless keyboard pack", 8471, 20, 500, 5)], "31/07/2026"))
    sales.append(invoice("SALE-NV-2026-003", "08/08/2026", "sales", BUSINESS["name"], "Nova Home Retail", [line("Smart display units", 8528, 12, 1500, 12)], "23/08/2026", interstate=True))
    sales.append(invoice("SALE-NV-2026-004", "05/09/2026", "sales", BUSINESS["name"], "Nova Home Retail", [line("Compact barcode readers", 8471, 10, 500, 18)], "20/09/2026", interstate=True))
    sales.append(invoice("SALE-CD-2026-005", "15/10/2026", "sales", BUSINESS["name"], "Cedar Grove Market", [line("Power management units", 8504, 25, 500, 28)], "30/10/2026", interstate=True, charges="400.00"))

    expenses.append(invoice("EXP-2026-001", "20/06/2026", "expense", "CityNet Utilities", BUSINESS["name"], [line("Office internet service", 9984, 1, 5000, 18)], "05/07/2026"))
    expenses.append(invoice("EXP-2026-002", "30/08/2026", "expense", "CityNet Utilities", BUSINESS["name"], [line("Cloud backup subscription", 9983, 1, 3200, 5)], "14/09/2026"))
    expenses.append(invoice("EXP-2026-003", "02/10/2026", "expense", "CityNet Utilities", BUSINESS["name"], [line("Regional data service", 9984, 1, 2500, 18)], "17/10/2026"))

    line_bad = invoice("SALE-AC-2026-009-LINE-MISMATCH", "28/09/2026", "sales", BUSINESS["name"], "Acme Corner Stores", [line("Sample taxable amount discrepancy", 8471, 2, 100, 18)], "13/10/2026")
    line_bad["lines"][0]["taxable"] = Decimal("250.00")
    line_bad["lines"][0]["tax"] = Decimal("45.00")
    line_bad["taxable"] = Decimal("250.00")
    line_bad["cgst"] = Decimal("22.50")
    line_bad["sgst"] = Decimal("22.50")
    line_bad["total"] = Decimal("295.00")
    line_bad["calculated_total"] = Decimal("295.00")
    line_bad["folder"] = "line_item_mismatch"
    mismatch.append(line_bad)

    return purchase, sales, expenses, mismatch


def pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def create_pdf(path, item):
    supplier_gstin = BUSINESS["gstin"] if item["type"] == "sales" else PARTIES[item["supplier"]]["gstin"]
    buyer_gstin = PARTIES[item["buyer"]]["gstin"] if item["type"] == "sales" else BUSINESS["gstin"]
    rows = [
        ("TAX INVOICE", "F2", 16),
        (item["supplier"], "F2", 13),
        ("SAMPLE / TESTING DOCUMENT - FICTIONAL TRANSACTION", "F2", 9),
        (f"Invoice Number {item['number']}", "F1", 10),
        (f"Invoice Date {item['date']}", "F1", 10),
        (f"Due Date {item['due']}", "F1", 10),
        (f"Supplier GSTIN {supplier_gstin}", "F1", 10),
        ("Bill To", "F2", 10),
        (item["buyer"], "F1", 10),
        (f"Buyer GSTIN {buyer_gstin}", "F1", 10),
        ("No  Description / HSN  Qty  Rate  Taxable  Tax Rate", "F2", 9),
    ]
    for index, row in enumerate(item["lines"], 1):
        rows.append((
            f"{index} {row['description']} {row['hsn']} {row['quantity']:.2f} "
            f"{row['rate']:.2f} {row['taxable']:.2f} {row['tax_rate']:.2f}%",
            "F1", 9,
        ))
    rows.extend([
        (f"Taxable Amount {item['taxable']:.2f}", "F1", 10),
        (f"CGST {item['cgst']:.2f}", "F1", 10),
        (f"SGST {item['sgst']:.2f}", "F1", 10),
        (f"IGST {item['igst']:.2f}", "F1", 10),
        (f"Other Charges {item['charges']:.2f}", "F1", 10),
        (f"Grand Total {item['total']:.2f}", "F2", 12),
        ("Prepared for Smart Invoice sample testing only.", "F1", 8),
    ])
    stream_lines = []
    y = 755
    for text, font, size in rows:
        stream_lines.append(f"BT /{font} {size} Tf 48 {y} Td ({pdf_escape(text)}) Tj ET")
        y -= 23 if size >= 10 else 19
    stream = ("\n".join(stream_lines) + "\n").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
    ]
    content = bytearray(b"%PDF-1.4\n%Sample invoice library\n")
    offsets = [0]
    for object_id, body in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{object_id} 0 obj\n".encode("ascii"))
        content.extend(body)
        content.extend(b"\nendobj\n")
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    write_bytes(path, bytes(content))


def write_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing sample file: {path}")
    path.write_bytes(content)


def write_text(path, content):
    write_bytes(path, content.encode("utf-8"))


def create_csv(path, headers, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing sample file: {path}")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(records)


def create_reconciliation_workbook(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing sample file: {path}")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "B2B Invoices"
    headers = ["Supplier GSTIN", "Invoice Number", "Invoice Date", "Taxable Value", "IGST", "CGST", "SGST", "Total Tax"]
    sheet.append(headers)
    for record in records:
        sheet.append([
            record["supplier_gstin"], record["invoice_number"],
            datetime.strptime(record["invoice_date"], "%d-%m-%Y").date(),
            float(record["taxable_value"]), float(record["igst_amount"]),
            float(record["cgst_amount"]), float(record["sgst_amount"]),
            float(record["total_tax"]),
        ])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="26382F")
        cell.alignment = Alignment(horizontal="center")
    widths = [20, 28, 16, 18, 14, 14, 14, 16]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for row in sheet.iter_rows(min_row=2, min_col=4, max_col=8):
        for cell in row:
            cell.number_format = "0.00"
    workbook.save(path)


def create_reconciliation_json(path, records):
    by_supplier = {}
    for record in records:
        supplier = by_supplier.setdefault(record["supplier_gstin"], {"ctin": record["supplier_gstin"], "inv": []})
        supplier["inv"].append({
            "inum": record["invoice_number"],
            "dt": record["invoice_date"],
            "items": [{
                "num": 1,
                "itm_det": {
                    "txval": record["taxable_value"],
                    "iamt": record["igst_amount"],
                    "camt": record["cgst_amount"],
                    "samt": record["sgst_amount"],
                },
            }],
        })
    payload = {"data": {"docdata": {"b2b": list(by_supplier.values())}}}
    write_text(path, json.dumps(payload, indent=2) + "\n")


def recon_record(supplier, invoice_number, date, taxable, igst, cgst, sgst):
    return {
        "supplier_gstin": PARTIES[supplier]["gstin"],
        "invoice_number": invoice_number,
        "invoice_date": datetime.strptime(date, "%d/%m/%Y").strftime("%d-%m-%Y"),
        "taxable_value": str(money(taxable)),
        "igst_amount": str(money(igst)),
        "cgst_amount": str(money(cgst)),
        "sgst_amount": str(money(sgst)),
        "total_tax": str(money(Decimal(str(igst)) + Decimal(str(cgst)) + Decimal(str(sgst)))),
    }


def make_directories():
    directories = [
        "invoices/purchase_invoices", "invoices/sales_invoices", "invoices/expense_invoices",
        "invoices/duplicate_invoice_samples", "invoices/mismatch_invoice_samples",
        "gst_reconciliation/gstr2b_samples", "gst_reconciliation/matching_samples",
        "gst_reconciliation/mismatch_samples", "party_ledger/supplier_samples",
        "party_ledger/customer_samples", "party_ledger/party_master_data",
        "payments/partial_payment_samples", "payments/full_payment_samples",
        "payments/payment_records", "outstanding_payments/receivables_samples",
        "outstanding_payments/payables_samples", "analytics_dashboard/sales_data",
        "analytics_dashboard/expense_data", "analytics_dashboard/payment_data",
        "analytics_dashboard/dashboard_sample_data", "reports/sample_exports",
        "reports/expected_results",
    ]
    for directory in directories:
        (ROOT / directory).mkdir(parents=True, exist_ok=True)


def create_all():
    make_directories()
    purchase, sales, expenses, mismatch = build_invoices()
    for item in purchase:
        safe_number = item["number"].replace("/", "-")
        create_pdf(ROOT / "invoices/purchase_invoices" / f"{safe_number}.pdf", item)
    for item in sales:
        create_pdf(ROOT / "invoices/sales_invoices" / f"{item['number']}.pdf", item)
    for item in expenses:
        create_pdf(ROOT / "invoices/expense_invoices" / f"{item['number']}.pdf", item)
    for item in mismatch:
        create_pdf(ROOT / "invoices/mismatch_invoice_samples" / f"{item['number']}.pdf", item)
    duplicate = dict(purchase[0])
    duplicate["number"] = "pine-p-2026-001"
    create_pdf(ROOT / "invoices/duplicate_invoice_samples" / "PINE-P-2026-001-lowercase-duplicate.pdf", duplicate)

    june = [recon_record("Pine & Pixel Supplies", "PINE-P-2026-001", "05/06/2026", "10000", "0", "900", "900")]
    july_tolerance = recon_record("Pine & Pixel Supplies", "PINE-P-2026-002", "15/07/2026", "20000.50", "0", "1200.30", "1200.20")
    july = [july_tolerance, dict(july_tolerance)]  # The parser should import this normalized key once.
    august = [
        recon_record("Pine & Pixel Supplies", "PINE-P-2026-005", "05/08/2026", "6000", "0", "552", "552"),
        recon_record("Pine & Pixel Supplies", "PINE-P-2026-005-ALT", "05/08/2026", "6000", "0", "540", "540"),
        recon_record("Harbor Components", "RIV-2026-007", "22/08/2026", "4000", "480", "0", "0"),
    ]
    september = [recon_record("Harbor Components", "HARBO-2026-009", "18/09/2026", "7000", "0", "630", "630")]
    reconciliation_sets = {
        "gstr2b_2026-06_exact": june,
        "gstr2b_2026-07_tolerance_duplicate": july,
        "gstr2b_2026-08_mismatch_manual_review": august,
        "gstr2b_2026-09_missing_in_books": september,
    }
    for basename, records in reconciliation_sets.items():
        create_reconciliation_workbook(ROOT / "gst_reconciliation/gstr2b_samples" / f"{basename}.xlsx", records)
        create_reconciliation_json(ROOT / "gst_reconciliation/gstr2b_samples" / f"{basename}.json", records)

    supplier_rows = []
    customer_rows = []
    master_rows = []
    for name, details in PARTIES.items():
        row = {"party_name": name, "party_type": details["type"], "gstin": details["gstin"], "state": details["state"], "fictional": "yes"}
        master_rows.append(row)
        (supplier_rows if details["type"] == "supplier" else customer_rows).append(row)
    party_headers = ["party_name", "party_type", "gstin", "state", "fictional"]
    create_csv(ROOT / "party_ledger/supplier_samples/suppliers.csv", party_headers, supplier_rows)
    create_csv(ROOT / "party_ledger/customer_samples/customers.csv", party_headers, customer_rows)
    create_csv(ROOT / "party_ledger/party_master_data/party_master.csv", party_headers, master_rows)

    payments = [
        ("SALE-AC-2026-001", "2026-06-20", "10000.00", "UPI", "SAMP-RCPT-001A", "Partial receipt"),
        ("SALE-AC-2026-001", "2026-06-25", "19500.00", "Bank Transfer", "SAMP-RCPT-001B", "Final receipt"),
        ("SALE-NV-2026-003", "2026-08-18", "20160.00", "Bank Transfer", "SAMP-RCPT-003", "Full receipt"),
        ("SALE-NV-2026-004", "2026-09-10", "2000.00", "Cheque", "SAMP-RCPT-004A", "Partial receipt"),
        ("SALE-CD-2026-005", "2026-10-25", "16400.00", "Bank Transfer", "SAMP-RCPT-005", "Full receipt"),
        ("PINE-P-2026-001", "2026-06-20", "11800.00", "Bank Transfer", "SAMP-PAY-001", "Full supplier payment"),
        ("PINE-P-2026-002", "2026-07-20", "10000.00", "UPI", "SAMP-PAY-002A", "Partial supplier payment"),
        ("METRO-P-2026-001", "2026-08-01", "4290.00", "Cheque", "SAMP-PAY-METRO", "Full supplier payment"),
        ("PINE-P-2026-005", "2026-08-10", "2000.00", "Cash", "SAMP-PAY-005A", "Partial supplier payment"),
        ("RIV-2026-007", "2026-08-30", "1000.00", "Cash", "SAMP-PAY-RIV-A", "Partial supplier payment"),
    ]
    payment_headers = ["invoice_number", "payment_date", "amount", "payment_mode", "reference_id", "notes"]
    payment_rows = [dict(zip(payment_headers, values)) for values in payments]
    create_csv(ROOT / "payments/payment_records/payment_records.csv", payment_headers, payment_rows)
    create_csv(ROOT / "analytics_dashboard/payment_data/payment_records.csv", payment_headers, payment_rows)
    create_csv(ROOT / "payments/partial_payment_samples/partial_payments.csv", payment_headers, [row for row in payment_rows if "Partial" in row["notes"]])
    create_csv(ROOT / "payments/full_payment_samples/full_payments.csv", payment_headers, [row for row in payment_rows if "Full" in row["notes"] or "Final" in row["notes"]])
    invalid_overpayment = [{"invoice_number": "PINE-P-2026-002", "payment_date": "2026-10-01", "amount": "12400.01", "payment_mode": "Bank Transfer", "reference_id": "NEGATIVE-OVERPAY-001", "notes": "Negative test only: exceeds remaining balance by INR 0.01; do not record."}]
    create_csv(ROOT / "payments/partial_payment_samples/negative_overpayment_test.csv", payment_headers, invalid_overpayment)

    sales_data = [{"invoice_number": item["number"], "invoice_date": item["date"], "buyer": item["buyer"], "taxable": str(item["taxable"]), "invoice_total": str(item["total"])} for item in sales]
    expense_data = [{"invoice_number": item["number"], "invoice_date": item["date"], "supplier": item["supplier"], "invoice_total": str(item["total"])} for item in expenses]
    create_csv(ROOT / "analytics_dashboard/sales_data/sales_invoice_reference.csv", ["invoice_number", "invoice_date", "buyer", "taxable", "invoice_total"], sales_data)
    create_csv(ROOT / "analytics_dashboard/expense_data/expense_invoice_reference.csv", ["invoice_number", "invoice_date", "supplier", "invoice_total"], expense_data)

    valid_purchase = [item for item in purchase]
    valid_sales = sales
    confirmed_purchases = [item for item in valid_purchase if item["number"] != "PINE-P-2026-003-MISMATCH"]
    sales_total = sum((item["total"] for item in valid_sales), Decimal("0.00"))
    purchases_total = sum((item["total"] for item in confirmed_purchases), Decimal("0.00"))
    expenses_total = sum((item["total"] for item in expenses), Decimal("0.00"))
    payments_received = sum((Decimal(row["amount"]) for row in payment_rows if row["invoice_number"].startswith("SALE-")), Decimal("0.00"))
    payments_made = sum((Decimal(row["amount"]) for row in payment_rows if not row["invoice_number"].startswith("SALE-")), Decimal("0.00"))
    receivables = sales_total - payments_received
    payables = purchases_total - payments_made
    kpis = [
        {"metric": "Total Sales", "expected_inr": str(sales_total)},
        {"metric": "Total Purchases (excludes review mismatch invoice)", "expected_inr": str(purchases_total)},
        {"metric": "Total Expenses", "expected_inr": str(expenses_total)},
        {"metric": "Payments Received", "expected_inr": str(payments_received)},
        {"metric": "Payments Made", "expected_inr": str(payments_made)},
        {"metric": "Accounts Receivable", "expected_inr": str(receivables)},
        {"metric": "Accounts Payable", "expected_inr": str(payables)},
        {"metric": "Net Cash Flow", "expected_inr": str(payments_received - payments_made)},
    ]
    create_csv(ROOT / "analytics_dashboard/dashboard_sample_data/expected_kpis.csv", ["metric", "expected_inr"], kpis)
    invoice_balances = [
        ("SALE-AC-2026-001", "sales", "29500.00", "29500.00", "0.00", "Paid"),
        ("SALE-AC-2026-002", "sales", "10500.00", "0.00", "10500.00", "Unpaid"),
        ("SALE-NV-2026-003", "sales", "20160.00", "20160.00", "0.00", "Paid"),
        ("SALE-NV-2026-004", "sales", "5900.00", "2000.00", "3900.00", "Partially paid"),
        ("SALE-CD-2026-005", "sales", "16400.00", "16400.00", "0.00", "Paid"),
        ("PINE-P-2026-001", "purchase", "11800.00", "11800.00", "0.00", "Paid"),
        ("PINE-P-2026-002", "purchase", "22400.00", "10000.00", "12400.00", "Partially paid"),
        ("PINE-P-2026-004", "purchase", "10240.00", "0.00", "10240.00", "Unpaid"),
        ("PINE-P-2026-005", "purchase", "7080.00", "2000.00", "5080.00", "Partially paid"),
        ("METRO-P-2026-001", "purchase", "4290.00", "4290.00", "0.00", "Paid"),
        ("RIV-2026-007", "purchase", "4480.00", "1000.00", "3480.00", "Partially paid / manual review candidate"),
        ("RIV2026/007", "purchase", "5040.00", "0.00", "5040.00", "Unpaid / manual review candidate"),
    ]
    balance_rows = [dict(zip(["invoice_number", "invoice_type", "invoice_total", "payments", "outstanding", "expected_status"], row)) for row in invoice_balances]
    create_csv(ROOT / "reports/expected_results/invoice_payment_balances.csv", list(balance_rows[0]), balance_rows)
    create_csv(ROOT / "outstanding_payments/receivables_samples/expected_receivables.csv", list(balance_rows[0]), [row for row in balance_rows if row["invoice_type"] == "sales"])
    create_csv(ROOT / "outstanding_payments/payables_samples/expected_payables.csv", list(balance_rows[0]), [row for row in balance_rows if row["invoice_type"] == "purchase"])

    recon_expectations = [
        {"return_period": "2026-06", "invoice_number": "PINE-P-2026-001", "expected_result": "Exact Match"},
        {"return_period": "2026-07", "invoice_number": "PINE-P-2026-002", "expected_result": "Match within tolerance; repeated row is deduplicated"},
        {"return_period": "2026-08", "invoice_number": "PINE-P-2026-005", "expected_result": "Mismatch (IGST/CGST/SGST data differs by more than INR 1.00)"},
        {"return_period": "2026-08", "invoice_number": "PINE-P-2026-005-ALT", "expected_result": "Unmatched; normalized invoice number differs from books"},
        {"return_period": "2026-08", "invoice_number": "RIV-2026-007", "expected_result": "Manual review required; two book invoices normalize to the same number"},
        {"return_period": "2026-09", "invoice_number": "HARBO-2026-009", "expected_result": "Unmatched; missing in books"},
        {"return_period": "2026-09", "invoice_number": "PINE-P-2026-004", "expected_result": "Purchase invoice is missing in GSTR-2B"},
    ]
    create_csv(ROOT / "reports/expected_results/reconciliation_expectations.csv", ["return_period", "invoice_number", "expected_result"], recon_expectations)
    write_text(ROOT / "gst_reconciliation/matching_samples/README.txt", "Use gstr2b_2026-06_exact and gstr2b_2026-07_tolerance_duplicate from ../gstr2b_samples after uploading the corresponding purchase invoices.\n")
    write_text(ROOT / "gst_reconciliation/mismatch_samples/README.txt", "Use gstr2b_2026-08_mismatch_manual_review and gstr2b_2026-09_missing_in_books from ../gstr2b_samples to exercise mismatch, manual review, unmatched, and missing-in-GSTR-2B cases.\n")
    write_text(ROOT / "reports/sample_exports/README.txt", "Party ledger exports and reconciliation exports are generated by the application after sample records are entered. Expected values are provided in ../expected_results/.\n")
    write_text(ROOT / "README.md", README)
    print(f"Sample library generated under {ROOT}")


if __name__ == "__main__":
    create_all()
