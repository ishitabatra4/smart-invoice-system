from config.export_security import spreadsheet_row, sanitize_workbook
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.forms import BusinessPartyForm
from accounts.models import BusinessParty, BusinessProfile
from audit.models import AuditEvent
from audit.services import record_event
from .ledger import (
    LEDGER_INVOICE_STATUSES, invoice_outstanding, invoice_party_matches,
    party_invoices, party_ledger_entries, payment_balance_state, payment_total,
)
from .models import Invoice, InvoicePayment
from .adjustment_reporting import adjusted_invoice_amount, overpayment
from .payment_forms import InvoicePaymentForm


def _business_for_user(user):
    return BusinessProfile.objects.filter(user=user).first()


def _party_or_redirect(request, party_id):
    business = _business_for_user(request.user)
    if business is None:
        messages.error(request, "Set up your business profile before managing party ledgers.")
        return None, redirect("profile")
    party = get_object_or_404(BusinessParty, pk=party_id, business=business)
    return party, None


def _party_for_invoice(invoice, parties):
    linked = invoice.buyer_party if invoice.invoice_type == "sales" else invoice.supplier_party
    if linked and linked.business.user_id == invoice.user_id:
        return linked
    return next((party for party in parties if invoice_party_matches(invoice, party)), None)


def _invoice_rows(user, parties):
    invoices = Invoice.objects.filter(
        user=user,
        status__in=LEDGER_INVOICE_STATUSES,
        invoice_type__in=("sales", "purchase"),
    ).select_related("supplier_party__business", "buyer_party__business").prefetch_related("payments", "adjustment_notes")
    rows = []
    for invoice in invoices:
        party = _party_for_invoice(invoice, parties)
        if party is None:
            continue
        paid = payment_total(invoice)
        outstanding = invoice_outstanding(invoice)
        rows.append({
            "invoice": invoice,
            "party": party,
            "side": "customer" if invoice.invoice_type == "sales" else "supplier",
            "paid": paid,
            "outstanding": outstanding,
            "state": "paid" if outstanding == 0 else "partial" if paid > 0 else "unpaid",
            "overdue": bool(invoice.due_date and invoice.due_date < timezone.localdate() and outstanding > 0),
        })
    return rows


@login_required
def party_list_view(request):
    business = _business_for_user(request.user)
    if business is None:
        messages.error(request, "Set up your business profile before managing party ledgers.")
        return redirect("profile")
    role = request.GET.get("role", "")
    parties = list(BusinessParty.objects.filter(business=business, is_active=True))
    invoice_rows = _invoice_rows(request.user, parties)
    grouped = {party.pk: {"party": party, "invoiced": Decimal("0.00"), "paid": Decimal("0.00"), "outstanding": Decimal("0.00"), "receivable": Decimal("0.00"), "payable": Decimal("0.00")} for party in parties}
    for row in invoice_rows:
        total = grouped[row['party'].pk]
        total['invoiced'] += row['invoice'].total_amount
        total['paid'] += row['paid']
        total['outstanding'] += row['outstanding']
        total['receivable' if row['side']=='customer' else 'payable'] += row['outstanding']
    party_rows = list(grouped.values())
    if role in ("customer", "supplier"):
        kinds = ("customer" if role == "customer" else "vendor", "both")
        party_rows = [row for row in party_rows if row["party"].party_type in kinds]
    return render(request, "invoices/party_list.html", {"party_rows": party_rows, "party_role": role})


