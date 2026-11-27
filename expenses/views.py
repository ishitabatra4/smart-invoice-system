from django.views.decorators.clickjacking import xframe_options_sameorigin
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, Prefetch, Count
from django.db.models.functions import Lower, Trim
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_POST

from accounts.models import BusinessParty, BusinessProfile
from audit.models import AuditEvent
from audit.services import record_event
from invoices.models import Invoice, InvoiceLineItem, InvoicePayment, NORMALIZATION_WHITESPACE
from invoices.payment_forms import InvoicePaymentForm
from invoices.ledger import invoice_outstanding, payment_balance_state

from .forms import ExpenseForm, RecurringExpenseForm, ExpenseMetadataForm
from .services import managed_invoices, itc_state, advanced_ready, save_metadata, remember_attachment
from .models import ExpenseCategory, ExpenseDetail, RecurringExpense, RecurringExpenseGeneration, private_expense_storage, ExpenseITCReview
from .recurrence import generate_due_expenses


DEFAULT_CATEGORIES = (
    "Rent", "Electricity", "Internet and Telephone", "Salaries", "Office Supplies",
    "Travel and Transportation", "Marketing and Advertising", "Software and Subscriptions",
    "Repairs and Maintenance", "Professional Services", "Bank Charges", "Insurance", "Miscellaneous",
)
ZERO = Decimal("0.00")
CONFIRMED = ("verified", "paid")


def _business(request):
    business = BusinessProfile.objects.filter(user=request.user).first()
    if not business:
        messages.error(request, "Set up your business profile before managing expenses.")
        return None
    return business


def _ensure_default_categories(business):
    for name in DEFAULT_CATEGORIES:
        if ExpenseCategory.objects.filter(business=business,name__iexact=name).exists():continue
        ExpenseCategory.objects.get_or_create(
            business=business,
            name=name,
            defaults={"is_default": True},
        )


def _expense_queryset(user):
    return managed_invoices(user).select_related(
        "supplier_party", "expense_detail__category"
    ).prefetch_related("payments", "line_items", "adjustment_notes")


def _payment_total(invoice):
    return sum((payment.amount for payment in invoice.payments.all() if payment.user_id == invoice.user_id), ZERO)


def _expense_state(invoice, paid):
    if invoice.status=="cancelled":return "cancelled"
    if invoice.status not in CONFIRMED:return "draft"
    return payment_balance_state(invoice)


def _expense_row(invoice, *, evidence_context=None):
    paid = _payment_total(invoice)
    outstanding = invoice_outstanding(invoice) if invoice.status in CONFIRMED else ZERO
    state = 'cancelled' if invoice.status == 'cancelled' else 'draft' if invoice.status not in CONFIRMED else 'paid' if outstanding == 0 else 'partial' if paid > 0 else 'unpaid' 
    return {
        "invoice": invoice,
        "detail": getattr(invoice, "expense_detail", None),
        "paid": paid,
        "outstanding": outstanding,
        "payment_state": state,
        "is_overdue": bool(
            invoice.status in CONFIRMED
            and invoice.due_date
            and invoice.due_date < timezone.localdate()
            and outstanding>0
        ),
        "gst": invoice.total_tax,
        "itc": itc_state(invoice, context=evidence_context),
    }


def _date_range(params, today, financial_year_start_month=4):
    preset = params.get("period", "this_month")
    if preset == "today":
        return today, today, preset, None
    if preset == "this_week":
        return today - timedelta(days=today.weekday()), today, preset, None
    if preset == "last_month":
        end = date(today.year, today.month, 1) - timedelta(days=1)
        return date(end.year, end.month, 1), end, preset, None
    if preset == "financial_year":
        start_year = today.year if today.month >= financial_year_start_month else today.year - 1
        start = date(start_year, financial_year_start_month, 1)
        return start, today, preset, None
    if preset == "custom":
        try:
            start = date.fromisoformat(params.get("start_date", ""))
            end = date.fromisoformat(params.get("end_date", ""))
            if start > end or (end - start).days > 3660 or end.year == 9999:
                raise ValueError
            return start, end, preset, None
        except (TypeError, ValueError):
            return today.replace(day=1), today, "this_month", "Enter a valid date range. Showing this month instead."
    return today.replace(day=1), today, "this_month", None


