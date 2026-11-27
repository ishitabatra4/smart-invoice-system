from datetime import date
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, DatabaseError, transaction
from django.utils import timezone

from accounts.models import BusinessProfile, BusinessParty
from audit.services import record_event
from .models import CommercialDocument, CommercialLineItem, CommercialNumberSequence
from .manual_services import snapshot, year_start, save_draft
from .manual_calculations import calculate_invoice

INPUT_FIELDS = ("description", "hsn_sac_code", "quantity", "unit", "rate", "discount_type", "tax_rate", "tax_treatment")
LINE_AMOUNTS = ("discount", "taxable_amount", "tax_amount", "total_amount", "invoice_discount_allocated", "cgst_amount", "sgst_amount", "igst_amount")
TOTAL_FIELDS = ("taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "other_charges", "total_amount", "invoice_discount_amount", "total_discount_amount")
HEADER_FIELDS = ("pricing_mode", "supply_category", "invoice_discount_type", "invoice_discount_value", "supplier_state_code", "place_of_supply", "payment_terms", "notes", "terms_conditions")


def commercial_available():
    try:
        return {"invoices_commercialdocument", "invoices_commerciallineitem", "invoices_commercialnumbersequence", "invoices_commercialemaildelivery"}.issubset(connection.introspection.table_names())
    except DatabaseError:
        return False


def amounts(data, rows):
    return calculate_invoice(rows, data["supplier_state_code"], data["place_of_supply"],
        pricing_mode=data["pricing_mode"], supply_category=data["supply_category"],
        invoice_discount_type=data["invoice_discount_type"], invoice_discount=data["invoice_discount_value"],
        adjustment=data["adjustment"], allowed_rates=settings.GST_ALLOWED_RATES)


def stored_inputs(document):
    return [{**{key: getattr(line, key) for key in INPUT_FIELDS}, "discount": line.discount_value} for line in document.line_items.all()]


def financial_values(document):
    return {"status": document.status, "number": document.number,
        **{key: getattr(document, key) for key in TOTAL_FIELDS},
        "line_items": list(document.line_items.values(*INPUT_FIELDS, "discount_value", *LINE_AMOUNTS))}


def _locked(user, document_id):
    business = BusinessProfile.objects.select_for_update().get(user=user)
    document = CommercialDocument.objects.select_for_update().get(pk=document_id, user=user, business=business)
    return business, document


def _number(business, kind, day):
    start = year_start(business, day)
    sequence, _ = CommercialNumberSequence.objects.select_for_update().get_or_create(business=business, kind=kind, year_start=start)
    prefix = "QT" if kind == "quotation" else "PF"
    while sequence.next_value <= 999999:
        value = sequence.next_value
        sequence.next_value += 1
        number = f"{prefix}/{start.year % 100:02}{(start.year + 1) % 100:02}/{value:06}"
        if not CommercialDocument.objects.filter(business=business, kind=kind, number=number).exists():
            sequence.save(update_fields=["next_value"])
            return number, start
    raise ValidationError("This commercial numbering series is exhausted.")


@transaction.atomic
def save_commercial(*, request, kind, data, line_data, creation_key=None, document_id=None):
    if kind not in dict(CommercialDocument.KINDS):
        raise ValidationError("Unknown document type.")
    business = BusinessProfile.objects.select_for_update().get(user=request.user)
    if document_id:
        document = CommercialDocument.objects.select_for_update().get(pk=document_id, user=request.user, business=business)
        if document.status != "draft" or document.kind != kind:
            raise ValidationError("Only a draft of this type can be edited.")
        before = financial_values(document)
    else:
        if not creation_key:
            raise ValidationError("A valid creation confirmation is required.")
        existing = CommercialDocument.objects.filter(creation_key=creation_key).first()
        if existing:
            if existing.user_id != request.user.pk or existing.kind != kind:
                raise ValidationError("This form has already been used.")
            return existing
        number, start = _number(business, kind, data["invoice_date"])
        document = CommercialDocument(user=request.user, business=business, kind=kind, number=number, year_start=start, creation_key=creation_key)
        before = {}
    customer = BusinessParty.objects.get(pk=data["party"].pk, business=business, is_active=True, party_type__in=["customer", "both"])
    if not document.year_start <= data["invoice_date"] < date(document.year_start.year + 1, document.year_start.month, 1):
        raise ValidationError("A numbered draft must stay in its original financial year. Cancel and create a new draft for another year.")
    if data["due_date"] is None or data["due_date"] < data["invoice_date"]:
        raise ValidationError("Validity must be on or after the document date.")
    if business.gstin and business.gstin[:2] != data["supplier_state_code"]:
        raise ValidationError("Supplier state must match the business GSTIN.")
    result = amounts(data, line_data)
    document.customer = customer
    document.document_date, document.valid_until = data["invoice_date"], data["due_date"]
    for key in HEADER_FIELDS:
        setattr(document, key, data.get(key) if data.get(key) is not None else "")
    for key in TOTAL_FIELDS:
        setattr(document, key, result[key])
    document.subtotal = result["subtotal"]
    document.treatment_breakdown = {key: str(value) for key, value in result["treatment_breakdown"].items()}
    document.business_snapshot, document.party_snapshot = snapshot(business, True), snapshot(customer)
    document.full_clean()
    document.save()
    document.line_items.all().delete()
    for row, calculated in zip(line_data, result["lines"]):
        line = CommercialLineItem(document=document, **{key: row[key] for key in INPUT_FIELDS},
                                  discount_value=row["discount"], **{key: calculated[key] for key in LINE_AMOUNTS})
        line.full_clean()
        line.save()
    record_event(request=request, business=business, action="update" if before else "create", module="commercial",
        description="Commercial draft saved.", obj=document, before=before, after=financial_values(document))
    return document


@transaction.atomic
def transition_commercial(*, request, document_id, action, notes=""):
    business, document = _locked(request.user, document_id)
    before = {"status": document.status}
    if action == "cancel":
        if document.status in {"cancelled", "converted"}:
            raise ValidationError("This document cannot be cancelled.")
        document.status = "cancelled"
    elif action == "issue":
        if document.status != "draft" or document.effective_status == "expired":
            raise ValidationError("Only an unexpired draft can be explicitly issued.")
        customer = document.customer
        if not customer.is_active or customer.party_type not in {"customer", "both"} or customer.business_id != business.pk:
            raise ValidationError("Review the active customer before issuance.")
        if document.business_snapshot != snapshot(business, True) or document.party_snapshot != snapshot(customer):
            raise ValidationError("Business/customer details changed. Review and save the draft first.")
        data = {key: getattr(document, key) for key in HEADER_FIELDS}
        data["adjustment"] = document.other_charges
        result = amounts(data, stored_inputs(document))
        if any(getattr(document, key) != result[key] for key in TOTAL_FIELDS) or document.treatment_breakdown != {key: str(value) for key, value in result["treatment_breakdown"].items()}:
            raise ValidationError("Saved totals differ. Review and save the draft first.")
        for line, calculated in zip(document.line_items.all(), result["lines"]):
            if any(getattr(line, key) != calculated[key] for key in LINE_AMOUNTS):
                raise ValidationError("Saved line values differ. Review and save first.")
        document.status, document.issued_at = "sent", timezone.now()
    elif action in {"accept", "reject"}:
        if document.effective_status != "sent":
            raise ValidationError("Only an unexpired issued document can receive a decision.")
        if len(notes) > 2000:
            raise ValidationError("Decision notes are limited to 2000 characters.")
        document.status = "accepted" if action == "accept" else "rejected"
        document.decision_at, document.decision_by, document.decision_notes = timezone.now(), request.user, notes
    else:
        raise ValidationError("Unknown transition.")
    document.full_clean()
    document.save()
    record_event(request=request, business=business, action="cancel" if action == "cancel" else "update", module="commercial",
        description="Commercial document " + {"issue": "explicitly issued (not an email acceptance claim)", "accept": "customer acceptance recorded by business user", "reject": "customer rejection recorded by business user", "cancel": "cancelled"}[action] + ".",
        obj=document, before=before, after={"status": document.status})
    return document


@transaction.atomic
def convert_commercial(*, request, document_id, invoice_date):
    business, document = _locked(request.user, document_id)
    if document.converted_invoice_id:
        return document.converted_invoice
    if document.effective_status != "accepted":
        raise ValidationError("Only an accepted, unexpired document can be converted.")
    data = {key: getattr(document, key) for key in HEADER_FIELDS}
    data.update(party=document.customer, invoice_date=invoice_date, due_date=None, adjustment=document.other_charges)
    invoice = save_draft(request=request, business=business, invoice_type="sales", data=data,
        line_data=stored_inputs(document), creation_key=uuid.uuid5(document.creation_key, "commercial-conversion"))
    changes = invoice_differences(document, invoice)
    document.converted_invoice, document.status, document.conversion_changes = invoice, "converted", changes
    document.full_clean()
    document.save(update_fields=["converted_invoice", "status", "conversion_changes", "updated_at"])
    record_event(request=request, business=business, action="create", module="commercial", obj=document, related_obj=invoice,
        description="Commercial document converted to one sales invoice draft for review.", after={"status": "converted", "invoice_id": invoice.pk}, metadata={"changed_fields": [item["field"] for item in changes]})
    return invoice


def invoice_differences(document, invoice):
    """Read-only comparison also used after the converted draft is edited."""
    changes = []
    def compare(field, old, new):
        if old != new:
            changes.append({"field": field, "source": str(old), "invoice": str(new)})
    compare("Date", document.document_date, invoice.invoice_date)
    for key in TOTAL_FIELDS:
        compare(key, getattr(document, key), getattr(invoice, key))
    for label, old, new in (("Business", document.business_snapshot, invoice.business_snapshot), ("Customer", document.party_snapshot, invoice.party_snapshot)):
        for key in old.keys() | new.keys():
            compare(label + " " + key, old.get(key, ""), new.get(key, ""))
    source_lines, invoice_lines = list(document.line_items.all()), list(invoice.line_items.all())
    compare("Item count", len(source_lines), len(invoice_lines))
    for index, (old, new) in enumerate(zip(source_lines, invoice_lines), 1):
        for key in (*INPUT_FIELDS, "discount_value", *LINE_AMOUNTS):
            compare(f"Line {index} {key}", getattr(old, key), getattr(new, key))
    return changes
