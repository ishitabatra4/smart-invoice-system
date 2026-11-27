from config.export_security import spreadsheet_row, sanitize_workbook
import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO

from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from accounts.models import BusinessParty
from invoices.models import Invoice,InvoicePayment
from invoices.ledger import invoice_outstanding, payment_balance_state
from .services import managed_invoices,itc_state,advanced_ready

from .forms import ExpenseReportFilterForm


MONEY_COLUMNS = ("Value before GST", "GST", "Total", "Paid", "Outstanding")
HEADERS = ("Expense number", "Expense date", "Supplier", "Category", *MONEY_COLUMNS)
CLASSIFICATIONS = (("taxable", "Taxable"), ("exempt", "Exempt"), ("nil_rated", "Nil-rated"), ("non_gst", "Non-GST"), ("unclassified", "Unclassified"))


def saved_classifications(invoice):
    """Use declared saved values only; rates and current party data cannot classify history."""
    values = {key: Decimal("0.00") for key, _ in CLASSIFICATIONS}
    breakdown = invoice.treatment_breakdown
    if invoice.calculation_version == 1 and isinstance(breakdown, dict):
        try:
            for key, _ in CLASSIFICATIONS[:-1]:
                values[key] = Decimal(str(breakdown.get(key, "0")))
            if any(not value.is_finite() or value < 0 for value in values.values()):
                raise ValueError
            classified = sum(values.values(), Decimal("0.00"))
            if classified > invoice.taxable_amount:
                raise ValueError
            values["unclassified"] = invoice.taxable_amount - classified
            return values
        except (InvalidOperation, ValueError):
            values = {key: Decimal("0.00") for key, _ in CLASSIFICATIONS}
    values["unclassified"] = invoice.taxable_amount
    return values


def _fiscal_year_start(today, month):
    return date(today.year if today.month >= month else today.year - 1, month, 1)


