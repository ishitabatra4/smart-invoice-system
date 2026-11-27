from config.export_security import spreadsheet_row, sanitize_workbook
import uuid
from pathlib import Path
from django.http import FileResponse, Http404
import calendar
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from audit.services import record_event

from invoices.models import Invoice, NORMALIZATION_WHITESPACE
from .models import ReconciliationAction, ReconciliationRecord, ReconciliationRun
from .reconciliation import (
    ReconciliationFileError, amount_tolerance, normalize_invoice_number,
    parse_uploaded_file,
)


def _period_bounds(period):
    year, month = (int(part) for part in period.split("-"))
    return timezone.datetime(year, month, 1).date(), timezone.datetime(year, month, calendar.monthrange(year, month)[1]).date()


def _period_purchase_invoices(user, start, end):
    # Unconfirmed OCR storage dates must never select a GST reconciliation period.
    return Invoice.objects.exclude(document_state__in=("draft", "cancelled")).exclude(
        status__in=("uploaded", "processing", "review"), extraction_attempts__isnull=False,
    ).filter(user=user, invoice_type="purchase", invoice_date__range=(start, end)).distinct()


def _compare_record(record, invoice):
    differences = {}
    date_delta = abs((record.invoice_date - invoice.invoice_date).days)
    if date_delta:
        differences["invoice_date"] = {"gstr2b": record.invoice_date.isoformat(), "invoice": invoice.invoice_date.isoformat(), "difference_days": date_delta}
    has_amount_mismatch = False
    has_amount_difference = False
    for name, gstr_value, invoice_value in (
        ("taxable_value", record.taxable_value, invoice.taxable_amount),
        ("igst_amount", record.igst_amount, invoice.igst_amount),
        ("cgst_amount", record.cgst_amount, invoice.cgst_amount),
        ("sgst_amount", record.sgst_amount, invoice.sgst_amount),
        ("total_tax", record.total_tax, invoice.total_tax),
    ):
        delta = (gstr_value - invoice_value).quantize(Decimal("0.01"))
        if delta:
            has_amount_difference = True
            outside_tolerance = abs(delta) > amount_tolerance()
            has_amount_mismatch = has_amount_mismatch or outside_tolerance
            differences[name] = {
                "gstr2b": str(gstr_value), "invoice": str(invoice_value),
                "difference": str(delta), "within_tolerance": not outside_tolerance,
            }
    exact = not differences and record.invoice_date == invoice.invoice_date and all(
        gstr_value == invoice_value for gstr_value, invoice_value in (
            (record.taxable_value, invoice.taxable_amount), (record.igst_amount, invoice.igst_amount),
            (record.cgst_amount, invoice.cgst_amount), (record.sgst_amount, invoice.sgst_amount),
            (record.total_tax, invoice.total_tax),
        )
    )
    if date_delta or has_amount_mismatch:
        status = "mismatch"
    elif exact:
        status = "matched"
    else:
        status = "tolerance_match" if has_amount_difference else "matched"
    return differences, status


def _invoice_number_is_exact(record, invoice):
    return (record.invoice_number or "") == (invoice.invoice_number or "")


def _manual_match_comparison(record, invoice):
    rows = [
        (
            "Supplier GSTIN", record.supplier_gstin, invoice.supplier_gstin,
            "Matches" if record.supplier_gstin.strip().upper() == (invoice.supplier_gstin or "").strip().upper() else "Different",
        ),
        (
            "Invoice number", record.invoice_number, invoice.invoice_number,
            "Matches" if _invoice_number_is_exact(record, invoice) else "Different",
        ),
        ("Invoice date", record.invoice_date, invoice.invoice_date,
         (record.invoice_date - invoice.invoice_date).days),
    ]
    for label, gstr_value, invoice_value in (
        ("Taxable value", record.taxable_value, invoice.taxable_amount),
        ("IGST", record.igst_amount, invoice.igst_amount),
        ("CGST", record.cgst_amount, invoice.cgst_amount),
        ("SGST", record.sgst_amount, invoice.sgst_amount),
        ("Total tax", record.total_tax, invoice.total_tax),
    ):
        rows.append((label, gstr_value, invoice_value, (gstr_value - invoice_value).quantize(Decimal("0.01"))))
    return rows


