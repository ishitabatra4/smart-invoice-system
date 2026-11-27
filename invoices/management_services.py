"""Explicit provisional reclassification/restoration; never financial acceptance."""
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from datetime import date
from decimal import Decimal, InvalidOperation
from accounts.models import BusinessProfile
from audit.models import AuditEvent
from audit.services import record_event
from .models import Invoice, InvoiceLineItem, InvoiceExtractionAttempt, InvoiceExtractionReview
from .extraction_services import latest_attempt, assert_original, duplicate_signals

TOKEN_SALT = 'invoice-provisional-management-v1'


def activity(invoice):
    return AuditEvent.objects.filter(Q(business__user_id=invoice.user_id) | Q(business__isnull=True,user_id=invoice.user_id)).filter(Q(object_type='invoices.invoice', object_id=str(invoice.pk)) | Q(related_object_type='invoices.invoice', related_object_id=str(invoice.pk))).order_by('-created_at', '-pk')


def classification_revision(invoice):
    return activity(invoice).filter(metadata__operation__in=['change_type','restore']).values_list('pk',flat=True).first() or 0


def eligibility_errors(invoice, business, operation, attempt=None):
    errors = []
    if invoice.user_id != business.user_id:
        return ['Choose an invoice in your own business workspace.']
    required = 'cancelled' if operation == 'restore' else 'review'
    if (invoice.entry_mode != 'uploaded' or invoice.status != required or invoice.document_state != 'legacy'
            or invoice.finalized_at or invoice.cancelled_at or invoice.calculation_version is not None or invoice.business_snapshot or invoice.party_snapshot):
        errors.append('Only never-confirmed uploaded provisional invoices in ' + ('Cancelled' if operation == 'restore' else 'Needs Review') + ' can use this action. Restore a cancelled provisional invoice separately before changing its type.')
    if invoice.extraction_reviews.exists():
        errors.append('A human-confirmed extraction review exists; this provisional action is forbidden.')
    # All other reverse relations are financial/metadata dependencies; new relations fail closed.
    for relation in Invoice._meta.related_objects:
        if relation.related_model in {InvoiceLineItem, InvoiceExtractionAttempt, InvoiceExtractionReview}:
            continue
        if relation.related_model.objects.filter(**{relation.field.name: invoice.pk}).exists():
            errors.append('Dependent ' + str(relation.related_model._meta.verbose_name) + ' records must not be reclassified or restored through this workflow.')
    for event in activity(invoice).only('action', 'before', 'after'):
        if event.action in {'verify', 'payment', 'payment_reverse'} or any(values.get('status') in {'verified','paid'} or values.get('document_state') == 'finalized' for values in (event.before, event.after)):
            errors.append('Audit history shows prior financial recognition; provisional restoration/reclassification is forbidden.')
            break
    attempt = attempt or latest_attempt(invoice)
    if (not attempt or attempt.user_id != invoice.user_id or attempt.business_id != business.pk
            or attempt.invoice_id != invoice.pk or attempt.status in {'uploaded', 'processing'}
            or invoice.source_file.name != attempt.source_file.name):
        errors.append('A completed, owned extraction and its intact retained original are required. Processing or mismatched workspace evidence cannot be changed.')
    if invoice.extraction_attempts.filter(status__in=['uploaded','processing']).exists():
        errors.append('Extraction is currently pending or processing. Wait for it to finish.')
    return list(dict.fromkeys(errors))


def state_token(invoice, business, operation, attempt):
    return signing.dumps({'user': invoice.user_id, 'business': business.pk, 'invoice': invoice.pk, 'operation': operation,
                          'type': invoice.invoice_type, 'status': invoice.status, 'version': invoice.updated_at.isoformat(),
                          'attempt': attempt.pk, 'sha256': attempt.file_sha256}, salt=TOKEN_SALT)


