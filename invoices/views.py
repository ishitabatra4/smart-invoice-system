from .identity import normalized_identity
from django.views.decorators.clickjacking import xframe_options_sameorigin
import os
import logging
import re
import tempfile
import uuid
from django.conf import settings
from django.http import FileResponse, Http404
from .file_security import validate_document
from datetime import date
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.db.models import Q
from django.db.models.functions import Lower, Trim
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_http_methods, require_POST
from django.core.exceptions import ValidationError

from accounts.models import BusinessParty, BusinessProfile
from audit.models import AuditEvent
from audit.services import record_event
from expenses.models import ExpenseCategory, ExpenseDetail

from .extraction import OCRUnavailableError, extract_invoice_data
from .models import NORMALIZATION_WHITESPACE, Invoice, InvoiceBatch, InvoiceLineItem
from .forms import InvoiceEditForm, InvoiceLineItemFormSet, InvoiceListFilterForm
from django.core.paginator import Paginator


ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_FILE_SIZE = 10 * 1024 * 1024
logger = logging.getLogger(__name__)


class InvoiceFileIssue(Exception):
    """A safe, user-facing per-file upload validation error."""


def get_or_create_party(business, name, gstin, party_type, request=None):
    """Find or create a party by GSTIN within the current business.

    A party is not merged automatically by name alone.
    """

    name = (name or "").strip()
    gstin = (gstin or "").strip().upper()

    if not name:
        return None

    if gstin:
        party = BusinessParty.objects.filter(
            business=business,
            gstin=gstin,
        ).first()

        if party:
            before = {"name": party.name, "party_type": party.party_type}
            if party.party_type != party_type:
                if party.party_type == "both":
                    pass
                else:
                    party.party_type = "both"

            if not party.name:
                party.name = name

            party.save()
            after = {"name": party.name, "party_type": party.party_type}
            if before != after and request:
                record_event(request=request, business=business, action="update", module="parties", description=f"Party {party.name} updated while linking an invoice.", obj=party, before=before, after=after)
            return party

    party = BusinessParty.objects.create(
        business=business,
        name=name,
        gstin=gstin,
        party_type=party_type,
    )
    if request:
        record_event(request=request, business=business, action="create", module="parties", description=f"Party {party.name} created while linking an invoice.", obj=party, after={"name": party.name, "party_type": party.party_type, "gstin": party.gstin})
    return party


def normalize_invoice_number(value):
    return (value or "").strip(NORMALIZATION_WHITESPACE).upper()


def normalize_gstin(value):
    return (value or "").strip(NORMALIZATION_WHITESPACE).upper()


def normalize_party_name(value):
    return " ".join((value or "").split()).casefold()


