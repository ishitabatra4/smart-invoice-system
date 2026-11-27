# Smart Invoice sample data library

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


## New feature acceptance pack

See [feature_acceptance/README.md](feature_acceptance/README.md) and
[feature_matrix.csv](feature_acceptance/feature_matrix.csv) for the newer manual GST,
OCR, quotations/proformas, adjustment notes, recurring expense/ITC, tax, security and
UI scenarios. These are a **separate isolated dataset**; the original library and
its golden totals above remain unchanged. No database importer is provided.

Validate without database access:
```bash
env/bin/python sample_data/generate_feature_library.py --verify
env/bin/python -m unittest discover -s sample_data -p test_feature_library.py
```
See [feature acceptance validation](feature_acceptance_validation.md) for checks
and the observed OCR date-recognition limitation.