@login_required
def expense_dashboard_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    _ensure_default_categories(business)
    today = timezone.localdate()
    fy_month = business.financial_year_start_month or 4
    start, end, period, date_error = _date_range(request.GET, today, fy_month)
    expenses = list(_expense_queryset(request.user).exclude(status="cancelled"))
    rows = [_expense_row(item) for item in expenses]
    period_rows = [row for row in rows if start <= row["invoice"].invoice_date <= end]
    current_month_start = today.replace(day=1)
    fy_year = today.year if today.month >= fy_month else today.year - 1
    fy_start = date(fy_year, fy_month, 1)
    confirmed_rows = [row for row in rows if row["invoice"].status in CONFIRMED]
    monthly_rows = [row for row in confirmed_rows if current_month_start <= row["invoice"].invoice_date <= today]
    ytd_rows = [row for row in confirmed_rows if fy_start <= row["invoice"].invoice_date <= today]
    confirmed_period = [row for row in period_rows if row["invoice"].status in CONFIRMED]
    totals = {
        "total": sum((r["invoice"].total_amount for r in confirmed_period), ZERO),
        "current_month": sum((r["invoice"].total_amount for r in monthly_rows), ZERO),
        "year_to_date": sum((r["invoice"].total_amount for r in ytd_rows), ZERO),
        "paid": sum((r["invoice"].total_amount for r in confirmed_period if r["payment_state"] == "paid"), ZERO),
        "partial": sum((r["invoice"].total_amount for r in confirmed_period if r["payment_state"] == "partial"), ZERO),
        "unpaid": sum((r["invoice"].total_amount for r in confirmed_period if r["payment_state"] == "unpaid"), ZERO),
        "outstanding": sum((r["outstanding"] for r in confirmed_period), ZERO),
        "gst": sum((
            (r["gst"] * min(r["paid"], r["invoice"].total_amount) / r["invoice"].total_amount)
            if r["invoice"].total_amount else ZERO
            for r in confirmed_period
        ), ZERO).quantize(Decimal("0.01")),
    }
    months = []
    cursor = start.replace(day=1)
    last_month = end.replace(day=1)
    while cursor <= last_month:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        months.append({
            "label": cursor.strftime("%b %Y"),
            "amount": sum((r["invoice"].total_amount for r in confirmed_period if cursor <= r["invoice"].invoice_date < next_month), ZERO),
        })
        cursor = next_month
    categories = {}
    for row in confirmed_period:
        name = row["detail"].category.name if row["detail"] else "Uncategorized"
        categories[name] = categories.get(name, ZERO) + row["invoice"].total_amount
    category_data = sorted(categories.items(), key=lambda item: item[1], reverse=True)
    recent = sorted(confirmed_period, key=lambda row: (row["invoice"].invoice_date, row["invoice"].created_at), reverse=True)[:8]
    upcoming_recurring = RecurringExpense.objects.filter(
        business=business, is_active=True,
    ).select_related("category", "supplier").order_by("next_due_date")[:6]
    return render(request, "expenses/dashboard.html", {
        "business": business, "totals": totals, "start_date": start, "end_date": end,
        "selected_period": period, "date_error": date_error, "filter_start_date": request.GET.get("start_date", start.isoformat()),
        "filter_end_date": request.GET.get("end_date", end.isoformat()), "monthly_data": months,
        "category_data": category_data, "recent_expenses": recent,
        "upcoming_recurring": upcoming_recurring,
        "itc_summary": {status:sum((r["itc"]["amount"] for r in confirmed_period if r["itc"]["status"]==status),ZERO) for status in ("potential","confirmed")},
        "pending_itc":sum(r["itc"]["status"]=="pending" for r in confirmed_period),
        "saved_gst":sum((r["gst"] for r in confirmed_period),ZERO),
    })


def _expense_list_context(request, business):
    from invoices.date_evidence import with_invoice_dates
    qs = with_invoice_dates(_expense_queryset(request.user))
    if request.GET.get("status")!="cancelled":qs=qs.exclude(status="cancelled")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    category_id = request.GET.get("category", "")
    supplier_id = request.GET.get("supplier", "")
    start_text, end_text = request.GET.get("start_date", ""), request.GET.get("end_date", "")
    if start_text:
        try:
            qs = qs.filter(evidence_invoice_date__gte=date.fromisoformat(start_text))
        except ValueError:
            messages.error(request, "Enter a valid start date.")
    if end_text:
        try:
            qs = qs.filter(evidence_invoice_date__lte=date.fromisoformat(end_text))
        except ValueError:
            messages.error(request, "Enter a valid end date.")
    if category_id.isdigit() and len(category_id) <= 18:
        qs = qs.filter(expense_detail__category_id=category_id, expense_detail__category__business=business)
    if supplier_id.isdigit() and len(supplier_id) <= 18:
        qs = qs.filter(supplier_party_id=supplier_id, supplier_party__business=business)
    if q:
        qs = qs.filter(Q(expense_detail__title__icontains=q) | Q(supplier_name__icontains=q) | Q(invoice_number__icontains=q))
    from django.core.paginator import Paginator
    derived_filter = status in {"overdue", "paid", "partial", "unpaid"} or bool(request.GET.get("itc", ""))
    page = None if derived_filter else Paginator(qs.order_by("-evidence_invoice_date", "-pk"), 25).get_page(request.GET.get("page"))
    available = advanced_ready()
    if derived_filter and qs.count() > 1000:
        messages.error(request, 'Narrow dates or filters: exact payment/ITC filtering supports at most 1,000 matching expenses.')
        page = Paginator([], 25).get_page(1)
        return [], {"itc":request.GET.get("itc", ""),"q":q,"status":status,"category":category_id,"supplier":supplier_id,"start_date":start_text,"end_date":end_text}, page
    selected = qs if derived_filter else page.object_list
    if available:
        selected = selected.prefetch_related(Prefetch('itc_reviews',queryset=ExpenseITCReview.objects.filter(business=business).order_by('-pk')[:1],to_attr='_latest_itc_reviews'))
    evidence_context = {'business':business,'available':available,'documents':{},'bytes_read':0}
    try:
        rows = [_expense_row(invoice, evidence_context=evidence_context) for invoice in selected]
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
        page = Paginator([], 25).get_page(1)
        return [], {"itc":request.GET.get("itc", ""),"q":q,"status":status,"category":category_id,"supplier":supplier_id,"start_date":start_text,"end_date":end_text}, page
    if status == "overdue":
        rows = [row for row in rows if row["is_overdue"]]
    elif status in {"paid", "partial", "unpaid"}:
        rows = [row for row in rows if row["payment_state"] == status]
    itc_filter=request.GET.get("itc", "")
    if itc_filter:rows=[row for row in rows if row["itc"]["status"]==itc_filter]
    if derived_filter:
        page = Paginator(rows, 25).get_page(request.GET.get("page"))
        rows = list(page.object_list)
    return rows, {"itc":itc_filter,"q": q, "status": status, "category": category_id, "supplier": supplier_id, "start_date": start_text, "end_date": end_text}, page