def report_rows(user, business, params):
    form = ExpenseReportFilterForm(params, business=business)
    if not form.is_valid():
        return form, [], {}
    data = form.cleaned_data
    today = timezone.localdate()
    report_type = data.get("report_type") or "complete"
    start, end = data.get("start_date"), data.get("end_date")
    if report_type == "monthly" and not start and not end:
        start, end = today.replace(day=1), today
    elif report_type == "financial_year" and not start and not end:
        start, end = _fiscal_year_start(today, business.financial_year_start_month or 4), today
    qs = managed_invoices(user).filter(status__in=("verified", "paid")).select_related(
        "supplier_party", "expense_detail__category",
    ).prefetch_related("payments")
    if start and report_type != "payments":
        qs = qs.filter(invoice_date__gte=start)
    if end and report_type != "payments":
        qs = qs.filter(invoice_date__lte=end)
    if data.get("category"):
        qs = qs.filter(expense_detail__category=data["category"])
    if data.get("supplier"):
        qs = qs.filter(supplier_party=data["supplier"])

    if report_type=="linked":qs=qs.filter(invoice_type="purchase")
    if report_type=="recurring":qs=qs.filter(recurrence_generation__isnull=False)
    if qs.count()>5000 or InvoicePayment.objects.filter(invoice__in=qs,user=user).count()>10000:
        form.add_error(None,"Report limit reached. Narrow dates or filters (5,000 invoices / 10,000 payments).");return form,[],{}
    # Same batched evidence path as the expense list (one latest-review query, prefetched
    # lines/adjustments, per-request document hash cache). ITC semantics are unchanged.
    available = advanced_ready()
    qs = qs.prefetch_related("line_items", "adjustment_notes")
    if available:
        from django.db.models import Prefetch
        from .models import ExpenseITCReview
        qs = qs.prefetch_related(Prefetch("itc_reviews", queryset=ExpenseITCReview.objects.filter(business=business).order_by("-pk")[:1], to_attr="_latest_itc_reviews"))
    evidence_context = {"business": business, "available": available, "documents": {}, "bytes_read": 0}
    rows, payments = [], []
    for invoice in qs:
        paid = sum((payment.amount for payment in invoice.payments.all() if payment.user_id == user.pk), Decimal("0.00"))
        is_overdue = bool(invoice.due_date and invoice.due_date < today and invoice_outstanding(invoice)>0)
        state = "overdue" if is_overdue else payment_balance_state(invoice)
        if data.get("status") and state != data["status"]:
            continue
        itc=itc_state(invoice, context=evidence_context)
        if data.get("itc_status") and itc["status"]!=data["itc_status"]:continue
        detail = getattr(invoice, "expense_detail", None)
        row = {
            "invoice": invoice,
            "number": invoice.invoice_number,
            "date": invoice.invoice_date,
            "supplier": invoice.supplier_name,
            "category": detail.category.name if detail else "Uncategorized",
            "taxable": invoice.taxable_amount,
            "gst": invoice.total_tax,
            "total": invoice.total_amount,
            "paid": paid,
            "outstanding": invoice_outstanding(invoice),
            "status": state,
            "classifications": saved_classifications(invoice),
            "itc_status":itc["status"],"itc_amount":itc["amount"],"source_type":invoice.invoice_type,
        }
        row["classification_items"] = [(label, row["classifications"][key]) for key, label in CLASSIFICATIONS]
        if report_type == "payments" and not any(
            p.user_id == user.pk and (not start or p.payment_date >= start) and (not end or p.payment_date <= end)
            for p in invoice.payments.all()
        ):
            continue
        rows.append(row)
        if report_type == "payments":
            for payment in invoice.payments.all():
                if payment.user_id == user.pk and (not start or payment.payment_date >= start) and (not end or payment.payment_date <= end):
                    payments.append({**row, "payment": payment})

    rows.sort(key=lambda row: (row["date"], row["number"]), reverse=True)
    if report_type == "payments":
        payments.sort(key=lambda row: (row["payment"].payment_date, row["payment"].pk), reverse=True)
    totals = {
        "count": len(payments) if report_type == "payments" else len(rows),
        "expense_count": len(rows),
        "taxable": sum((row["taxable"] for row in rows), Decimal("0.00")),
        "gst": sum((row["gst"] for row in rows), Decimal("0.00")),
        "total": sum((row["total"] for row in rows), Decimal("0.00")),
        "paid": sum((row["paid"] for row in rows), Decimal("0.00")),
        "outstanding": sum((row["outstanding"] for row in rows), Decimal("0.00")),
    }
    totals["itc"]={status:sum((r["itc_amount"] for r in rows if r["itc_status"]==status),Decimal("0.00")) for status in ("potential","confirmed")}
    totals["itc_pending_count"]=sum(r["itc_status"]=="pending" for r in rows)
    if report_type == "payments":
        totals["payment_amount"] = sum((row["payment"].amount for row in payments), Decimal("0.00"))
    totals["classifications"] = {key: sum((row["classifications"][key] for row in rows), Decimal("0.00")) for key, _ in CLASSIFICATIONS}
    totals["classification_items"] = [(label, totals["classifications"][key]) for key, label in CLASSIFICATIONS]
    group_field = {"category": "category", "supplier": "supplier", "payment_status": "status"}.get(report_type)
    if report_type == "monthly":
        group_field = "month"
    groups = defaultdict(lambda: {"count": 0, "taxable": Decimal("0.00"), "gst": Decimal("0.00"), "total": Decimal("0.00"), "paid": Decimal("0.00"), "outstanding": Decimal("0.00")})
    if group_field:
        for row in rows:
            label = row["date"].strftime("%b %Y") if group_field == "month" else row[group_field]
            group = groups[label]
            group["count"] += 1
            for field in ("taxable", "gst", "total", "paid", "outstanding"):
                group[field] += row[field]
        totals["groups"] = sorted(groups.items())
    if report_type == "gst":
        invoices = [row["invoice"] for row in rows]
        totals["tax_breakdown"] = {
            key: sum((getattr(invoice, f"{key}_amount") for invoice in invoices), Decimal("0.00"))
            for key in ("cgst", "sgst", "igst")
        }
    return form, payments if report_type == "payments" else rows, totals


def _format_money(amount):
    return f"INR {amount:,.2f}"


