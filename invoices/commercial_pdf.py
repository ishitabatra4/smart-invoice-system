"""Presentation adapter into Phase 5; never creates an accounting Invoice record."""
import hashlib
import json
from django.utils import timezone
from .models import Invoice, InvoiceLineItem
from .pdf_generator import invoice_pdf_data, render_invoice_pdf, invoice_pdf_filename, MAX_LINES, PDFGenerationError

NOTICE = "Non-accounting commercial document. Not a tax invoice or payment demand. GST amounts are estimates, not recognized tax liabilities."


def commercial_pdf_data(document):
    lines = list(document.line_items.all()[:MAX_LINES + 1])
    if len(lines) > MAX_LINES:
        raise PDFGenerationError("Document exceeds the PDF item limit.")
    names = ("pricing_mode", "supply_category", "supplier_state_code", "place_of_supply", "calculation_version",
        "invoice_discount_type", "invoice_discount_value", "invoice_discount_amount", "total_discount_amount",
        "taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "other_charges", "total_amount", "treatment_breakdown",
        "business_snapshot", "party_snapshot", "payment_terms", "notes", "terms_conditions")
    adapter = Invoice(pk=document.pk or 0, user_id=document.user_id, invoice_type="sales", entry_mode="manual",
        document_state="draft" if document.status == "draft" else "finalized", status="verified",
        invoice_number=document.number, invoice_date=document.document_date, due_date=document.valid_until,
        **{key: getattr(document, key) for key in names})
    fields = [field.name for field in InvoiceLineItem._meta.fields if field.name not in {"id", "invoice", "created_at"}]
    adapter._prefetched_objects_cache = {"payments": [], "line_items": [InvoiceLineItem(invoice=adapter, **{key: getattr(line, key) for key in fields}) for line in lines]}
    data = invoice_pdf_data(adapter)
    data.update(commercial=True, title=document.get_kind_display().upper(), type=document.get_kind_display(),
                state="DRAFT" if document.status == "draft" else document.effective_status.upper(), commercial_notice=NOTICE)
    if document.issued_at:
        data["issued"] = timezone.localtime(document.issued_at).date().isoformat()
    if document.effective_status == "expired":
        data["notices"].append("This commercial document has expired under its saved validity date.")
    data.pop("payment")
    data.pop("revision")
    data["revision"] = hashlib.sha256(json.dumps(data, default=str, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return data


def generate_commercial_pdf(document):
    return render_invoice_pdf(commercial_pdf_data(document))


def commercial_pdf_filename(document):
    return invoice_pdf_filename(document)