@login_required
def expense_list_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    rows, filters, page = _expense_list_context(request, business)
    return render(request, "expenses/list.html", {
        "rows": rows, "filters": filters, "page": page,
        "categories": ExpenseCategory.objects.filter(business=business, is_active=True),
        "suppliers": BusinessParty.objects.filter(business=business, is_active=True, party_type__in=("vendor", "both")),
    })


def _calculate_tax_parts(invoice, supplier, total_tax):
    from invoices.manual_calculations import split_tax
    for key,value in split_tax(total_tax,invoice.supplier_state_code,invoice.place_of_supply,invoice.supply_category).items():
        setattr(invoice,key,value)


def _expense_snapshot(invoice, detail):
    return {
        "title": detail.title,
        "category": detail.category.name,
        "supplier": invoice.supplier_name,
        "supplier_gstin": invoice.supplier_gstin,
        "supplier_party_id": invoice.supplier_party_id or "",
        "expense_date": invoice.invoice_date.isoformat(),
        "due_date": invoice.due_date.isoformat() if invoice.due_date else "",
        "taxable_amount": str(invoice.taxable_amount),
        "cgst": str(invoice.cgst_amount),
        "sgst": str(invoice.sgst_amount),
        "igst": str(invoice.igst_amount),
        "total": str(invoice.total_amount),
        "description": detail.description,
        "notes": detail.notes,
        "reference_number": detail.reference_number,
        "discount": str(detail.discount),
        "gst_inclusive": detail.gst_inclusive,
        "pricing_mode":invoice.pricing_mode,"supplier_state_code":invoice.supplier_state_code,"place_of_supply":invoice.place_of_supply,
        "supply_category":invoice.supply_category,"treatment_breakdown":invoice.treatment_breakdown,
    }


