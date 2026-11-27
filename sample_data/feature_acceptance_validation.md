# Feature sample validation

41 generated fixture/reference files plus their SHA-256 manifest. The separate
pack extends the original 46 sample files without changing their data or golden totals.
The root sample README only gains a link and usage instructions.

- Ten fixture-only unit checks passed: fixed expected GST/inclusive/discount values,
  classifications, all 12 text-PDF header values, 60-line/A4 multipage output,
  negative missing/ambiguous fields, real image-only scans, image rotation dimensions,
  JSON/Excel reconciliation parity, current application income-estimate expectation,
  unique 60-row pagination references, manifest integrity and no-overwrite behavior.
- Installed Tesseract 5.5.4, direct clean-PNG OCR with --psm 6: **9/10 header fields**
  matched. Invoice date was missing; all invoice number/name/GSTIN/money fields matched.
  The date was NOT silently corrected. Ground truth remains 2026-11-01; mandatory
  visual/date review is required. One line item was detected.
- Rotated, low-quality and mixed-page samples are challenge fixtures; no accuracy
  guarantee or completed browser/end-to-end OCR acceptance is claimed for them.
- The income result 124800.00 is a regression expectation for the application's
  current bounded rule bundle, not independent statutory verification or tax advice.
- No models, database import, account creation, financial finalization, real email,
  external service, installation or migration was used to generate this data.

Run the commands in README.md. The generator refuses an existing output directory;
--verify checks the existing manifest without rewriting files. JSON/CSV references
are NOT bulk-import formats. Only document files and the explicitly labeled
reconciliation JSON/XLSX examples are intended for their matching upload screens.