def _csv_response(rows, report_type, totals):
    output = StringIO()
    writer = csv.writer(output)
    headers = list(HEADERS) + ["Status"]
    if report_type == "payments":
        headers += ["Payment date", "Payment amount", "Payment mode", "Payment reference"]
    headers += [label for _, label in CLASSIFICATIONS]+["Source type","ITC preparation status","Reviewed GST (not a claim)"]
    writer.writerow(headers)
    for row in rows:
        values = [row["number"], row["date"].isoformat(), row["supplier"], row["category"]]
        values.extend(_format_money(row[key]) for key in ("taxable", "gst", "total", "paid", "outstanding"))
        values.append(row["status"])
        if report_type == "payments":
            payment = row["payment"]
            values.extend([payment.payment_date.isoformat(), _format_money(payment.amount), payment.get_payment_mode_display(), payment.reference_id])
        values.extend(_format_money(row["classifications"][key]) for key, _ in CLASSIFICATIONS)
        values.extend([row["source_type"],row["itc_status"],_format_money(row["itc_amount"])])
        writer.writerow(spreadsheet_row(values))
    writer.writerow([])
    writer.writerow(["Totals", f"Expenses: {totals['expense_count']}", f"Records: {totals['count']}", "Value before GST", _format_money(totals["taxable"]), "GST", _format_money(totals["gst"]), "Total", _format_money(totals["total"]), "Paid", _format_money(totals["paid"]), "Outstanding", _format_money(totals["outstanding"])])
    writer.writerow(["Tax classification", "Value before GST"])
    for label, value in totals["classification_items"]:
        writer.writerow([label, _format_money(value)])
    if report_type == "payments":
        writer.writerow(["Payment amount total", _format_money(totals["payment_amount"])])
    if totals.get("groups"):
        writer.writerow([])
        writer.writerow(["Group", "Count", "Value before GST", "GST", "Total", "Paid", "Outstanding"])
        for label, values in totals["groups"]:
            writer.writerow(spreadsheet_row([label, values["count"], *(_format_money(values[key]) for key in ("taxable", "gst", "total", "paid", "outstanding"))]))
    if totals.get("tax_breakdown"):
        writer.writerow(["CGST", _format_money(totals["tax_breakdown"]["cgst"]), "SGST", _format_money(totals["tax_breakdown"]["sgst"]), "IGST", _format_money(totals["tax_breakdown"]["igst"])])
    return HttpResponse(output.getvalue(), content_type="text/csv; charset=utf-8")


