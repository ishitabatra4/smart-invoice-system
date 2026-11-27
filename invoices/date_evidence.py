"""Source and reviewed dates; required provisional storage dates are never evidence."""
from datetime import date
from django.db.models import Case, When, Q, F, OuterRef, Subquery, DateField
from django.db.models.functions import Cast


def with_invoice_dates(queryset):
    from .models import InvoiceExtractionAttempt, InvoiceExtractionReview
    latest=InvoiceExtractionAttempt.objects.filter(invoice_id=OuterRef('pk')).order_by('-created_at','-pk')
    queryset=queryset.annotate(
        _source_attempt_id=Subquery(latest.values('pk')[:1]),
        _source_invoice_date=Subquery(latest.values('result__invoice_date')[:1]),
        _source_due_date=Subquery(latest.values('result__due_date')[:1]),
    )
    review=InvoiceExtractionReview.objects.filter(invoice_id=OuterRef('pk'),attempt_id=OuterRef('_source_attempt_id')).order_by('-confirmed_at','-pk')
    queryset=queryset.annotate(
        _review_invoice_date=Cast(Subquery(review.values('snapshot__invoice__header__invoice_date')[:1]),DateField()),
        _review_due_date=Cast(Subquery(review.values('snapshot__invoice__header__due_date')[:1]),DateField()),
    )
    return queryset.annotate(evidence_invoice_date=Case(
        When(Q(entry_mode='manual')|Q(status__in=('verified','paid'))|Q(_review_invoice_date=F('invoice_date')),then=F('invoice_date')),
        default=Cast(F('_source_invoice_date'),DateField()),output_field=DateField()))


def source_dates(invoice):
    if invoice.entry_mode=='manual' or invoice.status in ('verified','paid'):
        return {'invoice_date':invoice.invoice_date,'invoice_state':'saved','due_date':invoice.due_date,'due_state':'saved','uploaded_at':invoice.created_at}
    if not hasattr(invoice,'_source_attempt_id'):
        # Bounded lists annotate once; individual detail pages use one read-only lookup.
        from .models import Invoice
        invoice=with_invoice_dates(Invoice.objects.filter(pk=invoice.pk)).get()
    saved=invoice.entry_mode=='manual' or invoice.status in ('verified','paid')
    def evidence(field):
        value=getattr(invoice,field)
        if saved:return value,'saved'
        if getattr(invoice,'_review_'+field)==value and invoice._source_attempt_id and getattr(invoice,'_review_invoice_date'):
            return value,'reviewed'
        candidate=getattr(invoice,'_source_'+field)
        try:candidate=date.fromisoformat(candidate) if isinstance(candidate,str) else None
        except ValueError:candidate=None
        return candidate,'extracted' if candidate else 'unknown'
    day,state=evidence('invoice_date');due,due_state=evidence('due_date')
    return {'invoice_date':day,'invoice_state':state,'due_date':due,'due_state':due_state,'uploaded_at':invoice.created_at}
