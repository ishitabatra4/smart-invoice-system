import uuid
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST
from accounts.models import BusinessProfile
from accounts.forms import BusinessPartyForm
from .manual_forms import ManualInvoiceForm, ManualLineFormSet, creation_key_from_token
from .manual_services import save_draft, finalize_invoice
from .models import Invoice


@login_required
@require_http_methods(["GET", "POST"])
def manual_invoice_view(request, invoice_type=None, invoice_id=None):
    business = BusinessProfile.objects.filter(user=request.user).first()
    if not business:
        messages.error(request, "Complete your business profile first.")
        return redirect("profile")
    invoice = get_object_or_404(Invoice, pk=invoice_id, user=request.user, entry_mode="manual") if invoice_id else None
    invoice_type = invoice.invoice_type if invoice else invoice_type
    if invoice_type not in {"sales", "purchase"}:
        raise Http404
    if invoice and invoice.document_state != "draft":
        messages.error(request, "Finalized and cancelled invoices cannot be edited.")
        return redirect("invoice_detail", invoice_id=invoice.pk)
    data = request.POST if request.method == "POST" else None
    form = ManualInvoiceForm(data, business=business, invoice_type=invoice_type, invoice=invoice)
    if not invoice and request.method == "GET":
        form.initial["creation_token"] = signing.dumps({"user": request.user.pk, "type": invoice_type, "key": str(uuid.uuid4())}, salt="invoices.create")
    party_form = BusinessPartyForm(data if data and data.get("add_new_party") else None, business=business, prefix="newparty", initial={"party_type": "customer" if invoice_type == "sales" else "vendor"})
    party_form.fields["party_type"].disabled = True
    initial = list(invoice.line_items.values("description", "hsn_sac_code", "quantity", "unit", "rate", "discount", "discount_type", "discount_value", "tax_treatment", "tax_rate")) if invoice else None
    if initial:
        for row in initial:
            row["discount"] = row["discount_value"] if row["discount_value"] is not None else row["discount"]
            row["discount_type"] = row["discount_type"] or "fixed"
            row["tax_treatment"] = row["tax_treatment"] or "taxable"
            row["tax_rate"] = format(row["tax_rate"].normalize(), "f")
    formset = ManualLineFormSet(data, initial=initial, prefix="items", form_kwargs={"pricing_mode": data.get("pricing_mode", "exclusive") if data else (invoice.pricing_mode if invoice and invoice.pricing_mode else "exclusive")})
    if request.method == "POST":
        if not invoice:
            try:
                key = creation_key_from_token(request.POST.get("creation_token"), request.user.pk, invoice_type)
            except ValidationError:
                pass
            else:
                existing = Invoice.objects.filter(user=request.user, invoice_type=invoice_type, creation_key=key).first()
                if existing:
                    return redirect("invoice_detail", invoice_id=existing.pk)
        form_valid, rows_valid = form.is_valid(), formset.is_valid()
        party_valid = party_form.is_valid() if form.cleaned_data.get("add_new_party") else True
        if form_valid and rows_valid and party_valid:
            try:
                invoice = save_draft(request=request, business=business, invoice_type=invoice_type,
                    data=form.cleaned_data, line_data=[row for row in formset.cleaned_data if row and not row.get("DELETE")],
                    creation_key=getattr(form, "creation_key", None), invoice_id=invoice.pk if invoice else None,
                    new_party_form=party_form if form.cleaned_data.get("add_new_party") else None)
            except (ValidationError, IntegrityError) as error:
                form.add_error(None, " ".join(error.messages) if isinstance(error, ValidationError) else "These invoice details conflict with an existing record. Reload and try again.")
            else:
                messages.success(request, "Draft saved. Review the preview before finalizing.")
                return redirect("invoice_detail", invoice_id=invoice.pk)
    return render(request, "invoices/manual_form.html", {"form": form, "formset": formset, "invoice": invoice, "invoice_type": invoice_type, "party_form": party_form})


@login_required
@require_POST
def manual_finalize_view(request, invoice_id):
    get_object_or_404(Invoice, pk=invoice_id, user=request.user, entry_mode="manual")
    if not BusinessProfile.objects.filter(user=request.user).exists():
        return redirect("profile")
    try:
        finalize_invoice(request=request, invoice_id=invoice_id)
    except (ValidationError, IntegrityError) as error:
        messages.error(request, " ".join(error.messages) if isinstance(error, ValidationError) else "Finalization conflicts with an existing invoice.")
    else:
        messages.success(request, "Invoice finalized and available in the ledger.")
    return redirect("invoice_detail", invoice_id=invoice_id)


@login_required
def manual_preview_view(request, invoice_id):
    invoice = get_object_or_404(Invoice.objects.prefetch_related("line_items"), pk=invoice_id, user=request.user, entry_mode="manual")
    from .ledger import payment_total, invoice_outstanding
    return render(request, "invoices/manual_preview.html", {"invoice": invoice, "paid": payment_total(invoice), "outstanding": invoice_outstanding(invoice)})


@login_required
@require_POST
def calculation_preview_view(request, invoice_type):
    from django.conf import settings
    from django.http import JsonResponse
    from .manual_calculations import calculate_invoice
    if invoice_type not in {"sales","purchase"}:
        raise Http404
    business = get_object_or_404(BusinessProfile,user=request.user)
    invoice = None
    if request.POST.get("invoice_id"):
        raw = request.POST["invoice_id"]
        if not raw.isdecimal() or len(raw)>18:
            raise Http404
        invoice=get_object_or_404(Invoice,pk=int(raw),user=request.user,entry_mode="manual",document_state="draft",invoice_type=invoice_type)
    form=ManualInvoiceForm(request.POST,business=business,invoice_type=invoice_type,invoice=invoice)
    rows=ManualLineFormSet(request.POST,prefix="items",form_kwargs={"pricing_mode":request.POST.get("pricing_mode","exclusive")})
    valid_form,valid_rows=form.is_valid(),rows.is_valid()
    if not valid_form or not valid_rows:
        return JsonResponse({"errors":{"invoice":form.errors,"lines":rows.errors,"formset":rows.non_form_errors()}},status=400)
    try:
        result=calculate_invoice([row for row in rows.cleaned_data if row and not row.get("DELETE")],
            form.cleaned_data["supplier_state_code"],form.cleaned_data["place_of_supply"],
            pricing_mode=form.cleaned_data["pricing_mode"],supply_category=form.cleaned_data["supply_category"],
            invoice_discount_type=form.cleaned_data["invoice_discount_type"],invoice_discount=form.cleaned_data["invoice_discount_value"],
            adjustment=form.cleaned_data["adjustment"],allowed_rates=settings.GST_ALLOWED_RATES)
    except ValidationError as error:
        return JsonResponse({"errors":error.messages},status=400)
    return JsonResponse(result)