def _save_expense(request, form, invoice=None):
    business = BusinessProfile.objects.get(user=request.user)
    data = form.cleaned_data
    is_new = invoice is None
    detail_before = getattr(invoice, "expense_detail", None) if invoice else None
    before = _expense_snapshot(invoice, detail_before) if invoice and detail_before else {}
    old_detail_file = (detail_before.attachment.storage, detail_before.attachment.name) if detail_before and detail_before.attachment else None
    supplier = None if data.get("add_new_supplier") else (data.get("supplier_party") or (invoice.supplier_party if invoice else None))
    supplier_name = supplier.name if supplier else data.get("supplier_name", "").strip()
    supplier_gstin = supplier.gstin if supplier else data.get("supplier_gstin", "")

    from .duplicates import expense_duplicates
    import hashlib
    uploaded=data.get("attachment");file_hash=""
    if uploaded:
        file_hash=hashlib.sha256(uploaded.read()).hexdigest();uploaded.seek(0)
    signals=expense_duplicates(request.user,data["reference_number"],supplier_gstin,data["expense_date"],data["calculated_total"],exclude=invoice.pk if invoice else None,file_hash=file_hash)
    if any(v["kind"] in {"strong","exact"} for v in signals) or (signals and not data.get("acknowledge_duplicates")):
        form.add_error(None,"Duplicate receipt/purchase/expense signals found. Review existing records; distinct possible matches require acknowledgment.");return None

    existing = Invoice.objects.filter(user=request.user, invoice_type="expense").annotate(
        normalized_number=Lower(Trim("invoice_number", chars=NORMALIZATION_WHITESPACE))
    ).filter(normalized_number=data["reference_number"].strip(NORMALIZATION_WHITESPACE).lower())
    existing = existing.annotate(
        normalized_gstin=Lower(Trim("supplier_gstin", chars=NORMALIZATION_WHITESPACE))
    ).filter(normalized_gstin=(supplier_gstin or "").strip().lower())
    if invoice:
        existing = existing.exclude(pk=invoice.pk)
    if existing.exists():
        form.add_error("reference_number", "An expense with this reference number and supplier already exists.")
        return None

    if invoice is None:
        invoice = Invoice(user=request.user, invoice_type="expense")
    invoice.supplier_party = supplier
    invoice.supplier_name = supplier_name
    invoice.supplier_gstin = supplier_gstin
    invoice.buyer_name = ""
    invoice.buyer_gstin = ""
    invoice.invoice_number = data["reference_number"].strip()
    invoice.invoice_date = data["expense_date"]
    invoice.due_date = data.get("due_date")
    invoice.taxable_amount = data["calculated_taxable"]
    invoice.other_charges = ZERO
    tax = data["calculated_tax"]
    invoice.cgst_amount = invoice.sgst_amount = invoice.igst_amount = ZERO
    invoice.total_amount = data["calculated_total"]
    if invoice.pk is None:
        invoice.status = "verified"
    elif invoice.status != "review":
        paid_before_save = InvoicePayment.objects.filter(user=request.user, invoice=invoice).aggregate(total=Sum("amount"))["total"] or ZERO
        invoice.status = "paid" if paid_before_save == invoice.total_amount else "verified"
    invoice.supplier_state_code=data["supplier_state_code"]
    invoice.place_of_supply=data["place_of_supply"]
    invoice.supply_category=data["supply_category"]
    invoice.pricing_mode=data["gst_inclusive"]
    invoice.calculation_version=1
    invoice.invoice_discount_type="fixed"
    invoice.invoice_discount_value=invoice.invoice_discount_amount=ZERO
    invoice.total_discount_amount=data.get("discount") or ZERO
    invoice.treatment_breakdown={key:str(value) for key,value in data["gst_result"]["treatment_breakdown"].items()}
    _calculate_tax_parts(invoice, supplier, tax)
    uploaded_attachment = form.cleaned_data.get("attachment")
    remove_attachment = form.cleaned_data.get("remove_attachment")
    new_attachment_name = ""
    if uploaded_attachment or remove_attachment:
        invoice.source_file = ""

    try:
        with transaction.atomic():
            if data["add_new_supplier"] and supplier is None:
                supplier = BusinessParty.objects.create(
                    business=business, party_type="vendor", name=data["supplier_name"].strip(),
                    gstin=data.get("supplier_gstin", ""), email=data.get("supplier_email", ""),
                    phone=data.get("supplier_phone", ""),
                )
                invoice.supplier_party = supplier
                invoice.supplier_name = supplier.name
                invoice.supplier_gstin = supplier.gstin
            invoice.full_clean()
            invoice.save()
            line_item = invoice.line_items.first()
            if line_item is None:
                line_item = InvoiceLineItem(invoice=invoice)
            line_item.description = data["title"].strip()
            line_item.quantity = Decimal("1.000")
            line_item.rate = data["amount"]
            line_item.discount_type="fixed"
            line_item.discount_value=line_item.discount=data.get("discount") or ZERO
            line_item.tax_treatment=data["tax_treatment"]
            line_item.invoice_discount_allocated=ZERO
            for key in ("cgst_amount","sgst_amount","igst_amount"):
                setattr(line_item,key,data["gst_result"]["lines"][0][key])
            line_item.tax_rate = Decimal(data["gst_rate"])
            line_item.taxable_amount = data["calculated_taxable"]
            line_item.tax_amount = tax
            line_item.total_amount = data["calculated_taxable"] + tax
            line_item.save()
            invoice.line_items.exclude(pk=line_item.pk).delete()
            try:
                detail = invoice.expense_detail
            except ExpenseDetail.DoesNotExist:
                detail = ExpenseDetail(invoice=invoice)
            detail.category = data["category"]
            detail.title = data["title"].strip()
            detail.description = data.get("description", "")
            detail.notes = data.get("notes", "")
            detail.reference_number = data["reference_number"].strip()
            detail.discount = data.get("discount") or ZERO
            detail.gst_inclusive = data["gst_inclusive"] == "inclusive"
            if uploaded_attachment:
                detail.attachment = uploaded_attachment
                detail.attachment_display_name = get_valid_filename(uploaded_attachment.name)[:255]
            elif remove_attachment:
                detail.attachment = ""
                detail.attachment_display_name = ""
            detail.full_clean()
            detail.save()
            if detail.attachment:remember_attachment(request,invoice,detail.attachment,"private",detail.attachment_display_name)
            if uploaded_attachment:
                new_attachment_name = detail.attachment.name
            after = _expense_snapshot(invoice, detail)
            if is_new:
                record_event(request=request, user=request.user, business=business, action="create", module="expenses", description=f"Expense {invoice.invoice_number} created.", obj=invoice, after={"title": detail.title, "category": detail.category.name, "total": invoice.total_amount})
            else:
                if before != after:
                    record_event(request=request, user=request.user, business=business, action="update", module="expenses", description=f"Expense {invoice.invoice_number} updated.", obj=invoice, before=before, after=after)
            if uploaded_attachment or remove_attachment:
                action_name = "replaced" if uploaded_attachment else "removed"
                record_event(request=request, user=request.user, business=business, action="update", module="expenses", description=f"Expense attachment {action_name}.", obj=invoice, after={"attachment_action": action_name, "attachment_name": detail.attachment_display_name})
            if form.allow_initial_payment and data.get("amount_paid", ZERO) > ZERO:
                payment = InvoicePayment(
                    user=request.user, party=supplier, invoice=invoice,
                    payment_date=data["payment_date"], amount=data["amount_paid"],
                    payment_mode=data["payment_mode"], reference_id=data["payment_reference"].strip(),
                    notes="Initial expense payment",
                )
                payment.full_clean()
                payment.save()
                record_event(request=request, user=request.user, business=business, action="payment", module="payments", description=f"Initial payment of ₹{payment.amount:.2f} recorded for expense {invoice.invoice_number}.", obj=payment, related_obj=invoice, after={"payment_amount": payment.amount, "payment_date": payment.payment_date, "payment_mode": payment.payment_mode, "payment_reference": payment.reference_id}, metadata={"invoice_id": invoice.pk, "party_id": supplier.pk if supplier else None})
                if data["amount_paid"] == invoice.total_amount:
                    invoice.status = "paid"
                    invoice.save(update_fields=["status", "updated_at"])
    except (IntegrityError, ValidationError):
        if new_attachment_name and (not old_detail_file or new_attachment_name != old_detail_file[1]):
            private_expense_storage.delete(new_attachment_name)
        form.add_error(None, "Expense could not be saved. Check the supplier, reference and financial values.")
        return None
    return invoice


