from .identity import normalized_identity
from datetime import date
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Lower, Trim
from django.db.models import Q
from django.utils import timezone
from accounts.models import BusinessProfile
from audit.services import record_event
from .manual_calculations import calculate_invoice
from django.conf import settings
from .models import Invoice, InvoiceLineItem, InvoiceNumberSequence, NORMALIZATION_WHITESPACE
from .gstin import validate_document_gstins,validate_registration_state

SNAPSHOT_FIELDS = ("name", "legal_name", "gstin", "pan", "email", "phone", "address_line", "city", "state", "pincode")
HEADER_FIELDS = ("pricing_mode", "supply_category", "invoice_discount_type", "invoice_discount_value", "invoice_date", "due_date", "supplier_state_code", "place_of_supply", "payment_terms", "notes", "terms_conditions")


def snapshot(record, business=False):
    result = {name: getattr(record, name, "") or "" for name in SNAPSHOT_FIELDS}
    if business:
        result["name"] = record.business_name
    return result


def number_exists(user, number, exclude=None):
    return Invoice.objects.filter(user=user, invoice_type="sales").exclude(pk=exclude).annotate(
        key=normalized_identity("invoice_number")
    ).filter(key=number.strip().lower()).exists()


def year_start(business, invoice_date):
    month = business.financial_year_start_month
    if not 1 <= month <= 12:
        raise ValidationError("Correct your business financial-year start month before creating invoices.")
    return date(invoice_date.year if invoice_date.month >= month else invoice_date.year - 1, month, 1)


def allocate_number(business, invoice_date):
    start = year_start(business, invoice_date)
    sequence, _ = InvoiceNumberSequence.objects.select_for_update().get_or_create(business=business, year_start=start)
    while sequence.next_value <= 999999:
        value = sequence.next_value
        sequence.next_value += 1
        number = f"SI/{start.year % 100:02}{(start.year + 1) % 100:02}/{value:06}"
        if not number_exists(business.user, number):
            sequence.save(update_fields=["next_value"])
            return number, start
    raise ValidationError("This numbering series is exhausted. Contact your administrator.")


def audit_values(invoice):
    return {"invoice_number": invoice.invoice_number, "document_state": invoice.document_state,
            "total_amount": invoice.total_amount, "taxable_amount": invoice.taxable_amount,
            "cgst_amount": invoice.cgst_amount, "sgst_amount": invoice.sgst_amount, "igst_amount": invoice.igst_amount,
            "party_id": invoice.buyer_party_id or invoice.supplier_party_id,
            "invoice_date": invoice.invoice_date, "due_date": invoice.due_date,
            "supplier_state_code": invoice.supplier_state_code, "place_of_supply": invoice.place_of_supply,
            "payment_terms": invoice.payment_terms, "round_off": invoice.other_charges,
            "terms_conditions": invoice.terms_conditions,
            "pricing_mode":invoice.pricing_mode, "supply_category":invoice.supply_category,
            "invoice_discount_type":invoice.invoice_discount_type,"invoice_discount_value":invoice.invoice_discount_value,
            "invoice_discount_amount":invoice.invoice_discount_amount,"total_discount_amount":invoice.total_discount_amount,
            "treatment_breakdown":invoice.treatment_breakdown,
            "line_items": list(invoice.line_items.values("description", "hsn_sac_code", "quantity", "unit", "rate", "discount", "discount_type", "discount_value", "tax_treatment", "invoice_discount_allocated", "cgst_amount", "sgst_amount", "igst_amount", "tax_rate", "taxable_amount", "tax_amount", "total_amount"))}


