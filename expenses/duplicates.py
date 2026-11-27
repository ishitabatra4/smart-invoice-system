from invoices.identity import normalized_identity
"""Owned cross-type duplicate signals; recurring amounts alone never imply duplication."""
from decimal import Decimal
from django.db.models.functions import Lower,Trim
from invoices.models import Invoice, NORMALIZATION_WHITESPACE
from invoices.extraction_services import history_ready


def expense_duplicates(user,number,gstin,day,total,exclude=None,file_hash=''):
    signals=[];number=(number or '').strip(NORMALIZATION_WHITESPACE).casefold();gstin=(gstin or '').strip(NORMALIZATION_WHITESPACE).upper()
    if file_hash and history_ready():
        from invoices.models import InvoiceExtractionAttempt
        ids=list(InvoiceExtractionAttempt.objects.filter(user=user,business__user=user,file_sha256=file_hash,invoice__invoice_type__in=['expense','purchase']).exclude(invoice_id=exclude).exclude(invoice_id=None).values_list('invoice_id',flat=True).distinct()[:10])
        if ids:signals.append({'kind':'exact','invoice_ids':ids,'reason':'Identical receipt bytes already exist in this business.'})
    if file_hash:
        from .services import advanced_ready
        if advanced_ready():
            from .models import ExpenseAttachmentRevision
            ids=list(ExpenseAttachmentRevision.objects.filter(invoice__user=user,sha256=file_hash).exclude(invoice_id=exclude).values_list("invoice_id",flat=True).distinct()[:10])
            if ids:signals.append({"kind":"exact","invoice_ids":ids,"reason":"Identical receipt bytes already exist in private expense history."})
    if number:
        qs=Invoice.objects.filter(user=user,invoice_type__in=['expense','purchase']).exclude(pk=exclude).annotate(key=normalized_identity('invoice_number')).filter(key=number)
        for invoice in qs[:100]:
            if gstin and gstin==invoice.supplier_gstin.strip(NORMALIZATION_WHITESPACE).upper():signals.append({'kind':'strong','invoice_ids':[invoice.pk],'reason':'Same supplier GSTIN and receipt reference across purchase/expense records.'})
            elif invoice.invoice_date==day and invoice.total_amount==total:signals.append({'kind':'possible','invoice_ids':[invoice.pk],'reason':'Same reference, date and total; review incomplete supplier identity.'})
    return signals


def assert_expense_identity(user,invoice,fields,file_hash,acknowledged):
    from django.core.exceptions import ValidationError
    signals=expense_duplicates(user,fields['invoice_number'],fields['supplier_gstin'],fields['invoice_date'],fields['total_amount'],exclude=invoice.pk,file_hash=file_hash)
    if any(s['kind'] in {'strong','exact'} for s in signals) or (signals and not acknowledged):
        raise ValidationError('An owned purchase/expense may already represent this receipt. Resolve duplicate accounting before confirming or verifying.')