def target_duplicates(user, business, invoice, target_type, attempt):
    fields = {key: getattr(invoice, key) for key in ('invoice_number','supplier_gstin','buyer_gstin','supplier_name','buyer_name','invoice_date','total_amount')}
    if not invoice.extraction_reviews.exists():
        # Required storage dates/zero amounts are not source evidence for fuzzy duplicates.
        try: fields['invoice_date'] = date.fromisoformat(attempt.result.get('invoice_date') or '')
        except (TypeError,ValueError): fields['invoice_date'] = None
        try:
            fields['total_amount'] = Decimal(str(attempt.result.get('total_amount')))
            if not fields['total_amount'].is_finite(): fields['total_amount'] = None
        except (InvalidOperation,TypeError,ValueError): fields['total_amount'] = None
    signals = duplicate_signals(user, business, target_type, fields, attempt.file_sha256, invoice.pk)
    if target_type in {'purchase','expense'}:
        from expenses.duplicates import expense_duplicates
        from .views import invoice_duplicate_exists
        signals += expense_duplicates(user, invoice.invoice_number, invoice.supplier_gstin, fields['invoice_date'],
                                      fields['total_amount'], exclude=invoice.pk, file_hash=attempt.file_sha256)
        # Existing signal lists bound their examples; the hard identity check must not be truncated.
        if invoice.invoice_number.strip() and invoice.supplier_gstin and any(
                invoice_duplicate_exists(user,kind,invoice.invoice_number,invoice.supplier_gstin,exclude_pk=invoice.pk)
                for kind in ('purchase','expense')) and not any(s['kind']=='strong' for s in signals):
            signals.append({'kind':'strong','invoice_ids':[], 'reason':'An owned purchase/expense has the same normalized supplier GSTIN and invoice number.'})
    if target_type == 'sales':
        from .manual_services import number_exists
        if number_exists(user, invoice.invoice_number, invoice.pk) and not any(s['kind'] == 'document_identity' for s in signals):
            signals.append({'kind':'document_identity', 'invoice_ids':[], 'reason':'The normalized sales invoice number is already used.'})
    return signals


@transaction.atomic
def manage_provisional(*, request, invoice_id, operation, token, reason, confirm, target_type=None, acknowledge_duplicates=False):
    if operation not in {'change_type','restore'}:
        raise ValidationError('Unsupported provisional operation.')
    business = get_object_or_404(BusinessProfile.objects.select_for_update(), user=request.user)
    invoice = get_object_or_404(Invoice.objects.select_for_update(), pk=invoice_id, user=request.user)
    attempt = latest_attempt(invoice)
    errors = eligibility_errors(invoice, business, operation, attempt)
    if errors:
        raise ValidationError(errors)
    try:
        supplied = signing.loads(token, salt=TOKEN_SALT, max_age=3600)
        expected = signing.loads(state_token(invoice, business, operation, attempt), salt=TOKEN_SALT)
        if supplied != expected:
            raise ValueError
    except (signing.BadSignature, TypeError, ValueError):
        raise ValidationError('This action is stale, expired or belongs to another workspace. Reload before submitting again.') from None
    if not confirm or not reason or not reason.strip() or len(reason.strip()) > 500:
        raise ValidationError('Enter a reason (up to 500 characters) and explicitly confirm this action.')
    target_type = invoice.invoice_type if operation == 'restore' else target_type
    if target_type not in dict(Invoice.INVOICE_TYPES):
        raise ValidationError('Choose a supported invoice type.')
    if operation == 'change_type' and target_type == invoice.invoice_type:
        raise ValidationError('Choose a different invoice type; nothing was changed.')
    assert_original(attempt)
    signals = target_duplicates(request.user, business, invoice, target_type, attempt)
    if any(s['kind'] in {'exact_file','document_identity','strong','exact'} for s in signals):
        raise ValidationError('An owned invoice already conflicts with this source or target identity. Resolve the exact/strong duplicate; acknowledgement cannot override it.')
    if signals and not acknowledge_duplicates:
        raise ValidationError('Review and explicitly acknowledge the potential duplicate warnings before proceeding.')
    before = {'invoice_type':invoice.invoice_type, 'status':invoice.status}
    cancellation = activity(invoice).filter(action='cancel').first() if operation == 'restore' else None
    invoice.invoice_type = target_type
    invoice.status = 'review'
    invoice.save(update_fields=['invoice_type','status','updated_at'])
    record_event(request=request, business=business, action='update', module='invoices', obj=invoice,
                 description='Cancelled provisional invoice restored to Needs Review; no financial recognition.' if operation == 'restore' else 'Provisional invoice type corrected; original extraction evidence preserved.',
                 before=before, after={'invoice_type':invoice.invoice_type,'status':invoice.status},
                 metadata={'operation':operation,'correction_reason':reason.strip(),'attempt_id':attempt.pk,
                           'previous_invoice_type':before['invoice_type'],'resulting_invoice_type':invoice.invoice_type,
                           'previous_status':before['status'],'resulting_status':invoice.status,
                           'source_sha256':attempt.file_sha256,'cancellation_event_id':cancellation.pk if cancellation else None,
                           'duplicate_signals':signals,'duplicates_acknowledged':bool(acknowledge_duplicates)})
    return invoice


def management_context(invoice, business, attempt=None):
    if invoice.entry_mode != 'uploaded' or invoice.status not in {'review','cancelled'}:
        return {'can_change_invoice_type':False,'can_restore_invoice':False}
    attempt = attempt or latest_attempt(invoice)
    return {'can_change_invoice_type':not eligibility_errors(invoice,business,'change_type',attempt),
            'can_restore_invoice':not eligibility_errors(invoice,business,'restore',attempt)}
