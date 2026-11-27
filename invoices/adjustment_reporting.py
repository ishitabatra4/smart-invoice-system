"""Append-only recognition and dated reversals, separate from immutable originals."""
from decimal import Decimal
from django.db.models import Q
from .models import InvoiceAdjustmentNote as Note
from .adjustment_services import adjustments_available
ZERO=Decimal('0.00')


def note_events(user, start=None, end=None, *, invoice_ids=None):
    if not adjustments_available():return []
    notes=Note.objects.filter(user=user,business__user=user,finalized_at__isnull=False).select_related('original_invoice','party')
    if invoice_ids is not None:
        notes=notes.filter(original_invoice_id__in=invoice_ids)
    if start is not None and end is not None:
        notes=notes.filter(Q(document_date__range=(start,end))|Q(cancellation_date__range=(start,end)))
    notes=notes.prefetch_related('original_invoice__payments','original_invoice__adjustment_notes')
    events=[]
    for note in notes:
        sign=-1 if note.kind=='credit' else 1
        for day,direction,reversal in [(note.document_date,sign,False)]+([(note.cancellation_date,-sign,True)] if note.status=='cancelled' else []):
            if day and (start is None or day>=start) and (end is None or day<=end):
                events.append({'note':note,'date':day,'sign':direction,'reversal':reversal,'invoice_type':note.original_snapshot['type'],
                    **{key:direction*getattr(note,key) for key in ('taxable_amount','cgst_amount','sgst_amount','igst_amount','total_amount')}})
    return sorted(events,key=lambda event:(event['date'],event['note'].pk,event['reversal']))


def _invoice_notes(invoice):
    cached=getattr(invoice,'_prefetched_objects_cache',{}).get('adjustment_notes')
    if cached is not None:return [n for n in cached if n.user_id==invoice.user_id]
    if not invoice.pk or invoice._state.adding or not adjustments_available():return []
    return list(Note.objects.filter(original_invoice_id=invoice.pk,user_id=invoice.user_id))


def adjusted_invoice_amount(invoice, as_of=None):
    notes=_invoice_notes(invoice)
    effective=[n for n in notes if n.status=='finalized'] if as_of is None else [n for n in notes if n.finalized_at and n.document_date<=as_of and (n.status!='cancelled' or n.cancellation_date>as_of)]
    return invoice.total_amount+sum(((-1 if n.kind=='credit' else 1)*n.total_amount for n in effective),ZERO)


def has_effective_adjustments(invoice):
    return any(n.status=='finalized' for n in _invoice_notes(invoice))


def overpayment(invoice):
    from .ledger import payment_total
    return max(payment_total(invoice)-adjusted_invoice_amount(invoice),ZERO)


def reporting_summary(user,start,end):
    """Gross original amounts, distinct credit/debit events, and review-only GST buckets."""
    from analytics.services import build_analytics_context
    context=build_analytics_context(user,start,end,'custom')
    events=context['adjustment_events']
    context['note_breakdown']={side:{kind:sum((e['total_amount'] for e in events if e['invoice_type']==side and e['note'].kind==kind),ZERO) for kind in ('credit','debit')} for side in ('sales','purchase')}
    context['gst_preparation']={direction:{component:sum((e[component+'_amount'] for e in events if e['note'].direction==direction and e['note'].tax_effect=='gst'),ZERO) for component in ('taxable','cgst','sgst','igst')} for direction in ('issued','received')}
    context['profit_before_tax_estimate']=context['totals']['taxable_sales']-context['totals']['taxable_purchases']-context['totals']['expenses']
    return context
