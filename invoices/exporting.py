from config.export_security import spreadsheet_row, sanitize_workbook
from io import BytesIO

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.comments import Comment
from .export_evidence import financial_export_evidence, evidence_note

from .models import Invoice
from .date_evidence import with_invoice_dates
from django.utils import timezone


HEADER_FILL = PatternFill("solid", fgColor="526F64")
ISSUE_FILL = PatternFill("solid", fgColor="FCE8E6")


def invoice_batch_workbook_response(batch):
    invoices = list(with_invoice_dates(Invoice.objects.filter(user=batch.user, batch=batch)).prefetch_related("line_items"))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoices"
    sheet.append([
        "Invoice number", "Type", "Invoice date", "Supplier", "Supplier GSTIN",
        "Buyer", "Buyer GSTIN", "Taxable amount", "CGST", "SGST", "IGST",
        "Other charges", "Total", "Status", "Validation issues", "Source file",
        "Invoice date evidence", "Due date", "Due date evidence", "Uploaded at (IST)",
    ])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    evidence_by_id = {invoice.pk: financial_export_evidence(invoice, list(invoice.line_items.all())) for invoice in invoices}
    def annotate(sheet, row, column, key, field):
        sheet.cell(row, column).value = field['value']
        sheet.cell(row, column).comment = Comment(evidence_note(key, field), 'Smart Invoice')
        if field['state'] in {'unknown', 'missing', 'conflicting'}:
            sheet.cell(row, column).fill = ISSUE_FILL

    for invoice in invoices:
        dates=invoice.source_dates
        evidence_labels={'unknown':'Unknown — review required','extracted':'Extracted date — unconfirmed','reviewed':'Reviewed date','saved':'Saved date'}
        issues = list(invoice.validation_errors or [])
        if invoice.total_mismatch:
            issues.append("Invoice total does not match calculated total")
        sheet.append([
            invoice.invoice_number, invoice.get_invoice_type_display(), dates['invoice_date'],
            invoice.supplier_name, invoice.supplier_gstin, invoice.buyer_name, invoice.buyer_gstin,
            invoice.taxable_amount, invoice.cgst_amount, invoice.sgst_amount,
            invoice.igst_amount, invoice.other_charges, invoice.total_amount,
            invoice.get_status_display(), "; ".join(issues),
            invoice.source_file.name.rsplit("/", 1)[-1] if invoice.source_file else "",
            evidence_labels[dates['invoice_state']],
            dates['due_date'], evidence_labels[dates['due_state']], timezone.localtime(invoice.created_at).strftime('%Y-%m-%d %H:%M:%S %Z'),
        ])
        row = sheet.max_row
        for column in range(1, 17):
            sheet.cell(row, column).number_format = "General"
        for column in range(8, 14):
            sheet.cell(row, column).number_format = '"₹"#,##0.00'
        if issues:
            for column in range(1, 17):
                sheet.cell(row, column).fill = ISSUE_FILL

        evidence = evidence_by_id[invoice.pk]
        if evidence:
            for column, key in enumerate(('taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'other_charges', 'total_amount'), 8):
                annotate(sheet, row, column, key, evidence['header'][key])
            sheet.cell(row, 15).value = '; '.join([*issues, 'UNCONFIRMED: financial cells use extraction evidence; see comments.'])
            sheet.cell(row, 21).value = evidence['header']['rounding_adjustment']['value']
            sheet.cell(row, 21).comment = Comment(evidence_note('rounding_adjustment', evidence['header']['rounding_adjustment']), 'Smart Invoice')
    sheet.cell(1, 21).value = 'Extracted round-off (unconfirmed only)'

    line_sheet = workbook.create_sheet("Line Items")
    line_sheet.append([
        "Invoice number", "Description", "HSN/SAC", "Quantity", "Unit", "Rate",
        "Taxable amount", "Tax rate", "Tax amount", "Line total", "Validation issue", "Line discount", "Financial evidence",
    ])
    for cell in line_sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    for invoice in invoices:
        evidence = evidence_by_id[invoice.pk]
        for index, item in enumerate(invoice.line_items.all()):
            issues = []
            if not evidence and item.taxable_amount_mismatch:
                issues.append("Quantity × rate differs from taxable amount")
            if not evidence and item.tax_amount_mismatch:
                issues.append("Tax amount differs from rate calculation")
            if not evidence and item.total_amount != item.calculated_line_total:
                issues.append("Line total differs from taxable amount + tax")
            line_sheet.append([
                invoice.invoice_number, item.description, item.hsn_sac_code,
                item.quantity, item.unit, item.rate, item.taxable_amount,
                item.tax_rate, item.tax_amount, item.total_amount, "; ".join(issues), item.discount, "Saved / confirmed" if not evidence else "UNCONFIRMED",
            ])
            row = line_sheet.max_row
            if evidence:
                fields = evidence['lines'][index]
                for column, key in ((4, 'quantity'), (6, 'rate'), (7, 'taxable_amount'), (8, 'tax_rate'), (9, 'tax_amount'), (10, 'total_amount'), (12, 'discount')):
                    annotate(line_sheet, row, column, key, fields[key])
                line_sheet.cell(row, 11).value = '; '.join(evidence_note(key, value) for key, value in fields.items() if value['state'] != 'extracted') or 'Extracted candidates - review required'
            for column in (6, 7, 9, 10):
                line_sheet.cell(row, column).number_format = '"₹"#,##0.00'
            line_sheet.cell(row, 4).number_format = "0.000"
            if issues:
                for column in range(1, 12):
                    line_sheet.cell(row, column).fill = ISSUE_FILL

    for target_sheet in workbook.worksheets:
        target_sheet.freeze_panes = "A2"
        target_sheet.auto_filter.ref = target_sheet.dimensions
        for column in target_sheet.columns:
            letter = column[0].column_letter
            target_sheet.column_dimensions[letter].width = min(
                max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 42,
            )
    buffer = BytesIO()
    sanitize_workbook(workbook)
    workbook.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="invoice-batch-{batch.pk}.xlsx"'
    return response