@transaction.atomic
def save_draft(*, request, business, invoice_type, data, line_data, creation_key=None, invoice_id=None, new_party_form=None):
    business = BusinessProfile.objects.select_for_update().get(pk=business.pk, user=request.user)
    if invoice_id:
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id, user=request.user, entry_mode="manual")
        if invoice.document_state != "draft" or invoice.status != "draft":
            raise ValidationError("Only draft invoices can be changed.")
        before = audit_values(invoice)
    else:
        existing = Invoice.objects.filter(creation_key=creation_key, user=request.user).first()
        if existing:
            return existing
        invoice = Invoice(user=request.user, invoice_type=invoice_type, entry_mode="manual", document_state="draft", status="draft", creation_key=creation_key)
        before = {}
    if invoice_type not in {"sales", "purchase"} or invoice.invoice_type != invoice_type:
        raise ValidationError("Unsupported invoice type.")
    if new_party_form is not None:
        party = new_party_form.save(commit=False)
        party.business = business
        party.party_type = "customer" if invoice_type == "sales" else "vendor"
        party.full_clean()
        party.save()
        record_event(request=request, business=business, action="create", module="parties", description="Party created from invoice draft.", obj=party, after={"name": party.name, "party_type": party.party_type})
    else:
        party = data["party"]
        party.refresh_from_db()
    seller_gstin = business.gstin if invoice_type == "sales" else party.gstin
    if seller_gstin and data["supplier_state_code"] != seller_gstin[:2]:
        raise ValidationError("Supplier state must match the supplier GSTIN.")
    valid_types = {"customer", "both"} if invoice_type == "sales" else {"vendor", "both"}
    if party.business_id != business.pk or not party.is_active or party.party_type not in valid_types:
        raise ValidationError("Choose an active party from this business.")
    for name in HEADER_FIELDS:
        value=data.get(name)
        setattr(invoice,name,value if value is not None else (None if name=="due_date" else ""))
    if invoice_type == "sales":
        if not invoice.pk:
            invoice.invoice_number, invoice.numbering_year_start = allocate_number(business, invoice.invoice_date)
        start = invoice.numbering_year_start
        if start and not start <= invoice.invoice_date < date(start.year + 1, start.month, 1):
            raise ValidationError("A numbered sales draft must stay within its original financial year. Cancel it and create a new draft for another year.")
        invoice.supplier_party = None
        invoice.buyer_party = party
        invoice.supplier_name, invoice.supplier_gstin = business.business_name, business.gstin or ""
        invoice.buyer_name, invoice.buyer_gstin = party.name, party.gstin
        if number_exists(request.user, invoice.invoice_number, invoice.pk):
            raise ValidationError("This sales invoice number already exists.")
    else:
        invoice.invoice_number = data["invoice_number"].strip()
        invoice.supplier_party, invoice.buyer_party = party, None
        invoice.supplier_name, invoice.supplier_gstin = party.name, party.gstin
        invoice.buyer_name, invoice.buyer_gstin = business.business_name, business.gstin or ""
        duplicates = Invoice.objects.filter(user=request.user, invoice_type="purchase").exclude(pk=invoice.pk).annotate(
            key=normalized_identity("invoice_number"), gst=normalized_identity("supplier_gstin"), party_name=Lower(Trim("supplier_name"))
        ).filter(key=invoice.invoice_number.lower())
        if (duplicates.filter(gst=party.gstin.strip().lower()).exists() if party.gstin.strip() else duplicates.filter(Q(supplier_party=party) | Q(gst="", party_name=party.name.strip().lower())).exists()):
            raise ValidationError("This supplier invoice number already exists.")
    rows = [{key:line[key] for key in ("description","hsn_sac_code","quantity","unit","rate","discount","tax_rate","discount_type","tax_treatment")} for line in line_data]
    result = calculate_invoice(rows,invoice.supplier_state_code,invoice.place_of_supply,
        pricing_mode=invoice.pricing_mode, supply_category=invoice.supply_category,
        invoice_discount_type=invoice.invoice_discount_type,invoice_discount=invoice.invoice_discount_value,
        adjustment=data["adjustment"],allowed_rates=settings.GST_ALLOWED_RATES)
    validate_document_gstins(invoice.supplier_gstin,invoice.buyer_gstin,invoice.supplier_state_code,result['tax_amount'])
    validate_registration_state(business.gstin,business.state,'Business profile')
    validate_registration_state(party.gstin,party.state,'Selected party')
    for row, calculated in zip(rows,result["lines"]):
        row["discount_value"] = row["discount"]
        row.update({key:calculated[key] for key in ("discount","taxable_amount","tax_amount","total_amount","invoice_discount_allocated","cgst_amount","sgst_amount","igst_amount")})
    for key in ("taxable_amount","cgst_amount","sgst_amount","igst_amount","other_charges","total_amount","invoice_discount_amount","total_discount_amount"):
        setattr(invoice,key,result[key])
    invoice.treatment_breakdown={key:str(value) for key,value in result["treatment_breakdown"].items()}
    invoice.calculation_version=1
    invoice.business_snapshot, invoice.party_snapshot = snapshot(business, True), snapshot(party)
    invoice.full_clean()
    invoice.save()
    invoice.line_items.all().delete()  # Only this owner's editable draft rows.
    for values in rows:
        line = InvoiceLineItem(invoice=invoice, **values)
        line.full_clean()
        line.save()
    record_event(request=request, business=business, action="update" if before else "create", module="invoices",
                 description="Manual invoice draft saved.", obj=invoice, before=before, after=audit_values(invoice))
    return invoice