def _xlsx_response(rows, report_type, totals):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Expense report"
    sheet.append(list(HEADERS) + ["Status"] + (["Payment date", "Payment amount", "Payment mode", "Payment reference"] if report_type == "payments" else []) + [label for _, label in CLASSIFICATIONS]+["Source type","ITC preparation status","Reviewed GST (not a claim)"])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="526F64")
    for row in rows:
        values = [row["number"], row["date"], row["supplier"], row["category"]]
        values.extend(row[key] for key in ("taxable", "gst", "total", "paid", "outstanding"))
        values.append(row["status"])
        if report_type == "payments":
            payment = row["payment"]
            values.extend([payment.payment_date, payment.amount, payment.get_payment_mode_display(), payment.reference_id])
        values.extend(row["classifications"][key] for key, _ in CLASSIFICATIONS)
        values.extend([row["source_type"],row["itc_status"],row["itc_amount"]])
        sheet.append(values)
    for row in sheet.iter_rows(min_row=2):
        for cell in row[4:9]:
            cell.number_format = '"₹"#,##0.00'
        if report_type == "payments":
            row[11].number_format = '"₹"#,##0.00'
        for cell in row[-len(CLASSIFICATIONS)-3:-3]:
            cell.number_format = '"₹"#,##0.00'
    sheet.append([])
    sheet.append(["Totals", "Expenses", totals["expense_count"], "Records", totals["count"], "Value before GST", totals["taxable"], "GST", totals["gst"], "Total", totals["total"], "Paid", totals["paid"], "Outstanding", totals["outstanding"]])
    for column_index in (7, 9, 11, 13, 15):
        sheet.cell(sheet.max_row, column_index).number_format = '"₹"#,##0.00'
    if report_type == "payments":
        sheet.append(["Payment amount total", totals["payment_amount"]])
        sheet.cell(sheet.max_row, 2).number_format = '"₹"#,##0.00'
    if totals.get("groups"):
        summary = workbook.create_sheet("Summary")
        summary.append(["Group", "Count", "Value before GST", "GST", "Total", "Paid", "Outstanding"])
        for cell in summary[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="526F64")
        for label, values in totals["groups"]:
            summary.append([label, values["count"], values["taxable"], values["gst"], values["total"], values["paid"], values["outstanding"]])
            for cell in summary[summary.max_row][2:]:
                cell.number_format = '"₹"#,##0.00'
    if totals.get("tax_breakdown"):
        summary = workbook.create_sheet("GST summary")
        summary.append(["Tax component", "Amount"])
        for key, value in totals["tax_breakdown"].items():
            summary.append([key.upper(), value])
            summary.cell(summary.max_row, 2).number_format = '"₹"#,##0.00'
    classification_sheet = workbook.create_sheet("Tax classifications")
    classification_sheet.column_dimensions["A"].width = 24
    classification_sheet.column_dimensions["B"].width = 24
    classification_sheet.append(["Tax classification", "Value before GST"])
    for label, value in totals["classification_items"]:
        classification_sheet.append([label, value])
        classification_sheet.cell(classification_sheet.max_row, 2).number_format = '"₹"#,##0.00'
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 32)
    buffer = BytesIO()
    sanitize_workbook(workbook)
    workbook.save(buffer)
    return HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _pdf_escape(value):
    safe = str(value).replace("₹", "INR ").encode("ascii", "replace").decode("ascii")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_response(rows, report_type, totals):
    headers = "Expense no. | Date | Supplier / Category | Value before GST | GST | Total | Paid | Outstanding"
    lines = ["Smart Invoice - Expense report", f"Report type: {report_type.replace('_', ' ').title()}", "", headers, "-" * 116]
    for row in rows:
        lines.append(f"{row['number']} | {row['date']:%d-%b-%Y} | {row['supplier']} / {row['category']}")
        lines.append("Value before GST " + _format_money(row["taxable"]) + "   GST " + _format_money(row["gst"]) + "   Total " + _format_money(row["total"]) + "   Paid " + _format_money(row["paid"]) + "   Due " + _format_money(row["outstanding"]))
        lines.append("Classifications: " + " / ".join(label + " " + _format_money(value) for label, value in row["classification_items"] if value))
        lines.append(f"Source: {row['source_type']} / ITC preparation: {row['itc_status']} / Reviewed GST (not claim): {_format_money(row['itc_amount'])}")
        if report_type == "payments":
            payment = row["payment"]
            lines.append(f"Payment: {payment.payment_date:%d-%b-%Y} / {_format_money(payment.amount)} / {payment.get_payment_mode_display()} / {payment.reference_id}")
    lines.extend(["", f"Expenses: {totals['expense_count']}  Records: {totals['count']}", f"Value before GST: {_format_money(totals['taxable'])}  GST: {_format_money(totals['gst'])}", f"Total: {_format_money(totals['total'])}  Paid: {_format_money(totals['paid'])}  Outstanding: {_format_money(totals['outstanding'])}"])
    lines.append("Tax classifications (value before GST):")
    lines.extend(label + ": " + _format_money(value) for label, value in totals["classification_items"])
    if report_type == "payments":
        lines.append("Payment amount total: " + _format_money(totals["payment_amount"]))
    if totals.get("groups"):
        lines.extend(["", "Grouped summary:"])
        lines.extend(f"{label}: {values['count']} record(s), total {_format_money(values['total'])}" for label, values in totals["groups"])
    if totals.get("tax_breakdown"):
        lines.append("GST components: " + " / ".join(f"{key.upper()} {_format_money(value)}" for key, value in totals["tax_breakdown"].items()))
    wrapped_lines = []
    for line in lines:
        while len(line) > 120:
            wrapped_lines.append(line[:120])
            line = line[120:]
        wrapped_lines.append(line)
    page_lines = [wrapped_lines[index:index + 55] for index in range(0, len(wrapped_lines), 55)] or [[]]
    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_refs = []
    for page in page_lines:
        content_id = len(objects) + 1
        page_id = content_id + 1
        text_ops = ["BT", "/F1 8 Tf", "36 756 Td", "10 TL"]
        for line in page:
            text_ops.append(f"({_pdf_escape(line[:190])}) Tj T*")
        text_ops.append("ET")
        content = "\n".join(text_ops).encode("ascii", "replace")
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode())
        page_refs.append(f"{page_id} 0 R")
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>".encode()
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())
    response = HttpResponse(bytes(pdf), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="expense-report.pdf"'
    return response


def export_report(rows, report_type, totals, export_format):
    if export_format == "csv":
        response = _csv_response(rows, report_type, totals)
        response["Content-Disposition"] = 'attachment; filename="expense-report.csv"'
        return response
    if export_format == "xlsx":
        response = _xlsx_response(rows, report_type, totals)
        response["Content-Disposition"] = 'attachment; filename="expense-report.xlsx"'
        return response
    if export_format == "pdf":
        return _pdf_response(rows, report_type, totals)
    raise ValueError("Unsupported export format")