def _invoice_validation_issues(invoice, previous_issues=(), confirmed_fields=(), line_items_confirmed=False):
    """Rebuild review issues from the corrected header and current line items."""
    issues = []
    confirmed_fields = set(confirmed_fields)
    if not invoice.invoice_number.strip():
        issues.append("Invoice number is required.")
    if not invoice.supplier_name.strip():
        issues.append("Supplier name is required.")
    if invoice.invoice_type == "sales" and not invoice.buyer_name.strip():
        issues.append("Buyer name is required for a sales invoice.")
    gstin_pattern = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
    for label, value in (("Supplier", invoice.supplier_gstin), ("Buyer", invoice.buyer_gstin)):
        if value and not gstin_pattern.fullmatch(normalize_gstin(value)):
            issues.append(f"{label} GSTIN does not match the expected 15-character format.")
    from .manual_calculations import validate_stored_totals
    issues.extend(validate_stored_totals(invoice.taxable_amount,invoice.cgst_amount,invoice.sgst_amount,invoice.igst_amount,invoice.other_charges,invoice.total_amount))
    if invoice.total_mismatch:
        issues.append("Invoice total does not match taxable amount, taxes, and other charges.")
    items = list(invoice.line_items.all())
    if not items:
        issues.append("No line items were extracted; add or confirm invoice line items before verification.")
    for item in items:
        if item.taxable_amount_mismatch:
            issues.append(f"Taxable amount for '{item.description}' does not match quantity multiplied by rate.")
        if item.tax_amount_mismatch:
            issues.append(f"Tax amount for '{item.description}' does not match the declared tax rate.")
        if item.total_amount != item.calculated_line_total:
            issues.append(f"Line total for '{item.description}' does not match taxable amount plus tax.")
    if items and sum((item.taxable_amount for item in items), 0) != invoice.taxable_amount:
        issues.append("Line-item taxable amounts do not match the invoice taxable amount.")
    if items and sum((item.tax_amount for item in items), 0) != invoice.total_tax:
        issues.append("Line-item tax amounts do not match the invoice tax amounts.")
    missing_field_names = {
        "invoice number": ("invoice_number", invoice.invoice_number),
        "invoice date": ("invoice_date", invoice.invoice_date),
        "supplier name": ("supplier_name", invoice.supplier_name),
        "buyer name": ("buyer_name", invoice.buyer_name),
        "taxable amount": ("taxable_amount", invoice.taxable_amount),
        "cgst amount": ("cgst_amount", invoice.cgst_amount),
        "sgst amount": ("sgst_amount", invoice.sgst_amount),
        "igst amount": ("igst_amount", invoice.igst_amount),
        "other charges": ("other_charges", invoice.other_charges),
        "grand total": ("total_amount", invoice.total_amount),
    }
    for previous in previous_issues:
        lowered = str(previous).lower()
        if (
            "invoice total does not reconcile with saved components" in lowered
            or "financial values cannot be negative" in lowered
            or "cgst/sgst and igst cannot both" in lowered
            or "invoice total does not match" in lowered
            or "taxable amount for '" in lowered
            or "tax amount for '" in lowered
            or "line-item taxable amounts" in lowered
            or "line-item tax amounts" in lowered
            or "no line items could be extracted" in lowered
            or "no line items were extracted" in lowered
            or ("gstin" in lowered and "15-character format" in lowered)
        ):
            continue
        if line_items_confirmed and (
            "line item" in lowered or "line-item" in lowered
        ):
            continue
        retained = False
        for label, (field_name, value) in missing_field_names.items():
            if label in lowered and ("could not be extracted" in lowered or "is invalid" in lowered):
                if field_name not in confirmed_fields and (not value or field_name == "invoice_date"):
                    issues.append(str(previous))
                retained = True
                break
        if not retained and previous not in issues:
            issues.append(str(previous))
    return issues


def invoice_duplicate_exists(user, invoice_type, invoice_number, gstin, exclude_pk=None):
    """Check for duplicates using the GSTIN identities enforced by the database."""
    gstin = normalize_gstin(gstin)
    invoice_number = normalize_invoice_number(invoice_number)

    invoices = Invoice.objects.filter(
        user=user,
        invoice_type=invoice_type,
    ).annotate(
        normalized_invoice_number=normalized_identity("invoice_number"),
    ).filter(normalized_invoice_number=invoice_number.lower())
    if exclude_pk is not None:
        invoices = invoices.exclude(pk=exclude_pk)

    if invoice_type == "sales":
        invoices = invoices.annotate(
            normalized_counterparty_gstin=normalized_identity("buyer_gstin"),
        )
        return invoices.filter(
            normalized_counterparty_gstin=gstin.lower(),
        ).exists()

    invoices = invoices.annotate(
        normalized_counterparty_gstin=normalized_identity("supplier_gstin"),
    )
    return invoices.filter(
        normalized_counterparty_gstin=gstin.lower(),
    ).exists()


def potential_name_duplicate_exists(user, invoice_type, invoice_number, name):
    """Identify likely duplicates by name without rejecting the upload."""
    if not name:
        return False

    invoice_number = normalize_invoice_number(invoice_number).lower()
    if invoice_type == "sales":
        name_field = "buyer_name"
    else:
        name_field = "supplier_name"

    candidates = Invoice.objects.filter(
        user=user,
        invoice_type=invoice_type,
    ).annotate(
        normalized_invoice_number=normalized_identity("invoice_number"),
    ).filter(
        normalized_invoice_number=invoice_number
    ).values_list(name_field, flat=True)

    normalized_name = normalize_party_name(name)
    return any(
        normalize_party_name(candidate) == normalized_name
        for candidate in candidates
    )


@login_required
def invoice_list_view(request):
    form = InvoiceListFilterForm(request.GET)
    from .date_evidence import with_invoice_dates
    invoices = with_invoice_dates(Invoice.objects.filter(user=request.user))
    if form.is_valid():
        data = form.cleaned_data
        if data['q']:
            invoices = invoices.filter(Q(invoice_number__icontains=data['q']) | Q(supplier_name__icontains=data['q']) | Q(buyer_name__icontains=data['q']))
        if data['type']:
            invoices = invoices.filter(invoice_type=data['type'])
        if data['status']:
            invoices = invoices.filter(status=data['status'])
        ordering = {'oldest': 'created_at', 'number': 'invoice_number', 'amount': '-total_amount'}.get(data['sort'], '-created_at')
        invoices = invoices.order_by(ordering, '-pk')
    else:
        invoices = invoices.none()
    page = Paginator(invoices, 25).get_page(request.GET.get('page'))
    return render(request, 'invoices/invoice_list.html', {'invoices': page.object_list, 'page': page, 'filter_form': form})


