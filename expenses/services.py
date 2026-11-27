"""Expense metadata and review; the existing Invoice remains the only financial record."""
import hashlib
import json
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import connection,transaction
from django.db.models import Q
from accounts.models import BusinessProfile
from audit.services import record_event
from invoices.models import Invoice
from invoices.gstin import gstin_evidence
from invoices.extraction_services import invoice_snapshot,serialized,calculate_review,MONEY
from .models import ExpenseDetail,ExpenseITCReview,ExpenseAttachmentRevision,private_expense_storage
ZERO=Decimal('0.00')


def advanced_ready():
    return {ExpenseITCReview._meta.db_table,ExpenseAttachmentRevision._meta.db_table}<=set(connection.introspection.table_names())


def managed_invoices(user):
    return Invoice.objects.filter(user=user).filter(Q(invoice_type='expense')|Q(invoice_type='purchase',expense_detail__isnull=False))


def source_evidence(invoice, *, context=None):
    if context is not None and context['business'].user_id != invoice.user_id:
        raise ValidationError('Expense evidence must belong to this business.')
    lines = getattr(invoice, '_prefetched_objects_cache', {}).get('line_items') if context is not None else None
    evidence=invoice_snapshot(invoice, lines=lines)
    evidence['invoice_type']=invoice.invoice_type
    evidence['status']='recognized' if invoice.status in {'verified','paid'} else invoice.status
    cached = getattr(invoice,'_prefetched_objects_cache',{}).get('adjustment_notes') if context is not None else None
    evidence['adjustments'] = ([{'pk':note.pk,'kind':note.kind,'total_amount':note.total_amount} for note in cached if note.finalized_at is not None] if cached is not None else list(invoice.adjustment_notes.filter(finalized_at__isnull=False).values('pk','kind','total_amount')))
    detail=getattr(invoice,'expense_detail',None)
    field=invoice.source_file or (detail.attachment if detail else None)
    evidence['document_name']=field.name if field else ''
    evidence['document_sha256']=''
    if field:
        cache = context['documents'] if context is not None else {}
        key = (id(field.storage), field.name)
        if key not in cache:
            try:
                digest=hashlib.sha256();size=0
                with field.open('rb') as source:
                    for chunk in source.chunks():
                        size += len(chunk)
                        if size > 10*1024*1024:raise OSError
                        if context is not None:
                            context['bytes_read'] += len(chunk)
                            if context['bytes_read'] > 64*1024*1024:
                                raise ValidationError('Narrow dates or filters: receipt evidence exceeds the 64 MB interactive read budget.')
                        digest.update(chunk)
                cache[key]=digest.hexdigest()
            except OSError:cache[key]='unavailable'
        evidence['document_sha256']=cache[key]
    evidence['supplier_gstin']=invoice.supplier_gstin
    evidence['business_gstin']=context['business'].gstin if context is not None else BusinessProfile.objects.get(user_id=invoice.user_id).gstin
    return serialized(evidence)


def fingerprint(evidence):return hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()


def itc_state(invoice, *, context=None):
    available = context['available'] if context is not None else advanced_ready()
    if not available:return {'status':'pending','amount':ZERO,'reason':'ITC review requires the approved expense migration.'}
    prefetched = getattr(invoice, '_latest_itc_reviews', None) if context is not None else None
    review = (prefetched[0] if prefetched else None) if prefetched is not None else ExpenseITCReview.objects.filter(invoice=invoice,business__user_id=invoice.user_id).first()
    if not review:return {'status':'pending','amount':ZERO,'reason':'No owner eligibility review. Saved GST is not a claim.'}
    stale=review.fingerprint!=fingerprint(source_evidence(invoice, context=context))
    return {'status':'pending' if stale else review.status,'amount':invoice.total_tax if not stale and review.status in {'confirmed','potential'} and invoice.status in {'verified','paid'} else ZERO,'reason':'Source changed; review again.' if stale else review.reason,'review':review}