@login_required
def expense_create_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    _ensure_default_categories(business)
    form = ExpenseForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None, business=business)
    if request.method == "POST" and form.is_valid():
        invoice = _save_expense(request, form)
        if invoice:
            messages.success(request, "Expense saved.")
            return redirect("expense_detail", expense_id=invoice.pk)
    return render(request, "expenses/form.html", {"form": form, "page_title": "Add Expense", "submit_label": "Save Expense"})


@login_required
@transaction.atomic
def expense_edit_view(request, expense_id):
    business = _business(request)
    if business is None:
        return redirect("profile")
    invoice = get_object_or_404(_expense_queryset(request.user).select_for_update(), pk=expense_id)
    if invoice.status == "cancelled":
        messages.error(request, "Cancelled expenses cannot be edited.")
        return redirect("expense_detail", expense_id=invoice.pk)
    detail = getattr(invoice, "expense_detail", None)
    if detail is None:
        _ensure_default_categories(business)
        detail = ExpenseDetail(invoice=invoice, category=ExpenseCategory.objects.get(business=business, name="Miscellaneous"), title=invoice.supplier_name, reference_number=invoice.invoice_number)
    from invoices.extraction_services import latest_attempt
    attempt=latest_attempt(invoice)
    if invoice.status in CONFIRMED or invoice.invoice_type=="purchase" or attempt:
        form=ExpenseMetadataForm(request.POST if request.method=="POST" else None,request.FILES if request.method=="POST" else None,business=business,invoice=invoice,detail=detail)
        if request.method=="POST" and form.is_valid():
            changed=False
            for key in ("amount","discount","gst_rate","gst_inclusive","supplier_state_code","place_of_supply","tax_treatment","supply_category","expense_date","due_date","reference_number","supplier_party"):
                if key not in request.POST:continue
                raw=request.POST.get(key,"");saved=form.initial.get(key,"")
                if key in {"amount","discount","gst_rate"}:
                    try:equal=Decimal(raw)==Decimal(str(saved))
                    except Exception:equal=False
                else:equal=str(raw or "")==str(saved or "")
                changed=changed or not equal
            if changed:form.add_error(None,"Recognized financial values are immutable. Cancel an unpaid, unreconciled expense with a reason and create a replacement; settled records require a dedicated correction workflow.")
            else:
                try:save_metadata(request,invoice,form.cleaned_data)
                except ValidationError as exc:form.add_error(None,exc)
                else:return redirect("expense_detail",expense_id=invoice.pk)
        return render(request,"expenses/metadata_form.html",{"form":form,"invoice":invoice,"detail":detail,"financial_locked":True,"current_attachment_name":detail.attachment_display_name,"page_title":"Expense metadata — financial values locked","submit_label":"Save metadata"})
    form = ExpenseForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None,
                       business=business, invoice=invoice, detail=detail, allow_initial_payment=False)
    if request.method == "POST" and form.is_valid():
        paid = InvoicePayment.objects.filter(user=request.user, invoice=invoice).aggregate(total=Sum("amount"))["total"] or ZERO
        if paid and (
            (form.cleaned_data.get("supplier_party") or invoice.supplier_party) != invoice.supplier_party
            or form.cleaned_data.get("add_new_supplier")
        ):
            form.add_error("supplier_party", "Supplier cannot be changed after payments are recorded; this keeps payment history linked correctly.")
        elif form.cleaned_data["calculated_total"] < paid:
            form.add_error("amount", f"The total cannot be less than payments already recorded (₹{paid:.2f}).")
        else:
            updated = _save_expense(request, form, invoice)
            if updated:
                messages.success(request, "Expense changes saved.")
                return redirect("expense_detail", expense_id=invoice.pk)
    current_attachment_name = detail.attachment_display_name if detail.attachment else (Path(invoice.source_file.name).name if invoice.source_file else "")
    return render(request, "expenses/form.html", {"form": form, "invoice": invoice, "detail": detail, "current_attachment_name": current_attachment_name, "page_title": "Edit Expense", "submit_label": "Save Changes"})