@login_required
def party_create_view(request):
    business = _business_for_user(request.user)
    if business is None:
        messages.error(request, "Set up your business profile before adding parties.")
        return redirect("profile")
    form_data = request.POST if request.method == "POST" else None
    role = request.GET.get("role", "")
    initial = {"party_type": "customer" if role == "customer" else "vendor"} if role in ("customer", "supplier") else {}
    form = BusinessPartyForm(form_data, business=business, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            party = form.save(commit=False)
            party.business = business
            with transaction.atomic():
                party.save()
                record_event(request=request, business=business, action="create", module="parties", description=f"Party {party.name} created.", obj=party, after={"name": party.name, "legal_name": party.legal_name, "party_type": party.party_type, "gstin": party.gstin, "email": party.email, "phone": party.phone})
            messages.success(request, "Party added successfully.")
            return redirect("party_detail", party_id=party.pk)
        messages.error(request, "Please correct the errors below and save again.")
    return render(request, "invoices/party_form.html", {"form": form, "page_title": "Add Party", "submit_label": "Add Party"})


@login_required
@transaction.atomic
def party_edit_view(request, party_id):
    party, response = _party_or_redirect(request, party_id)
    if response:
        return response
    form_data = request.POST if request.method == "POST" else None
    before = {field: getattr(party, field) for field in ("name", "legal_name", "party_type", "gstin", "email", "phone", "address_line", "city", "state", "pincode", "is_active")}
    form = BusinessPartyForm(form_data, instance=party, business=party.business)
    if request.method == "POST":
        if form.is_valid():
            with transaction.atomic():
                form.save()
                after = {field: getattr(party, field) for field in before}
                if before != after:
                    record_event(request=request, business=party.business, action="update", module="parties", description=f"Party {party.name} updated.", obj=party, before=before, after=after)
            messages.success(request, "Party details updated successfully.")
            return redirect("party_detail", party_id=party.pk)
        messages.error(request, "Please correct the errors below and save again.")
    return render(request, "invoices/party_form.html", {
        "form": form, "party": party, "page_title": "Edit Party", "submit_label": "Save Changes",
    })


@login_required
@transaction.atomic
def party_detail_view(request, party_id):
    party, response = _party_or_redirect(request, party_id)
    if response:
        return response
    invoices = party_invoices(request.user, party)
    for invoice in invoices:
        invoice.ledger_paid_amount = payment_total(invoice)
        invoice.ledger_outstanding_amount = invoice_outstanding(invoice)
        invoice.ledger_adjusted_amount = adjusted_invoice_amount(invoice)
    outstanding_invoices = [invoice for invoice in invoices if invoice.ledger_outstanding_amount > 0]
    if request.method == "POST":
        form = InvoicePaymentForm(request.POST, invoices=Invoice.objects.filter(pk__in=[i.pk for i in outstanding_invoices]))
        if form.is_valid():
            selected_invoice = form.cleaned_data["invoice"]
            if not invoice_party_matches(selected_invoice, party):
                form.add_error("invoice", "This invoice does not belong to the selected party.")
            elif form.cleaned_data["payment_date"] < selected_invoice.invoice_date:
                form.add_error("payment_date", "Payment date cannot be earlier than the invoice date. Advances are not supported.")
            else:
                try:
                    with transaction.atomic():
                        invoice = Invoice.objects.select_for_update().get(
                            pk=selected_invoice.pk,
                            user=request.user,
                            status__in=LEDGER_INVOICE_STATUSES,
                        )
                        if not invoice_party_matches(invoice, party):
                            raise ValidationError("This invoice does not belong to the selected party.")
                        payments_total = InvoicePayment.objects.filter(invoice=invoice,user=request.user).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
                        remaining = invoice_outstanding(invoice)
                        if form.cleaned_data["amount"] <= 0:
                            form.add_error("amount", "Payment amount must be greater than zero.")
                        elif form.cleaned_data["amount"] > remaining:
                            form.add_error("amount", f"Payment exceeds the remaining invoice balance of ₹{remaining:.2f}.")
                        else:
                            payment = form.save(commit=False)
                            payment.user = request.user
                            payment.party = party
                            payment.invoice = invoice
                            payment.full_clean()
                            payment.save()
                            record_event(request=request, business=party.business, action="payment", module="payments", description=f"Payment of ₹{payment.amount:.2f} recorded for invoice {invoice.invoice_number}.", obj=payment, related_obj=invoice, after={"payment_amount": payment.amount, "payment_date": payment.payment_date, "payment_mode": payment.payment_mode, "payment_reference": payment.reference_id}, metadata={"invoice_id": invoice.pk, "party_id": party.pk})
                            paid_after = payments_total + payment.amount
                            if paid_after >= adjusted_invoice_amount(invoice):
                                invoice.status = "paid"
                                invoice.save(update_fields=["status", "updated_at"])
                except Invoice.DoesNotExist:
                    form.add_error("invoice", "This invoice is no longer available for payment.")
                except IntegrityError:
                    form.add_error("reference_id", "This transaction reference has already been recorded for this invoice.")
                except ValidationError as exc:
                    if hasattr(exc, "error_dict"):
                        for field_name, errors in exc.error_dict.items():
                            form.add_error(field_name if field_name in form.fields else None, errors)
                    else:
                        form.add_error(None, exc)
                else:
                    if not form.errors:
                        messages.success(request, "Payment recorded successfully.")
                        return redirect("party_detail", party_id=party.pk)
    else:
        form = InvoicePaymentForm(
            invoices=Invoice.objects.filter(pk__in=[i.pk for i in outstanding_invoices])
        )

    entries = party_ledger_entries(request.user, party, invoices=invoices)
    activity = AuditEvent.objects.filter(business=party.business).filter(
        Q(object_type="accounts.businessparty", object_id=str(party.pk)) | Q(metadata__party_id=party.pk)
    ).select_related("user")[:50]
    return render(request, "invoices/party_detail.html", {
        "party": party,
        "invoices": invoices,
        "entries": entries,
        "activity": activity,
        "payments": InvoicePayment.objects.filter(
            user=request.user, party=party, invoice__invoice_type__in=("sales", "purchase")
        ).select_related("invoice"),
        "adjusted_invoiced_total": sum((invoice.ledger_adjusted_amount for invoice in invoices), Decimal("0.00")),
        "overpayment_total": sum((max(invoice.ledger_paid_amount - invoice.ledger_adjusted_amount, Decimal("0.00")) for invoice in invoices), Decimal("0.00")),
        "invoiced_total": sum((invoice.total_amount for invoice in invoices), Decimal("0.00")),
        "payments_total": sum((invoice.ledger_paid_amount for invoice in invoices), Decimal("0.00")),
        "receivable_total": sum((invoice.ledger_outstanding_amount for invoice in invoices if invoice.invoice_type=='sales'), Decimal('0.00')),
        "payable_total": sum((invoice.ledger_outstanding_amount for invoice in invoices if invoice.invoice_type=='purchase'), Decimal('0.00')),
        "outstanding_total": sum((invoice.ledger_outstanding_amount for invoice in invoices), Decimal("0.00")),
        "form": form,
    })


@login_required
def outstanding_dashboard_view(request):
    business = _business_for_user(request.user)
    if business is None:
        messages.error(request, "Set up your business profile before viewing outstanding payments.")
        return redirect("profile")
    parties = list(BusinessParty.objects.filter(business=business))
    all_rows = _invoice_rows(request.user, parties)
    totals = {
        "receivable": sum((row["outstanding"] for row in all_rows if row["side"] == "customer"), Decimal("0.00")),
        "payable": sum((row["outstanding"] for row in all_rows if row["side"] == "supplier"), Decimal("0.00")),
        "overdue_receivable": sum((row["outstanding"] for row in all_rows if row["side"] == "customer" and row["overdue"]), Decimal("0.00")),
        "overdue_payable": sum((row["outstanding"] for row in all_rows if row["side"] == "supplier" and row["overdue"]), Decimal("0.00")),
        "partial_count": sum(1 for row in all_rows if row["state"] == "partial"),
        "unpaid_count": sum(1 for row in all_rows if row["state"] == "unpaid"),
    }
    rows = all_rows
    party_kind = request.GET.get("party_type", "")
    status = request.GET.get("status", "")
    search = request.GET.get("q", "").strip()
    start_text, end_text = request.GET.get("start_date", ""), request.GET.get("end_date", "")
    try:
        start_date = date.fromisoformat(start_text) if start_text else None
        end_date = date.fromisoformat(end_text) if end_text else None
        if start_date and end_date and start_date > end_date:
            raise ValueError
    except ValueError:
        messages.error(request, "Enter a valid date range.")
        start_date = end_date = None
    if party_kind in {"customer", "supplier"}:
        rows = [row for row in rows if row["side"] == party_kind]
    if start_date:
        rows = [row for row in rows if row["invoice"].invoice_date >= start_date]
    if end_date:
        rows = [row for row in rows if row["invoice"].invoice_date <= end_date]
    if status == "overdue":
        rows = [row for row in rows if row["overdue"]]
    elif status in {"paid", "partial", "unpaid"}:
        rows = [row for row in rows if row["state"] == status]
    if search:
        query = search.casefold()
        rows = [row for row in rows if query in row["party"].name.casefold() or query in row["invoice"].invoice_number.casefold()]
    rows.sort(key=lambda row: (row["invoice"].due_date or row["invoice"].invoice_date, row["party"].name.casefold()))
    return render(request, "invoices/outstanding_dashboard.html", {
        "rows": rows, "totals": totals,
        "filters": {"party_type": party_kind, "status": status, "q": search, "start_date": start_text, "end_date": end_text},
    })


@login_required
def party_ledger_export_view(request, party_id):
    from openpyxl import Workbook

    party, response = _party_or_redirect(request, party_id)
    if response:
        return response
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Party Ledger"
    sheet.append(["Date", "Transaction/reference number", "Description", "Invoice number", "Debit", "Credit", "Receivable balance", "Payable balance", "Transaction type"])
    entries = party_ledger_entries(request.user, party)
    for entry in entries:
        sheet.append([
            entry["date"].isoformat(), entry["reference"], entry["description"],
            entry["invoice"].invoice_number,
            entry["debit"], entry["credit"], entry["receivable_balance"], entry["payable_balance"], entry["transaction_type"],
        ])
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="party-ledger-{party.pk}.xlsx"'
    sanitize_workbook(workbook)
    workbook.save(response)
    record_event(request=request, business=party.business, action="export", module="reports", description=f"Ledger for party {party.name} exported.", obj=party, after={"format": "xlsx", "transaction_count": len(entries)})
    return response