@transaction.atomic
def review_itc(request,invoice,status,reason,attested):
    if not advanced_ready():raise ValidationError('Expense migration approval is required.')
    business=BusinessProfile.objects.select_for_update().get(user=request.user)
    invoice=managed_invoices(request.user).select_for_update().get(pk=invoice.pk)
    if not reason.strip():raise ValidationError('Explain the eligibility decision.')
    if status not in dict(ExpenseITCReview.STATUSES):raise ValidationError('Choose a review status.')
    evidence=source_evidence(invoice);previous=itc_state(invoice)
    if status in {'potential','confirmed'}:
        if invoice.status not in {'verified','paid'} or invoice.calculation_version!=1 or invoice.supply_category!='standard' or invoice.total_tax<=0 or invoice.total_mismatch:raise ValidationError('A verified, reconciled standard GST invoice is required. No automatic eligibility decision is supported.')
        if invoice.buyer_gstin and invoice.buyer_gstin!=business.gstin:raise ValidationError('The tax document identifies another buyer GSTIN; review ownership before ITC preparation.')
        if not gstin_evidence(invoice.supplier_gstin)['checksum_valid'] or not gstin_evidence(business.gstin or '')['checksum_valid']:raise ValidationError('Valid supplier and business GSTIN checksums are required; registration remains a manual check.')
        detail=getattr(invoice,'expense_detail',None)
        if not invoice.source_file and not (detail and detail.attachment):raise ValidationError('Supporting tax document is required.')
        if evidence['document_sha256']=='unavailable':raise ValidationError('Supporting document is unavailable or exceeds the accepted limit.')
        data={k:getattr(invoice,k) for k in ('supplier_state_code','place_of_supply','pricing_mode','supply_category','invoice_discount_type','invoice_discount_value',*MONEY)}
        data.update(other_charges=ZERO,rounding_adjustment=invoice.other_charges)
        rows=[{**row,'discount':row['discount_value']} for row in invoice.line_items.values('description','quantity','rate','tax_rate','tax_treatment','discount_value','discount_type')]
        calculate_review(data,rows)
        if evidence['adjustments']:raise ValidationError('Adjusted invoices require specialist review; automatic reviewed-ITC totals are unsupported.')
        if not attested:raise ValidationError('Confirm that registration, documentation, receipt, business use, blocked-credit restrictions and applicable conditions were reviewed independently.')
    prior=ExpenseITCReview.objects.filter(invoice=invoice,business=business).first()
    if prior and prior.status==status and prior.reason==reason.strip() and prior.fingerprint==fingerprint(evidence):return prior
    if status=='reversed' and (not prior or prior.status!='confirmed'):raise ValidationError('Only a previously confirmed review can be reversed, including a stale source requiring reversal.')
    record=ExpenseITCReview.objects.create(invoice=invoice,business=business,reviewer=request.user,status=status,reason=reason.strip(),fingerprint=fingerprint(evidence),evidence=evidence)
    record_event(request=request,business=business,action='update',module='expenses',description='ITC preparation review recorded; no claim or filing.',obj=invoice,before={'itc_status':previous['status']},after={'itc_status':status},metadata={'review_id':record.pk})
    return record


def remember_attachment(request,invoice,field,kind,display=''):
    if not field or not advanced_ready():return
    digest=hashlib.sha256()
    try:
        with field.open('rb') as source:
            for chunk in source.chunks():digest.update(chunk)
    except OSError:raise ValidationError('Receipt is unavailable; original history must be preserved.') from None
    ExpenseAttachmentRevision.objects.get_or_create(invoice=invoice,storage_kind=kind,file_name=field.name,defaults={'actor':request.user,'sha256':digest.hexdigest(),'display_name':display or field.name.rsplit('/',1)[-1]})


@transaction.atomic
def save_metadata(request,invoice,data):
    BusinessProfile.objects.select_for_update().get(user=request.user)
    invoice=managed_invoices(request.user).select_for_update().get(pk=invoice.pk)
    if invoice.status=='cancelled':raise ValidationError('Cancelled records are immutable.')
    detail=getattr(invoice,'expense_detail',None)
    if detail is None:detail=ExpenseDetail(invoice=invoice)
    before={'title':detail.title,'category_id':detail.category_id}
    if detail.attachment:remember_attachment(request,invoice,detail.attachment,'private',detail.attachment_display_name)
    if invoice.source_file:remember_attachment(request,invoice,invoice.source_file,'invoice')
    if invoice.calculation_version==1:detail.gst_inclusive=invoice.pricing_mode=='inclusive'
    detail.title=data['title'];detail.category=data['category'];detail.description=data.get('description','');detail.notes=data.get('notes','');detail.reference_number=invoice.invoice_number
    if data.get('attachment'):
        detail.attachment=data['attachment'];detail.attachment_display_name=data['attachment'].name[:255]
    elif data.get('remove_attachment'):detail.attachment='';detail.attachment_display_name=''
    detail.full_clean();detail.save()
    if detail.attachment:remember_attachment(request,invoice,detail.attachment,'private',detail.attachment_display_name)
    record_event(request=request,action='update',module='expenses',description='Expense metadata updated; financial values preserved.',obj=invoice,before=before,after={'title':detail.title,'category_id':detail.category_id},metadata={'receipt_changed':bool(data.get('attachment') or data.get('remove_attachment'))})
    return detail


@transaction.atomic
def link_purchase(request,invoice,category,confirm):
    BusinessProfile.objects.select_for_update().get(user=request.user)
    invoice=Invoice.objects.select_for_update().get(pk=invoice.pk,user=request.user,invoice_type='purchase')
    if invoice.status not in {'verified','paid'} or not confirm:raise ValidationError('Explicitly confirm a verified purchase. No new financial record will be created.')
    if category.business.user_id!=request.user.pk:raise ValidationError('Choose an owned category.')
    from .duplicates import expense_duplicates
    signals=expense_duplicates(request.user,invoice.invoice_number,invoice.supplier_gstin,invoice.invoice_date,invoice.total_amount,exclude=invoice.pk)
    if signals:raise ValidationError('A separate expense may already represent this purchase. Review it before linking; no automatic merge is supported.')
    detail,created=ExpenseDetail.objects.get_or_create(invoice=invoice,defaults={'category':category,'title':invoice.supplier_name or invoice.invoice_number,'reference_number':invoice.invoice_number,'gst_inclusive':invoice.pricing_mode=='inclusive'})
    if created:record_event(request=request,action='create',module='expenses',description='Purchase linked to expense register; existing purchase is authoritative.',obj=invoice,after={'purchase_invoice_id':invoice.pk,'category_id':category.pk})
    return detail