@login_required
def expense_detail_view(request, expense_id):
    business = _business(request)
    if business is None:
        return redirect("profile")
    invoice = get_object_or_404(_expense_queryset(request.user), pk=expense_id)
    row = _expense_row(invoice)
    detail = row["detail"]
    attachment_name = ""
    if detail and detail.attachment:
        attachment_name = detail.attachment_display_name or Path(detail.attachment.name).name
    elif invoice.source_file:
        attachment_name = Path(invoice.source_file.name).name
    attachment_ext = Path(attachment_name).suffix.lower()
    payment_form = InvoicePaymentForm(
        invoices=Invoice.objects.filter(user=request.user, pk=invoice.pk),
        initial={"invoice": invoice.pk},
    )
    return render(request, "expenses/detail.html", {
        "invoice": invoice, "detail": detail, "paid": row["paid"],
        "outstanding": row["outstanding"], "payment_state": row["payment_state"],
        "is_overdue": row["is_overdue"],
        "payments": InvoicePayment.objects.filter(user=request.user, invoice=invoice).select_related("party"),
        "payment_form": payment_form,
        "attachment_name": attachment_name,
        "attachment_is_image": attachment_ext in {".jpg", ".jpeg", ".png"},
        "itc":row["itc"],
        "saved_discount":invoice.total_discount_amount if invoice.calculation_version==1 else (detail.discount if detail else ZERO),
        "receipt_revisions":invoice.expense_attachments.all() if advanced_ready() else [],
        "itc_reviews":invoice.itc_reviews.all() if advanced_ready() else [],
        "audit_events": AuditEvent.objects.filter(business__user=request.user).filter(
            Q(object_type="invoices.invoice", object_id=str(invoice.pk))
            | Q(related_object_type="invoices.invoice", related_object_id=str(invoice.pk))
        ).select_related("user")[:100],
    })


