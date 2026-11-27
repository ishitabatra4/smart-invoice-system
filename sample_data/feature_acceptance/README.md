# Feature acceptance samples — fictional and isolated only

This extends the older sample library without changing its files or expected totals.
Use a NEW isolated demo database/media directory and demo business. Nothing here is
an instruction or program to import records into the main development database.
Never send real emails, make payments, file returns or contact any sample identity.
Emails use reserved example.test addresses. GSTINs have valid local checksums for
positive tests but are fabricated, not confirmed registrations or proof of eligibility.

## How to use
1. Read manual_entry/parties.csv and create the fictional business/parties in your
   isolated app. The business row is a profile, not a BusinessParty; use business
   type proprietorship for the income-estimate example. Use a separate second demo account for authorization tests. No
   passwords, account IDs, live sender settings or security tokens are supplied.
2. Pick ONE scenario at a time. manual_entry/invoice_cases.json has actual form values,
   calculation options and expected saved values. Sales numbers are AUTO-ASSIGNED:
   record the number/ID the app actually returns. These JSON/CSV files are references,
   NOT supported bulk import formats or Django fixtures.
3. documents/*.pdf and ocr/*.{pdf,png,jpg} are uploadable alternatives. Choose the
   matching invoice type, extract, review every field/line, confirm, save, and verify
   separately. Do not count PDF/PNG/JPEG alternatives as different expenses. Images
   and scans require existing OCR; no OCR accuracy guarantee or auto-correction.
4. For credit/debit notes, create/finalize the original MANUALLY using the JSON values
   so saved Phase 4 lines/snapshots exist; uploaded invoices are not eligible originals.
   Select the actual original invoice/line IDs. Follow adjustments/*.json. Commercial
   estimates are separate from accounting invoices until the supported conversion.
5. Enter payment references from payments/reporting_regressions.json once through the
   payment form, only after the matching invoice exists. They are TEST transactions
   in the disposable demo database, never real payments. Read known-bug expectations
   before interpreting the historical or mixed-party running balance.
6. Recurring expenses use expenses/*.json: explicit historical catch-up, month-end
   dates, no duplicate periods and inclusive 1180.03 notes-only editing. Purchase
   attribution must not double-count one purchase as another expense. ITC confirmation
   requires independent evidence and review; do not blindly tick attestations.
7. tax/reconciliation_exact.json and .xlsx are the ONLY uploadable structured-data
   references here: same rows, choose ONE, return period 2026-11. They match the upload
   supplier reference FEATURE-PURCHASE-18. A manually created supplier purchase may
   use that supplier number; sales/note numbers are never hardcoded. Use actual app
   record/run IDs in tax working-paper forms; snapshots need explicit review.
8. ui/pagination_60_manual_references.csv is a reference list, NOT an importer. Create
   only in a disposable local dataset to check 25/25/10 pages. User/security lifecycle
   scenarios are manual instructions, not fabricated audit history or production load
   tests. Existing automated Django tests remain the faster option for large batches.

## Expected data and negative cases
Expected invoice components and treatment classifications are in expected/invoice_values.csv.
Each case is independent. Never add this pack to the old library's published golden
KPI totals without explicitly defining a new scenario. Taxable Amount here is the
saved value before GST; classified taxable/exempt/nil/non-GST values are separate.
Discount and inclusive pricing must be reviewed explicitly after OCR. Round-off is
not an unclassified charge. The MULTIPAGE PDF has 60 lines; line extraction can be
incomplete and requires review. Do not silently accept inaccurate OCR output.

negative/ contains deliberately missing, ambiguous, duplicate and corrupt documents,
plus harmless validation/rendering inputs. These must be rejected or left for review;
they are not valid invoices. No malicious executable, external URL or exploit payload
is included. The mixed text/scan PDF intentionally combines conflicting references
and must not be merged/accepted automatically.

Known current review findings are deliberately preserved as repro data: an October
invoice paid in November must remain outstanding at October end; the current paid
shortcut can report zero. A party with a 100 sale and 80 purchase has separate 100
receivable/80 payable or a 20 signed net, not a net settlement of 180. Very early date
filters may currently crash. Preset dashboard periods may ignore manually edited
dates unless Custom is selected. Generating data does not fix these bugs.

## Coverage
Manual sales/purchases; inclusive/exclusive and inter-state GST; rates/treatments;
discounts/round-off; multipage PDF, draft/cancelled watermarks via app lifecycle;
OCR text/scans/PNG/JPEG/rotation/low quality/missing/conflicts/corruption/duplicates;
expenses/receipts/recurrence/purchase attribution/ITC; quotations/proformas/conversion;
issued/received credit/debit notes and reversal; payments/overpayment/duplicate refs;
reconciliation and tax working-paper/income-estimate inputs; tenant isolation,
alerts/sessions/read-only audit integrity; UI filters/pagination/responsive checklists.
Live email, physical printing, official GST/ITC verification and statutory filing are
NOT tested or authorized. Email delivery should use locmem in isolated testing; any
live recipient/sender/send count needs separate explicit approval.

## Reproduce/verify
From the repository root:
  env/bin/python sample_data/generate_feature_library.py --verify
  env/bin/python sample_data/generate_feature_library.py --output /private/tmp/new-sample-pack
Generation refuses ANY existing destination, writes files exclusively and never
imports Django models or modifies databases. --verify only checks manifest hashes.