@login_required
def reconciliation_list_view(request):
    runs = ReconciliationRun.objects.filter(user=request.user).prefetch_related("records")
    return render(request, "taxation/reconciliation_list.html", {"runs": runs, "tolerance": amount_tolerance()})


@login_required
@require_POST
def reconciliation_upload_view(request):
    period = request.POST.get("return_period", "")
    upload = request.FILES.get("source_file")
    try:
        start, end = _period_bounds(period)
        if start.strftime("%Y-%m") != period:
            raise ValueError
    except (ValueError, TypeError):
        messages.error(request, "Select a valid GST return period in YYYY-MM format.")
        return redirect("reconciliation_list")
    if not upload:
        messages.error(request, "Select a GSTR-2B .xlsx or .json file to upload.")
        return redirect("reconciliation_list")
    if upload.size > 10 * 1024 * 1024:
        messages.error(request, "GSTR-2B files must be 10 MB or smaller.")
        return redirect("reconciliation_list")
    try:
        parsed_records = parse_uploaded_file(upload)
    except ReconciliationFileError as exc:
        messages.error(request, str(exc))
        return redirect("reconciliation_list")
    purchase_invoices = list(_period_purchase_invoices(request.user, start, end))
    tolerance = amount_tolerance()
    upload.name = uuid.uuid4().hex + Path(upload.name).suffix.lower()
    upload.seek(0)
    with transaction.atomic():
        run = ReconciliationRun.objects.create(user=request.user, return_period=period, source_file=upload)
        matched_ids = set()
        for data in parsed_records:
            rec = ReconciliationRecord.objects.create(run=run, **data)
            candidates = [inv for inv in purchase_invoices
                if (inv.supplier_gstin or "").strip(NORMALIZATION_WHITESPACE).upper() == data["supplier_gstin"]
                and normalize_invoice_number(inv.invoice_number) == normalize_invoice_number(data["invoice_number"])]
            if len(candidates) > 1:
                rec.status = "manual_review"
                rec.differences = {"candidate_invoice_ids": [invoice.pk for invoice in candidates], "message": "Multiple purchase invoices share this supplier GSTIN and normalized invoice number."}
            elif len(candidates) == 1 and candidates[0].pk not in matched_ids:
                invoice = candidates[0]
                rec.matched_invoice = invoice
                matched_ids.add(invoice.pk)
                rec.differences, rec.status = _compare_record(rec, invoice)
            else:
                rec.status = "unmatched"
                rec.differences = {"message": f"No purchase invoice found in the selected return period (amount tolerance ₹{tolerance})."}
            rec.save(update_fields=["matched_invoice", "status", "differences"])
        counts = {}
        for record in run.records.all():
            counts[record.status] = counts.get(record.status, 0) + 1
        record_event(request=request, user=request.user, action="upload", module="taxation", description=f"GSTR-2B reconciliation uploaded for {period}.", obj=run, after={"return_period": period, "record_count": len(parsed_records), "status_counts": counts})
    messages.success(request, f"Imported {len(parsed_records)} unique GSTR-2B record(s) for {period}.")
    return redirect("reconciliation_run", run_id=run.pk)