@login_required
@xframe_options_sameorigin
def expense_attachment_view(request, expense_id):
    invoice = get_object_or_404(managed_invoices(request.user), pk=expense_id)
    detail = getattr(invoice, "expense_detail", None)
    field = detail.attachment if detail and detail.attachment else invoice.source_file
    if not field:
        raise Http404("No attachment is available for this expense.")
    try:
        file_handle = field.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("The expense attachment is unavailable.")
    filename = (detail.attachment_display_name if detail and detail.attachment else Path(field.name).name) or "expense-attachment"
    content_type = {
        ".pdf": "application/pdf", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")
    inline = request.GET.get("inline") == "1" and content_type in {"application/pdf", "image/png", "image/jpeg"}
    response = FileResponse(file_handle, as_attachment=not inline, filename=get_valid_filename(filename), content_type=content_type)
    if inline:
        response["Content-Disposition"] = f'inline; filename="{get_valid_filename(filename)}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = "sandbox; frame-ancestors 'self'"
    return response


@login_required
@require_POST
@transaction.atomic
def expense_payment_view(request, expense_id):
    business = _business(request)
    if business is None:
        return redirect("profile")
    invoice = get_object_or_404(managed_invoices(request.user), pk=expense_id)
    if invoice.status not in {"verified", "paid"} or not invoice.supplier_party_id:
        messages.error(request, "This expense cannot receive a payment.")
        return redirect("expense_detail", expense_id=invoice.pk)
    form = InvoicePaymentForm(request.POST, invoices=Invoice.objects.filter(pk=invoice.pk, user=request.user))
    if form.is_valid():
        try:
            with transaction.atomic():
                locked = Invoice.objects.select_for_update().get(pk=invoice.pk, user=request.user)
                paid = InvoicePayment.objects.filter(user=request.user, invoice=locked).aggregate(total=Sum("amount"))["total"] or ZERO
                remaining = invoice_outstanding(locked)
                if form.cleaned_data["amount"] <= ZERO or form.cleaned_data["amount"] > remaining:
                    form.add_error("amount", f"Payment must be greater than zero and no more than ₹{remaining:.2f}.")
                elif form.cleaned_data["payment_date"] < locked.invoice_date:
                    form.add_error("payment_date", "Payment date cannot be earlier than the expense date.")
                else:
                    payment = form.save(commit=False)
                    payment.user, payment.party, payment.invoice = request.user, locked.supplier_party, locked
                    payment.full_clean()
                    payment.save()
                    record_event(request=request, user=request.user, business=business, action="payment", module="payments", description=f"Payment of ₹{payment.amount:.2f} recorded for expense {locked.invoice_number}.", obj=payment, related_obj=locked, after={"payment_amount": payment.amount, "payment_date": payment.payment_date, "payment_mode": payment.payment_mode, "payment_reference": payment.reference_id}, metadata={"invoice_id": locked.pk, "party_id": locked.supplier_party_id})
                    if payment.amount == remaining:
                        locked.status = "paid"
                        locked.save(update_fields=["status", "updated_at"])
        except (IntegrityError, ValidationError):
            form.add_error(None, "Payment could not be recorded. Check the amount and transaction reference.")
        else:
            if not form.errors:
                messages.success(request, "Expense payment recorded.")
                return redirect("expense_detail", expense_id=invoice.pk)
    errors = [str(error) for field_errors in form.errors.values() for error in field_errors]
    messages.error(request, errors[0] if errors else "Please correct the payment details.")
    return redirect("expense_detail", expense_id=invoice.pk)


@login_required
@require_POST
@transaction.atomic
def expense_cancel_view(request, expense_id):
    invoice = get_object_or_404(managed_invoices(request.user).select_for_update(), pk=expense_id)
    if invoice.status == 'processing' or invoice.extraction_attempts.filter(status__in=['uploaded','processing']).exists():
        messages.error(request,'Extraction is still processing. Wait before cancelling this provisional invoice.')
        return redirect('invoice_detail',invoice_id=invoice.pk)
    if invoice.invoice_type=="purchase":
        messages.error(request,"Linked purchases retain their original financial lifecycle. No cancellation is performed from the expense register.");return redirect("expense_detail",expense_id=invoice.pk)
    if invoice.status == "cancelled":
        return redirect("expense_list")
    if invoice.status == "paid" or InvoicePayment.objects.filter(invoice=invoice).exists() or invoice.reconciliation_records.exists() or invoice.adjustment_notes.filter(finalized_at__isnull=False).exists():
        messages.error(request, "This expense has payment history and cannot be deleted. Its financial history has been retained.")
    else:
        reason=request.POST.get("reason", "").strip()
        if not reason:
            messages.error(request,"A cancellation reason is required.");return redirect("expense_detail",expense_id=invoice.pk)
        invoice.status = "cancelled"
        invoice.save(update_fields=["status", "updated_at"])
        record_event(request=request, user=request.user, action="cancel", module="expenses", description=f"Expense {invoice.invoice_number} cancelled.", obj=invoice, before={"status": "active"}, after={"status": "cancelled"},metadata={"reason":reason[:500]})
        messages.success(request, "Expense deleted from active reports. Its record and audit history are retained as a cancellation.")
    return redirect("expense_list")


@login_required
@require_POST
def recurring_generate_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    created = generate_due_expenses(business, timezone.localdate(), actor=request.user,allow_backfill=request.POST.get("include_backlog")=="on")
    messages.success(request, f"Generated {len(created)} expense record(s) for due recurring schedules.")
    return redirect("recurring_expense_list")


@login_required
def recurring_expense_list_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    schedules = RecurringExpense.objects.filter(business=business).select_related("category", "supplier").annotate(generation_count=Count("generations")).prefetch_related(Prefetch("generations",queryset=RecurringExpenseGeneration.objects.order_by("-generated_at")[:20],to_attr="recent_generations"))
    return render(request, "expenses/recurring_list.html", {"schedules": schedules, "upcoming": schedules.filter(is_active=True).order_by("next_due_date")[:8]})


@login_required
def recurring_expense_create_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    _ensure_default_categories(business)
    form = RecurringExpenseForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            schedule = form.save()
            record_event(request=request, business=business, action="create", module="expenses", description=f"Recurring expense schedule {schedule.title} created.", obj=schedule, after={"title": schedule.title, "category": schedule.category.name, "amount": schedule.taxable_amount, "frequency": schedule.frequency, "start_date": schedule.start_date, "end_date": schedule.end_date})
        messages.success(request, "Recurring expense schedule created.")
        return redirect("recurring_expense_list")
    return render(request, "expenses/recurring_form.html", {"form": form, "page_title": "Add recurring expense"})


@login_required
@transaction.atomic
def recurring_expense_edit_view(request, recurring_id):
    business = _business(request)
    if business is None:
        return redirect("profile")
    schedule = get_object_or_404(RecurringExpense, pk=recurring_id, business=business)
    before = {field: getattr(schedule, field) for field in ("title", "taxable_amount", "gst_rate", "gst_inclusive", "frequency", "start_date", "end_date", "is_active", "supplier_state_code", "place_of_supply", "supply_category", "tax_treatment")}
    form = RecurringExpenseForm(request.POST or None, instance=schedule, business=business)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            after = {field: getattr(schedule, field) for field in before}
            if before != after:
                record_event(request=request, business=business, action="update", module="expenses", description=f"Recurring expense schedule {schedule.title} updated.", obj=schedule, before=before, after=after)
        messages.success(request, "Recurring expense schedule updated.")
        return redirect("recurring_expense_list")
    return render(request, "expenses/recurring_form.html", {"form": form, "schedule": schedule, "page_title": "Edit recurring expense"})


@login_required
@require_POST
@transaction.atomic
def recurring_expense_action_view(request, recurring_id, action):
    business = _business(request)
    if business is None:
        return redirect("profile")
    schedule = get_object_or_404(RecurringExpense, pk=recurring_id, business=business)
    previous_status = "cancelled" if schedule.is_cancelled else "active" if schedule.is_active else "paused"
    if action == "pause":
        schedule.is_active = False
        messages.success(request, "Recurring expense paused.")
    elif action == "resume" and not schedule.is_cancelled:
        schedule.is_active = True
        messages.success(request, "Recurring expense resumed.")
    elif action == "cancel":
        schedule.is_active = False
        schedule.is_cancelled = True
        schedule.cancelled_at = timezone.now()
        messages.success(request, "Recurring expense cancelled. Generated expense history is retained.")
    else:
        raise Http404
    with transaction.atomic():
        schedule.save(update_fields=["is_active", "is_cancelled", "cancelled_at", "updated_at"])
        current_status = "cancelled" if schedule.is_cancelled else "active" if schedule.is_active else "paused"
        record_event(request=request, business=business, action="cancel" if action == "cancel" else "update", module="expenses", description=f"Recurring expense {schedule.title} {current_status}.", obj=schedule, before={"schedule_status": previous_status}, after={"schedule_status": current_status})
    return redirect("recurring_expense_list")


@login_required
def expense_categories_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    _ensure_default_categories(business)
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())
        if not name or len(name)>100:
            messages.error(request, "Enter a category name of at most 100 characters.")
        elif ExpenseCategory.objects.filter(business=business, name__iexact=name).exists():
            messages.error(request, "That category already exists.")
        else:
            with transaction.atomic():
                category = ExpenseCategory.objects.create(business=business, name=name)
                record_event(request=request, business=business, action="create", module="expenses", description=f"Expense category {name} created.", obj=category, after={"name": name})
            messages.success(request, "Expense category added.")
            return redirect("expense_categories")
    return render(request, "expenses/categories.html", {"categories": ExpenseCategory.objects.filter(business=business)})