@login_required
@require_http_methods(["GET", "POST"])
def invoice_upload_view(request):
    if request.method == "GET":
        return render(request, "invoices/invoice_upload.html")

    uploaded_files = request.FILES.getlist("source_files") or request.FILES.getlist("source_file")
    invoice_type = request.POST.get("invoice_type", "purchase")

    if invoice_type not in {"sales", "purchase", "expense"}:
        messages.error(request, "Please select a valid invoice type.")
        return render(request, "invoices/invoice_upload.html")

    if not uploaded_files:
        messages.error(request, "Please select one or more PDF or image files.")
        return render(request, "invoices/invoice_upload.html")
    business = BusinessProfile.objects.filter(
        user=request.user
    ).first()

    if not business:
        messages.error(
            request,
            "Please complete your business profile before uploading invoices.",
        )
        return redirect("profile")
    if len(uploaded_files) > settings.INVOICE_BATCH_MAX_FILES:
        messages.error(request, "Upload at most 20 files per batch.")
        return render(request, "invoices/invoice_upload.html", status=400)
    batch = InvoiceBatch.objects.create(user=request.user, invoice_type=invoice_type, file_count=len(uploaded_files))
    created, failed, warnings = [], [], []
    for uploaded_file in uploaded_files:
        try:
            invoice, file_warnings = _process_invoice_file(request, uploaded_file, invoice_type, business, batch)
            if invoice:
                created.append(invoice)
                warnings.extend(file_warnings)
        except OCRUnavailableError as exc:
            failed.append({"filename": uploaded_file.name, "error": str(exc)})
        except InvoiceFileIssue as exc:
            failed.append({"filename": uploaded_file.name, "error": str(exc)})
        except (IntegrityError, ValueError, OSError, ProtectedError) as exc:
            failed.append({"filename": uploaded_file.name, "error": "Could not read or save this file. Check its file type and invoice values."})
        except Exception as exc:
            logger.error("Unexpected invoice batch processing failure (%s)", type(exc).__name__)
            failed.append({"filename": uploaded_file.name, "error": "An unexpected processing error occurred. The rest of the batch was still processed."})
    batch.failed_files = failed
    batch.save(update_fields=["failed_files"])
    if created:
        messages.success(request, f"{len(created)} of {len(uploaded_files)} invoice file(s) added to review.")
    if failed:
        messages.warning(request, f"{len(failed)} file(s) could not be processed. Open this batch to see the reasons.")
    if warnings:
        messages.warning(request, "An invoice may match an existing record with the same party name and invoice number. Review before verification.")
    if len(uploaded_files) == 1 and not created:
        messages.error(request, failed[0]["error"] if failed else "This invoice could not be processed.")
        return render(request, "invoices/invoice_upload.html", {"batch": batch})
    if len(uploaded_files) == 1 and created:
        return redirect("invoice_list")
    return redirect("invoice_batch_detail", batch_id=batch.pk)


def _process_invoice_file(request, uploaded_file, invoice_type, business, batch):
    from .extraction_services import process_upload
    return process_upload(request=request,upload=uploaded_file,invoice_type=invoice_type,business=business,batch=batch,extractor=extract_invoice_data)


@login_required
@require_POST
@transaction.atomic
def invoice_delete_view(request, invoice_id):
    invoice = get_object_or_404(
        Invoice.objects.select_for_update(),
        pk=invoice_id,
        user=request.user,
    )

    if invoice.status == "processing" or invoice.extraction_attempts.filter(status__in=['uploaded','processing']).exists():
        messages.error(request, "Extraction is still processing. Wait before cancelling this provisional invoice.")
        return redirect("invoice_detail",invoice_id=invoice.pk)
    if invoice.invoice_type=="expense":
        from expenses.views import expense_cancel_view
        return expense_cancel_view(request,invoice.pk)
    from .adjustment_services import adjustments_available
    if (adjustments_available() and invoice.adjustment_notes.filter(finalized_at__isnull=False).exists()) or invoice.status == "paid" or invoice.payments.exists() or invoice.reconciliation_records.exists():
        messages.error(request, "This invoice has financial history and cannot be cancelled.")
    elif invoice.status != "cancelled":
        previous = invoice.status
        invoice.status = "cancelled"
        if invoice.entry_mode == "manual":
            from django.utils import timezone
            invoice.document_state, invoice.cancelled_at = "cancelled", timezone.now()
            invoice.save(update_fields=["status", "document_state", "cancelled_at", "updated_at"])
        else:
            invoice.save(update_fields=["status", "updated_at"])
        record_event(request=request, action="cancel", module="invoices", description="Invoice cancelled; original document retained.", obj=invoice, before={"status": previous}, after={"status": "cancelled"}, metadata={'reason':request.POST.get('reason','').strip()[:500]})
        messages.success(request, "Invoice cancelled. Its record and source document are retained.")
    return redirect("invoice_list")