@transaction.atomic
def finalize_invoice(*, request, invoice_id):
    business = BusinessProfile.objects.select_for_update().get(user=request.user)
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id, user=request.user, entry_mode="manual")
    if invoice.document_state != "draft" or invoice.status != "draft":
        raise ValidationError("Only a draft can be finalized.")
    party = invoice.buyer_party if invoice.invoice_type == "sales" else invoice.supplier_party
    valid_types = {"customer", "both"} if invoice.invoice_type == "sales" else {"vendor", "both"}
    if not party or party.business_id != business.pk or not party.is_active or party.party_type not in valid_types:
        raise ValidationError("Choose an active party from your business before finalizing.")
    if invoice.business_snapshot != snapshot(business, True) or invoice.party_snapshot != snapshot(party):
        raise ValidationError("Business or party details changed. Review and save this draft before finalizing.")
    lines = list(invoice.line_items.all())
    if not lines:
        raise ValidationError("Add at least one line item.")
    if invoice.calculation_version == 1:
        result = calculate_invoice([{"quantity":line.quantity,"rate":line.rate,"discount":line.discount_value,"discount_type":line.discount_type,"tax_rate":line.tax_rate,"tax_treatment":line.tax_treatment} for line in lines],
            invoice.supplier_state_code,invoice.place_of_supply,pricing_mode=invoice.pricing_mode,
            supply_category=invoice.supply_category,invoice_discount_type=invoice.invoice_discount_type,
            invoice_discount=invoice.invoice_discount_value,adjustment=invoice.other_charges,allowed_rates=settings.GST_ALLOWED_RATES)
        for line,values in zip(lines,result["lines"]):
            line.full_clean()
            if any(getattr(line,key)!=values[key] for key in ("discount","taxable_amount","tax_amount","total_amount","invoice_discount_allocated","cgst_amount","sgst_amount","igst_amount")):
                raise ValidationError("Line amounts changed. Review and save the draft before finalizing.")
        if any(getattr(invoice,key)!=result[key] for key in ("taxable_amount","cgst_amount","sgst_amount","igst_amount","other_charges","total_amount","invoice_discount_amount","total_discount_amount")) or invoice.treatment_breakdown != {key:str(value) for key,value in result["treatment_breakdown"].items()}:
            raise ValidationError("Invoice amounts do not reconcile. Review and save the draft.")
    else:
        raise ValidationError("This earlier draft needs GST review. Open Edit draft, confirm its pricing and supply details, then save before finalizing. Existing amounts were not changed.")
    if invoice.invoice_type == "sales" and number_exists(request.user, invoice.invoice_number, invoice.pk):
        raise ValidationError("This sales invoice number already exists.")
    validate_document_gstins(invoice.supplier_gstin,invoice.buyer_gstin,invoice.supplier_state_code,invoice.total_tax)
    validate_registration_state(business.gstin,business.state,'Business profile')
    validate_registration_state(party.gstin,party.state,'Selected party')
    before = audit_values(invoice)
    invoice.document_state, invoice.status, invoice.finalized_at = "finalized", "verified", timezone.now()
    invoice.full_clean()
    invoice.save(update_fields=["document_state", "status", "finalized_at", "updated_at"])
    record_event(request=request, business=business, action="verify", module="invoices", description="Sales invoice finalized." if invoice.invoice_type == "sales" else "Manual purchase invoice verified.", obj=invoice, before=before, after=audit_values(invoice))
    return invoice