@login_required
def expense_category_edit_view(request, category_id):
    business = _business(request)
    if business is None:
        return redirect("profile")
    category = get_object_or_404(ExpenseCategory, pk=category_id, business=business)
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())
        if not name or len(name)>100:
            messages.error(request, "Enter a category name of at most 100 characters.")
        elif ExpenseCategory.objects.filter(business=business, name__iexact=name).exclude(pk=category.pk).exists():
            messages.error(request, "That category already exists.")
        else:
            previous_name = category.name
            with transaction.atomic():
                category.name = name
                category.is_active=(request.POST.get("is_active")=="on") if ("category_status_present" in request.POST or "is_active" in request.POST) else category.is_active
                category.save(update_fields=["name", "is_active", "updated_at"])
                record_event(request=request, business=business, action="update", module="expenses", description=f"Expense category {name} updated.", obj=category, before={"name": previous_name}, after={"name": category.name})
            messages.success(request, "Expense category updated.")
            return redirect("expense_categories")
    return render(request, "expenses/category_form.html", {"category": category})


@login_required
@require_POST
def expense_category_deactivate_view(request, category_id):
    business = _business(request)
    if business is None:
        return redirect("profile")
    category = get_object_or_404(ExpenseCategory, pk=category_id, business=business)
    if category.is_default:
        messages.error(request, "Default categories cannot be deactivated.")
    else:
        previous_name = category.name
        with transaction.atomic():
            category.is_active = False
            category.save(update_fields=["is_active", "updated_at"])
            record_event(request=request, business=business, action="update", module="expenses", description=f"Expense category {previous_name} deactivated.", obj=category, before={"is_active": True}, after={"is_active": False})
        messages.success(request, "Category deactivated. Existing expenses retain this category.")
    return redirect("expense_categories")


@login_required
@require_POST
def expense_calculation_preview(request):
    from copy import deepcopy
    from django import forms
    from django.conf import settings
    from django.http import JsonResponse
    from invoices.manual_calculations import calculate_invoice
    get_object_or_404(BusinessProfile,user=request.user)
    form=forms.Form(request.POST)
    form.fields={name:deepcopy(ExpenseForm.base_fields[name]) for name in ("amount","discount","gst_rate","gst_inclusive","supplier_state_code","place_of_supply","supply_category","tax_treatment")}
    form.fields["gst_rate"].choices=[(value,value+"%") for value in settings.GST_ALLOWED_RATES]
    if not form.is_valid():
        return JsonResponse({"errors":form.errors},status=400)
    data=form.cleaned_data
    try:
        result=calculate_invoice([{"quantity":Decimal("1"),"rate":data["amount"],"discount":data["discount"] or ZERO,"tax_rate":Decimal(data["gst_rate"]),"tax_treatment":data["tax_treatment"]}],
            data["supplier_state_code"],data["place_of_supply"],pricing_mode=data["gst_inclusive"],supply_category=data["supply_category"],allowed_rates=settings.GST_ALLOWED_RATES)
    except ValidationError as error:
        return JsonResponse({"errors":error.messages},status=400)
    return JsonResponse(result)