@login_required
def invoice_detail_view(request, invoice_id):
    invoice = get_object_or_404(
        Invoice.objects.select_related(
            "supplier_party",
            "buyer_party",
        ).prefetch_related("line_items"),
        pk=invoice_id,
        user=request.user,
    )
    if invoice.entry_mode == "manual":
        from .ledger import payment_total, invoice_outstanding
        from .email_views import invoice_email_context
        from .commercial_services import commercial_available, invoice_differences
        from .models import CommercialDocument
        source_document = CommercialDocument.objects.filter(converted_invoice=invoice, user=request.user).first() if commercial_available() else None
        return render(request, "invoices/manual_detail.html", {"invoice": invoice, "paid": payment_total(invoice), "outstanding": invoice_outstanding(invoice),
            "source_document": source_document,
            "conversion_changes": invoice_differences(source_document, invoice) if source_document else [],
            **invoice_email_context(invoice),
            "activity": AuditEvent.objects.filter(business__user=request.user, object_type="invoices.invoice", object_id=str(invoice.pk))[:50]})
    from .extraction_services import latest_attempt
    extraction=latest_attempt(invoice)
    if extraction:
        from .extraction_services import extraction_notice_context
        reviews=invoice.extraction_reviews.select_related('user')
        return render(request,'invoices/extraction_detail.html',{'invoice':invoice,'attempt':extraction,'reviews':reviews[:20],'attempts':invoice.extraction_attempts.all()[:20],
            **extraction_notice_context(invoice,extraction,reviews.filter(attempt=extraction).first())})
    validation_errors = _invoice_validation_issues(
        invoice, previous_issues=invoice.validation_errors or (),
    )
    source_extension = Path(invoice.source_file.name).suffix.lower() if invoice.source_file else ""

    return render(
        request,
        "invoices/invoice_detail.html",
        {
            "invoice": invoice,
            "validation_errors": validation_errors,
            "source_is_pdf": source_extension == ".pdf",
            "source_is_image": source_extension in {".png", ".jpg", ".jpeg"},
            "activity": AuditEvent.objects.filter(business__user=request.user).filter(Q(object_type="invoices.invoice", object_id=str(invoice.pk)) | Q(related_object_type="invoices.invoice", related_object_id=str(invoice.pk))).select_related("user")[:50],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def invoice_edit_view(request, invoice_id):
    BusinessProfile.objects.select_for_update().filter(user=request.user).first()
    invoice = get_object_or_404(Invoice.objects.select_for_update(), pk=invoice_id, user=request.user)
    if invoice.entry_mode == "manual":
        return redirect("invoice_draft_edit", invoice_id=invoice.pk)
    if invoice.status != "review":
        messages.error(request, "Only invoices awaiting review can be edited.")
        return redirect("invoice_detail", invoice_id=invoice.pk)

    from .extraction_services import latest_attempt
    attempt=latest_attempt(invoice)
    if attempt:return redirect('extraction_review',attempt_id=attempt.pk)
    before_values = {field: getattr(invoice, field) for field in ("invoice_number", "invoice_date", "due_date", "supplier_name", "supplier_gstin", "buyer_name", "buyer_gstin", "taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "other_charges", "total_amount", "status")}
    if request.method == "POST":
        form = InvoiceEditForm(request.POST, instance=invoice)
        formset = InvoiceLineItemFormSet(request.POST, instance=invoice, prefix="items")
        if form.is_valid() and formset.is_valid():
            previous_validation_errors = list(invoice.validation_errors or [])
            line_items_confirmed = any(item_form.has_changed() for item_form in formset.forms)
            before_values["line_items"] = list(invoice.line_items.order_by("pk").values("description", "quantity", "rate", "tax_rate", "taxable_amount", "tax_amount", "total_amount"))
            edited = form.save(commit=False)
            counterparty_gstin = edited.buyer_gstin if edited.invoice_type == "sales" else edited.supplier_gstin
            from .manual_services import number_exists
            sales_collision = edited.invoice_type == "sales" and number_exists(request.user, edited.invoice_number, invoice.pk)
            if sales_collision or (counterparty_gstin and invoice_duplicate_exists(
                request.user, edited.invoice_type, edited.invoice_number,
                counterparty_gstin, exclude_pk=invoice.pk,
            )):
                form.add_error("invoice_number", "An invoice with this invoice number and GSTIN already exists.")
            else:
                try:
                    with transaction.atomic():
                        edited.full_clean()
                        edited.save()
                        formset.instance = edited
                        formset.save()
                        edited.validation_errors = _invoice_validation_issues(
                            edited,
                            previous_issues=previous_validation_errors,
                            confirmed_fields=form.changed_data,
                            line_items_confirmed=line_items_confirmed,
                        )
                        edited.save(update_fields=["validation_errors", "updated_at"])
                        after_values = {field: getattr(edited, field) for field in before_values}
                        after_values["line_items"] = list(edited.line_items.order_by("pk").values("description", "quantity", "rate", "tax_rate", "taxable_amount", "tax_amount", "total_amount"))
                        if before_values != after_values:
                            record_event(request=request, user=request.user, action="update", module="invoices", description=f"Invoice {edited.invoice_number} updated.", obj=edited, before=before_values, after=after_values)
                except (IntegrityError, ValidationError):
                    form.add_error(None, "These invoice details conflict with an existing invoice or are invalid.")
                else:
                    messages.success(request, "Invoice changes saved.")
                    return redirect("invoice_detail", invoice_id=invoice.pk)
    else:
        form = InvoiceEditForm(instance=invoice)
        formset = InvoiceLineItemFormSet(instance=invoice, prefix="items")

    return render(request, "invoices/invoice_edit.html", {
        "invoice": invoice, "form": form, "formset": formset,
    })


@login_required
@require_POST
@transaction.atomic
def invoice_verify_view(request, invoice_id):
    invoice = get_object_or_404(Invoice, pk=invoice_id, user=request.user)
    if invoice.entry_mode == "manual":
        messages.error(request, "Use the draft finalization action for manual invoices.")
        return redirect("invoice_detail", invoice_id=invoice.pk)
    from .extraction_services import latest_attempt,verify_review
    if latest_attempt(invoice):
        try:verify_review(request,invoice);messages.success(request,'Reviewed invoice verified.')
        except ValidationError as exc:messages.error(request,' '.join(exc.messages))
        return redirect('invoice_detail',invoice_id=invoice.pk)
    current_issues = _invoice_validation_issues(
        invoice, previous_issues=invoice.validation_errors or (),
    )
    if invoice.status != "review":
        messages.error(request, "Only invoices awaiting review can be verified.")
    elif (
        current_issues
        or invoice.total_mismatch
        or any(
            item.taxable_amount_mismatch
            or item.tax_amount_mismatch
            or item.total_amount != item.calculated_line_total
            for item in invoice.line_items.all()
        )
    ):
        if current_issues != invoice.validation_errors:
            invoice.validation_errors = current_issues
            invoice.save(update_fields=["validation_errors", "updated_at"])
        messages.error(
            request,
            "Invoice validation issues must be resolved before it can be verified.",
        )
    else:
        invoice.status = "verified"
        with transaction.atomic():
            invoice.save(update_fields=["status", "updated_at"])
            record_event(request=request, user=request.user, action="verify", module="invoices", description=f"Invoice {invoice.invoice_number} verified.", obj=invoice, before={"status": "review"}, after={"status": "verified"})
        messages.success(request, "Invoice verified.")
    return redirect("invoice_detail", invoice_id=invoice.pk)


@login_required
@xframe_options_sameorigin
def invoice_source_view(request, invoice_id):
    invoice = get_object_or_404(Invoice, pk=invoice_id, user=request.user)
    if not invoice.source_file:
        raise Http404("Source document unavailable.")
    try:
        handle = invoice.source_file.open("rb")
    except (OSError, ValueError):
        raise Http404("Source document unavailable.") from None
    suffix = Path(invoice.source_file.name).suffix.lower()
    content_type = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(suffix, "application/octet-stream")
    return FileResponse(handle, as_attachment=request.GET.get("download") == "1" or content_type == "application/octet-stream", filename="invoice-source" + suffix, content_type=content_type)
