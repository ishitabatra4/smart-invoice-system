import re

from django.db.models import Q

from .models import Invoice, InvoicePayment, NORMALIZATION_WHITESPACE
from decimal import Decimal

LEDGER_INVOICE_STATUSES = ("verified", "paid")


def _normalize_gstin(value):
    return re.sub(r"\s+", "", value or "").upper()


def _normalize_name(value):
    return " ".join((value or "").split()).casefold()


def invoice_party_matches(invoice, party):
    if invoice.invoice_type == "sales":
        linked_party_id = invoice.buyer_party_id
        invoice_name, invoice_gstin = invoice.buyer_name, invoice.buyer_gstin
    elif invoice.invoice_type == "purchase":
        linked_party_id = invoice.supplier_party_id
        invoice_name, invoice_gstin = invoice.supplier_name, invoice.supplier_gstin
    else:
        return False

    if linked_party_id:
        return linked_party_id == party.pk
    if _normalize_gstin(invoice_gstin) and _normalize_gstin(party.gstin):
        return _normalize_gstin(invoice_gstin) == _normalize_gstin(party.gstin)
    return _normalize_name(invoice_name) == _normalize_name(party.name)


def party_invoices(user, party):
    invoices = Invoice.objects.filter(
        user=user,
        status__in=LEDGER_INVOICE_STATUSES,
        invoice_type__in=("sales", "purchase"),
    ).select_related("supplier_party__business", "buyer_party__business").prefetch_related("payments", "adjustment_notes")
    return [invoice for invoice in invoices if invoice_party_matches(invoice, party)]


def payment_total(invoice):
    cached = getattr(invoice, "_prefetched_objects_cache", {}).get("payments")
    if cached is not None:
        from decimal import Decimal
        return sum((payment.amount for payment in cached if payment.user_id == invoice.user_id), Decimal("0.00"))
    from django.db.models import Sum
    from decimal import Decimal
    return InvoicePayment.objects.filter(invoice=invoice, user_id=invoice.user_id).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def invoice_outstanding(invoice, as_of=None):
    from .adjustment_reporting import adjusted_invoice_amount, has_effective_adjustments
    if as_of is None and invoice.status == "paid" and not has_effective_adjustments(invoice):
        return Decimal("0.00")
    if as_of is not None:
        cancelled_at = getattr(invoice, 'cancelled_at', None)
        if invoice.status == 'cancelled' and cancelled_at is not None:
            from django.utils import timezone
            day = timezone.localdate(cancelled_at) if timezone.is_aware(cancelled_at) else cancelled_at.date()
            if day <= as_of:
                return Decimal('0.00')
    paid = payment_total(invoice) if as_of is None else sum((p.amount for p in invoice.payments.all() if p.user_id == invoice.user_id and p.payment_date <= as_of), Decimal("0.00"))
    return max(adjusted_invoice_amount(invoice, as_of) - paid, Decimal("0.00"))


def payment_balance_state(invoice):
    if invoice_outstanding(invoice) == 0:
        return "paid"
    return "partial" if payment_total(invoice) > 0 else "unpaid"


def party_ledger_entries(user, party, *, invoices=None):
    invoices = party_invoices(user, party) if invoices is None else [invoice for invoice in invoices if invoice.user_id == user.pk and invoice_party_matches(invoice, party)]
    entries = []
    for invoice in invoices:
        if invoice.invoice_type == "sales":
            debit, credit, description = invoice.total_amount, None, "Sales invoice"
        else:
            debit, credit, description = None, invoice.total_amount, "Purchase invoice"
        entries.append({
            "date": invoice.invoice_date,
            "sort_order": 0,
            "reference": invoice.invoice_number,
            "description": description,
            "invoice": invoice,
            "debit": debit,
            "credit": credit,
            "transaction_type": "Invoice",
            "payment": None,
        })
        for payment in invoice.payments.all():
            if payment.user_id != user.pk:
                continue
            is_receipt = invoice.invoice_type == "sales"
            entries.append({
                "date": payment.payment_date,
                "sort_order": 2,
                "reference": payment.reference_id,
                "description": payment.notes or payment.get_payment_mode_display(),
                "invoice": invoice,
                "debit": payment.amount if not is_receipt else None,
                "credit": payment.amount if is_receipt else None,
                "transaction_type": "Payment received" if is_receipt else "Payment made",
                "payment": payment,
            })
    from .adjustment_reporting import note_events
    invoice_ids = {invoice.pk for invoice in invoices}
    for event in note_events(user, invoice_ids=invoice_ids):
        note = event["note"]
        if note.original_invoice_id not in invoice_ids:
            continue
        amount = event["total_amount"]
        sales = event["invoice_type"] == "sales"
        entries.append({"date":event["date"],"sort_order":1,"reference":note.number,
            "description":note.get_kind_display() + (" reversal" if event["reversal"] else ""),
            "invoice":note.original_invoice,"debit":abs(amount) if (amount>0)==sales else None,
            "credit":abs(amount) if (amount<0)==sales else None,"transaction_type":"Adjustment", "payment":None})
    entries.sort(key=lambda entry: (entry["date"], entry["sort_order"], entry["reference"]))
    receivable = payable = Decimal("0.00")
    for entry in entries:
        debit = entry["debit"] or Decimal("0.00")
        credit = entry["credit"] or Decimal("0.00")
        if entry["invoice"].invoice_type == "sales":
            receivable += debit - credit
        else:
            payable += credit - debit
        entry["receivable_balance"] = receivable
        entry["payable_balance"] = payable
        # Signed comparison only; never offsets either invoice or settles obligations.
        entry["balance"] = receivable - payable
    return entries