@login_required
def reconciliation_run_view(request, run_id):
    run = get_object_or_404(ReconciliationRun, pk=run_id, user=request.user)
    start, end = _period_bounds(run.return_period)
    records = run.records.select_related("matched_invoice").prefetch_related("actions")
    seen_keys = {(r.supplier_gstin.upper(), normalize_invoice_number(r.invoice_number)) for r in run.records.all()}
    period_invoices = _period_purchase_invoices(request.user, start, end)
    missing = [inv for inv in period_invoices if ((inv.supplier_gstin or "").strip(NORMALIZATION_WHITESPACE).upper(), normalize_invoice_number(inv.invoice_number)) not in seen_keys]
    candidates = list(_period_purchase_invoices(request.user, start, end))
    for record in records:
        ranked_candidates = []
        for invoice in candidates:
            ranked_candidates.append({
                "invoice": invoice,
                "supplier_match": (
                    (invoice.supplier_gstin or "").strip(NORMALIZATION_WHITESPACE).upper()
                    == record.supplier_gstin.upper()
                ),
                "number_match": (
                    normalize_invoice_number(invoice.invoice_number)
                    == normalize_invoice_number(record.invoice_number)
                ),
                "date_match": invoice.invoice_date == record.invoice_date,
            })
        record.manual_candidates = sorted(
            ranked_candidates,
            key=lambda candidate: (
                not candidate["supplier_match"],
                not candidate["number_match"],
                not candidate["date_match"],
                abs((candidate["invoice"].invoice_date - record.invoice_date).days),
                candidate["invoice"].invoice_number.casefold(),
            ),
        )
    return render(request, "taxation/reconciliation_run.html", {
        "run": run, "records": records, "missing_invoices": missing,
        "candidate_invoices": candidates, "tolerance": amount_tolerance(),
    })


@login_required
@require_POST
@transaction.atomic
def reconciliation_match_view(request, run_id, record_id):
    run = get_object_or_404(ReconciliationRun, pk=run_id, user=request.user)
    record = get_object_or_404(ReconciliationRecord, pk=record_id, run=run)
    if record.matched_invoice_id and not record.manually_matched_at:
        messages.error(request, "This GSTR-2B record already has an automatic invoice match. Remove it through review before changing the match.")
        return redirect("reconciliation_run", run_id=run.pk)
    if record.manually_matched_at:
        messages.error(request, "Remove the current manual match before choosing another invoice.")
        return redirect("reconciliation_run", run_id=run.pk)
    raw_invoice_id = request.POST.get("invoice_id", "")
    if not raw_invoice_id.isdecimal() or len(raw_invoice_id) > 18:
        raise Http404("Invoice unavailable.")
    invoice = get_object_or_404(Invoice, pk=int(raw_invoice_id), user=request.user, invoice_type="purchase", document_state__in=("legacy", "finalized"))
    if run.records.exclude(pk=record.pk).filter(matched_invoice=invoice).exists():
        messages.error(request, "This invoice is already matched to another record in this run.")
        return redirect("reconciliation_run", run_id=run.pk)
    start, end = _period_bounds(run.return_period)
    if invoice.extraction_attempts.exists() and invoice.status not in ("verified", "paid"):
        messages.error(request, "Confirm and verify the extracted invoice date before GST reconciliation.")
        return redirect("reconciliation_run", run_id=run.pk)
    if not start <= invoice.invoice_date <= end:
        messages.error(request, "Choose a purchase invoice dated within this GST return period.")
    else:
        confirmation_key = {
            "run_id": run.pk,
            "record_id": record.pk,
            "invoice_id": invoice.pk,
            "user_id": request.user.pk,
        }
        confirmation_requested = request.POST.get("confirm_number_difference") == "1"
        if not confirmation_requested and not _invoice_number_is_exact(record, invoice):
            return render(request, "taxation/reconciliation_match_confirm.html", {
                "run": run,
                "record": record,
                "invoice": invoice,
                "comparison_rows": _manual_match_comparison(record, invoice),
                "confirmation_token": signing.dumps(confirmation_key, salt="taxation.manual_match"),
            })
        if confirmation_requested:
            try:
                confirmed = signing.loads(
                    request.POST.get("confirmation_token", ""),
                    salt="taxation.manual_match",
                    max_age=600,
                )
            except signing.BadSignature:
                confirmed = None
            if confirmed != confirmation_key:
                messages.error(request, "The manual match confirmation expired or changed. Please select the invoice again.")
                return redirect("reconciliation_run", run_id=run.pk)
        with transaction.atomic():
            record.matched_invoice = invoice
            record.differences, record.status = _compare_record(record, invoice)
            record.manually_matched_at = timezone.now()
            record.manually_matched_by = request.user
            record.save()
            ReconciliationAction.objects.create(record=record, user=request.user, action="match", invoice=invoice)
            record_event(request=request, user=request.user, action="match", module="taxation", description=f"GSTR-2B record {record.invoice_number} manually matched to invoice {invoice.invoice_number}.", obj=record, before={"status": "unmatched", "matched_invoice": None}, after={"status": record.status, "matched_invoice": invoice.invoice_number})
        messages.success(request, "Manual invoice match recorded.")
    return redirect("reconciliation_run", run_id=run.pk)


