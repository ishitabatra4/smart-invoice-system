"""Read-only review links. Counts disclose their scope; source hashing is bounded."""
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404
from django.urls import reverse
from invoices.models import Invoice, InvoiceExtractionAttempt
from expenses.services import itc_state
from .models import TaxWorkingPaper
from .preparation import stale


def review_panel(user, params, period=None):
    invoices = Invoice.objects.filter(user=user)
    papers = TaxWorkingPaper.objects.filter(period__business__user=user)
    if period:
        invoices = invoices.filter(invoice_date__range=(period.start_date, period.end_date))
        papers = papers.filter(period=period)
    attempts = InvoiceExtractionAttempt.objects.filter(user=user, invoice_id=OuterRef('pk')).order_by('-pk')
    awaiting = invoices.filter(status__in=('draft', 'uploaded', 'processing', 'review')).annotate(review_attempt=Subquery(attempts.values('pk')[:1])).order_by('-pk')
    review_page = Paginator(awaiting, 10).get_page(params.get('review_page'))
    review_rows = []
    for invoice in review_page:
        if invoice.entry_mode == 'manual' and invoice.document_state == 'draft':
            url = reverse('invoice_draft_edit', args=[invoice.pk])
        elif invoice.review_attempt:
            url = reverse('extraction_review', args=[invoice.review_attempt])
        else:
            url = reverse('expense_detail' if invoice.invoice_type == 'expense' else 'invoice_detail', args=[invoice.pk])
        review_rows.append({'invoice': invoice, 'url': url, 'warnings': (invoice.validation_errors or [])[:10]})
    inputs = invoices.filter(invoice_type__in=('purchase', 'expense'), status__in=('verified', 'paid')).select_related('expense_detail').order_by('-pk')
    input_page = Paginator(inputs, 10).get_page(params.get('input_page'))
    input_rows = []
    for invoice in input_page:
        managed = invoice.invoice_type == 'expense' or hasattr(invoice, 'expense_detail')
        input_rows.append({'invoice': invoice, 'state': itc_state(invoice), 'url': reverse('expense_itc_review', args=[invoice.pk]) if managed else reverse('expense_purchase_link'), 'label': 'Review ITC' if managed else 'Attribute existing purchase before ITC review'})
    paper_page = Paginator(papers.select_related('period').defer('data', 'inputs').order_by('-pk'), 10).get_page(params.get('paper_page'))
    checked = None
    if params.get('check_paper'):
        try:
            pk = int(params['check_paper'])
        except (TypeError, ValueError):
            from django.http import Http404
            raise Http404 from None
        paper = get_object_or_404(papers.select_related('period__business__user'), pk=pk)
        try:
            changed = stale(paper)
            text = 'Source/rule changes detected: review an explicit revision.' if changed else 'Source/rule fingerprints currently match; this is not statutory acceptance.'
        except (ValidationError, OSError):
            changed = True
            text = 'Source verification is unavailable; review evidence before preparing a revision.'
        checked = {'paper': paper, 'changed': changed, 'text': text}
    latest = papers.order_by('-pk').first() if period else None
    warnings = latest.data.get('warnings', []) if latest else []
    return {'review_page': review_page, 'review_rows': review_rows, 'input_page': input_page, 'input_rows': input_rows, 'pending_on_page': sum(row['state']['status'] == 'pending' for row in input_rows), 'paper_page': paper_page, 'checked': checked, 'warnings': warnings[:20], 'warning_count': len(warnings), 'warning_paper': latest}