@login_required
@require_POST
@transaction.atomic
def reconciliation_unmatch_view(request, run_id, record_id):
    run = get_object_or_404(ReconciliationRun, pk=run_id, user=request.user)
    record = get_object_or_404(ReconciliationRecord, pk=record_id, run=run)
    if record.manually_matched_at:
        previous_invoice_number = record.matched_invoice.invoice_number if record.matched_invoice else None
        ReconciliationAction.objects.create(record=record, user=request.user, action="unmatch", invoice=record.matched_invoice)
        record.matched_invoice = None
        record.manually_matched_at = None
        record.manually_matched_by = None
        record.status = "unmatched"
        record.differences = {"message": "Manual match removed."}
        record.save()
        record_event(request=request, user=request.user, action="unmatch", module="taxation", description=f"Manual match removed for GSTR-2B record {record.invoice_number}.", obj=record, before={"matched_invoice": previous_invoice_number}, after={"matched_invoice": None, "status": "unmatched"})
        messages.success(request, "Manual match removed.")
    else:
        messages.error(request, "Only a manual match can be removed here.")
    return redirect("reconciliation_run", run_id=run.pk)


@login_required
def reconciliation_export_view(request, run_id):
    from openpyxl import Workbook
    run = get_object_or_404(ReconciliationRun, pk=run_id, user=request.user)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "GSTR-2B Reconciliation"
    sheet.append(["Status", "Supplier GSTIN", "GSTR-2B Invoice Number", "Invoice Date", "Taxable Value", "IGST", "CGST", "SGST", "Total Tax", "Invoice Number", "Differences"])
    for rec in run.records.select_related("matched_invoice"):
        sheet.append([rec.get_status_display(), rec.supplier_gstin, rec.invoice_number, rec.invoice_date.isoformat(), rec.taxable_value, rec.igst_amount, rec.cgst_amount, rec.sgst_amount, rec.total_tax, rec.matched_invoice.invoice_number if rec.matched_invoice else "", str(rec.differences)])
    start, end = _period_bounds(run.return_period)
    seen_keys = {(rec.supplier_gstin.upper(), normalize_invoice_number(rec.invoice_number)) for rec in run.records.all()}
    missing_invoices = _period_purchase_invoices(request.user, start, end)
    for invoice in missing_invoices:
        key = ((invoice.supplier_gstin or "").strip(NORMALIZATION_WHITESPACE).upper(), normalize_invoice_number(invoice.invoice_number))
        if key not in seen_keys:
            sheet.append(["Missing in GSTR-2B", invoice.supplier_gstin, invoice.invoice_number, invoice.invoice_date.isoformat(), invoice.taxable_amount, invoice.igst_amount, invoice.cgst_amount, invoice.sgst_amount, invoice.total_tax, invoice.invoice_number, "No corresponding record in this run"])
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="gstr2b-reconciliation-{run.return_period}.xlsx"'
    sanitize_workbook(workbook)
    workbook.save(response)
    record_event(request=request, user=request.user, action="export", module="taxation", description=f"GSTR-2B reconciliation exported for {run.return_period}.", obj=run, after={"format": "xlsx", "record_count": run.records.count()})
    return response


@login_required
def reconciliation_source_view(request, run_id):
    run = get_object_or_404(ReconciliationRun, pk=run_id, user=request.user)
    if not run.source_file:
        raise Http404("Source document unavailable.")
    try:
        handle = run.source_file.open("rb")
    except (OSError, ValueError):
        raise Http404("Source document unavailable.") from None
    return FileResponse(handle, as_attachment=True, filename="gstr2b-source" + Path(run.source_file.name).suffix, content_type="application/octet-stream")
