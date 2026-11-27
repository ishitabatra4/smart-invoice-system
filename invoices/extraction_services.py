from .identity import normalized_identity
"""Existing upload/extraction orchestration with preserved originals and explicit review."""
import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import connection,DatabaseError,transaction
from django.db.models.functions import Lower,Trim
from django.utils import timezone
from accounts.models import BusinessProfile
from audit.services import record_event
from .models import Invoice,InvoiceLineItem,InvoiceExtractionAttempt as Attempt,InvoiceExtractionReview as Review
from .manual_calculations import money,calculate_invoice
from .manual_calculations import decimal
from .gstin import validate_document_gstins,validate_registration_state

MONEY=('taxable_amount','cgst_amount','sgst_amount','igst_amount','other_charges','total_amount')


def history_ready():
    try:return {Attempt._meta.db_table,Review._meta.db_table}<=set(connection.introspection.table_names())
    except DatabaseError:return False


def latest_attempt(invoice):
    return Attempt.objects.filter(invoice=invoice,user_id=invoice.user_id).first() if history_ready() else None


def serialized(value):return json.loads(json.dumps(value,default=str))


def duplicate_signals(user,business,invoice_type,fields,file_hash,exclude=None):
    from .views import normalize_invoice_number,normalize_gstin,normalize_party_name
    number=normalize_invoice_number(fields.get('invoice_number'));gst=normalize_gstin(fields.get('buyer_gstin') if invoice_type=='sales' else fields.get('supplier_gstin'))
    party_name=normalize_party_name(fields.get('buyer_name') if invoice_type=='sales' else fields.get('supplier_name'))
    qs=Invoice.objects.filter(user=user,invoice_type=invoice_type).exclude(pk=exclude)
    signals=[]
    exact=Attempt.objects.filter(user=user,business=business,file_sha256=file_hash).exclude(invoice_id=exclude) if exclude else Attempt.objects.filter(user=user,business=business,file_sha256=file_hash)
    ids=list(exact.exclude(invoice__isnull=True).order_by('invoice_id').values_list('invoice_id',flat=True).distinct()[:10])
    if ids:signals.append({'kind':'exact_file','invoice_ids':ids,'reason':'Same SHA-256 bytes were previously processed for this business.'})
    identity=[];potential=[]
    if number:
        qs=qs.annotate(number_key=normalized_identity('invoice_number')).filter(number_key=number.lower())
        for inv in qs[:100]:
            other_gst=normalize_gstin(inv.buyer_gstin if invoice_type=='sales' else inv.supplier_gstin)
            other_name=normalize_party_name(inv.buyer_name if invoice_type=='sales' else inv.supplier_name)
            if gst and gst==other_gst:identity.append(inv.pk)
            elif not gst and party_name and party_name==other_name:
                potential.append(inv.pk)
            elif not (gst and other_gst and gst!=other_gst) and not (party_name and other_name and party_name!=other_name) and fields.get('invoice_date') and fields.get('total_amount') is not None and str(inv.invoice_date)==str(fields['invoice_date']) and inv.total_amount==decimal(fields['total_amount']):potential.append(inv.pk)
    if identity:signals.append({'kind':'document_identity','invoice_ids':identity[:10],'reason':'Same party GSTIN, type and normalized invoice number; unique identity cannot be accepted twice.'})
    if potential:signals.append({'kind':'potential','invoice_ids':potential[:10],'reason':'Incomplete party identity with matching number/name or number/date/total; confirm before proceeding.'})
    return signals


def _provisional_money(value):
    try:return max(money(value),Decimal('0')) if value is not None else Decimal('0')
    except ValidationError:return Decimal('0')


def assert_original(attempt):
    try:
        digest=hashlib.sha256();size=0
        with attempt.source_file.open('rb') as source:
            for chunk in source.chunks():
                size+=len(chunk)
                if size>10*1024*1024:raise ValidationError('Original file exceeds its accepted size; review is blocked.')
                digest.update(chunk)
        if digest.hexdigest()!=attempt.file_sha256:raise ValidationError('Original file changed since extraction; no financial data was accepted.')
    except (OSError,ValueError):raise ValidationError('Original file is unavailable; financial acceptance is blocked.') from None


def process_upload(*,request,upload,invoice_type,business,batch,extractor):
    from .file_security import validate_document
    from .views import InvoiceFileIssue
    if not history_ready():raise InvoiceFileIssue('Advanced extraction requires the approved Phase 9 migration. Existing invoices and manual entry remain available.')
    try:validate_document(upload)
    except ValidationError as exc:
        record_event(request=request,business=business,action='security',module='security',description='Upload rejected by bounded document validation.',metadata={'category':'validation_rejected','invoice_type':invoice_type},success=False,event_type='FILE_UPLOAD_REJECTED')
        raise InvoiceFileIssue(' '.join(exc.messages)) from None
    original_name=upload.name;digest=hashlib.sha256()
    for chunk in upload.chunks():digest.update(chunk)
    upload.seek(0);suffix=original_name.rsplit('.',1)[-1].lower();upload.name=uuid.uuid4().hex+'.'+suffix
    attempt=Attempt.objects.create(user=request.user,business=business,batch=batch,source_file=upload,original_name=original_name[:255],file_sha256=digest.hexdigest(),status='processing',started_at=timezone.now(),result={'upload_invoice_type':invoice_type})
    try:return finish_attempt(request,attempt,invoice_type,extractor)
    except (DatabaseError,ValidationError):
        evidence=attempt.result
        attempt.refresh_from_db()
        if attempt.status=='processing':
            attempt.result=evidence;attempt.status='failed';attempt.error_category='persistence_failed';attempt.completed_at=timezone.now();attempt.save()
        raise InvoiceFileIssue('The original was retained in extraction history, but the result could not be saved. Review it manually.') from None


def finish_attempt(request,attempt,invoice_type,extractor):
    from .views import InvoiceFileIssue,normalize_invoice_number,normalize_gstin
    try:
        with attempt.source_file.open('rb') as source:
            from django.core.files import File
            # The extractor receives a private temporary path, not a user-provided URL.
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory(prefix='invoice-extraction-') as directory:
                path=Path(directory)/('source'+Path(attempt.source_file.name).suffix)
                with path.open('wb') as output:
                    for chunk in source.chunks():output.write(chunk)
                data=extractor(str(path))
        state='review';category=''
        failures=[page.get('error') for page in data.get('pages',[]) if page.get('error')]
        if failures:state='partial' if any(p.get('text') for p in data.get('pages',[])) else 'failed';category=failures[0]
        elif data.get('pipeline_version')==9 and not (data.get('extracted_text') or '').strip():state='failed';category='no_text'
    except Exception as exc:
        from .extraction import OCRUnavailableError
        state='failed';category='ocr_unavailable' if isinstance(exc,OCRUnavailableError) else 'processing_failed'
        data={'pipeline_version':9,'validation_errors':['Extraction failed safely; the original is retained for manual review or an explicit retry.'],'confidence':{},'pages':[],'line_items':[]}
    data=serialized(data);attempt.method=data.get('document_type','unknown')
    attempt.result={k:v for k,v in data.items() if k!='extracted_text'}
    attempt.result['upload_invoice_type']=invoice_type
    attempt.error_category=category
    attempt.duplicate_signals=duplicate_signals(request.user,attempt.business,invoice_type,data,attempt.file_sha256,attempt.invoice_id)
    number=normalize_invoice_number(data.get('invoice_number'));supplier=normalize_gstin(data.get('supplier_gstin'));buyer=normalize_gstin(data.get('buyer_gstin'))
    from .manual_services import number_exists
    hard=any(s['kind']=='document_identity' for s in attempt.duplicate_signals) or (invoice_type=='sales' and number and number_exists(request.user,number,attempt.invoice_id))
    if hard and attempt.invoice_id is None:
        attempt.status='blocked';attempt.completed_at=timezone.now();attempt.save()
        record_event(request=request,business=attempt.business,action='create',module='extraction',description='Duplicate upload retained for explicit review; no invoice merged or accepted.',obj=attempt)
        raise InvoiceFileIssue('Duplicate invoice: this document identity already exists. The original upload was retained in extraction history for review.')
    try:day=date.fromisoformat(data.get('invoice_date') or '')
    except (TypeError,ValueError):
        # Required storage only: date_evidence never presents this as an extracted/reviewed date.
        day=timezone.localdate()
    with transaction.atomic():
        BusinessProfile.objects.select_for_update().get(pk=attempt.business_id,user=request.user)
        invoice=Invoice.objects.select_for_update().get(pk=attempt.invoice_id,user=request.user) if attempt.invoice_id else Invoice(user=request.user,batch=attempt.batch,invoice_type=invoice_type,entry_mode='uploaded')
        if attempt.invoice_id and Attempt.objects.filter(invoice=invoice).first().pk!=attempt.pk:
            raise ValidationError('A newer extraction superseded this result; the invoice was not changed.')
        invoice.invoice_number=number;invoice.invoice_date=day
        if not number:supplier=buyer=''
        invoice.calculation_version=None;invoice.treatment_breakdown=None;invoice.business_snapshot={};invoice.party_snapshot={};invoice.supplier_party=None;invoice.buyer_party=None
        invoice.supplier_name=(data.get('supplier_name') or '')[:255];invoice.supplier_gstin=supplier if len(supplier)<=15 else ''
        invoice.buyer_name=(data.get('buyer_name') or '')[:255];invoice.buyer_gstin=buyer if len(buyer)<=15 else ''
        for field in MONEY:setattr(invoice,field,_provisional_money(data.get(field)))
        invoice.extracted_text=(data.get('extracted_text') or '')[:150000]
        invoice.validation_errors=list(data.get('validation_errors') or [])+['Extraction is provisional. Confirm the reviewed data, then explicitly verify it.']
        invoice.status='review';invoice.source_file.name=attempt.source_file.name;invoice.save()
        # Provisional table rows are evidence, not accepted accounting lines.
        invoice.line_items.all().delete()
        for row in data.get('line_items',[])[:100]:
            try:quantity=decimal(row.get('quantity') or 1)
            except ValidationError:quantity=Decimal('1')
            if quantity>Decimal('999999999.999') or quantity!=quantity.quantize(Decimal('.001')):quantity=Decimal('1')
            InvoiceLineItem.objects.create(invoice=invoice,description=str(row.get('description') or '')[:500],hsn_sac_code=str(row.get('hsn_sac_code') or '')[:20],unit=str(row.get('unit') or '')[:30],quantity=quantity if quantity>0 else 1,
                **{field:(_provisional_money(row.get(field)) if field!='tax_rate' else min(_provisional_money(row.get(field)),Decimal('100'))) for field in ('rate','tax_rate','taxable_amount','tax_amount','total_amount')})
        attempt.invoice=invoice;attempt.status=state;attempt.completed_at=timezone.now();attempt.save()
        record_event(request=request,business=attempt.business,action='create',module='invoices',description='Invoice upload retained for mandatory review; no financial recognition.',obj=invoice)
    warnings=['potential_duplicate'] if attempt.duplicate_signals else []
    return invoice,warnings


def invoice_snapshot(invoice, *, lines=None):
    fields=('invoice_number','invoice_date','due_date','supplier_name','supplier_gstin','buyer_name','buyer_gstin',*MONEY,'pricing_mode','supply_category','supplier_state_code','place_of_supply','invoice_discount_type','invoice_discount_value','calculation_version','business_snapshot','party_snapshot','treatment_breakdown')
    line_fields = ('description','hsn_sac_code','quantity','unit','rate','discount','discount_type','discount_value','tax_rate','tax_treatment','invoice_discount_allocated','taxable_amount','cgst_amount','sgst_amount','igst_amount','tax_amount','total_amount')
    values = list(invoice.line_items.values(*line_fields)) if lines is None else [{name:getattr(line,name) for name in line_fields} for line in sorted(lines,key=lambda line:line.pk)]
    return serialized({'header':{k:getattr(invoice,k) for k in fields},'lines':values})


def extraction_notice_context(invoice,attempt,review=None):
    """Read-only current assessment, separate from immutable extraction history."""
    from .extraction import financial_reconciliation,line_financial_reconciliation
    historical=list(attempt.result.get('validation_errors') or [])
    current=[]
    snapshot_matches=bool(invoice and review and review.attempt_id==attempt.pk and review.snapshot.get('invoice')==invoice_snapshot(invoice))
    if invoice is None or invoice.status=='review':
        if review and review.attempt_id==attempt.pk:
            current=['Confirmed draft still requires separate explicit financial verification.']
            if not snapshot_matches:
                current.append('The confirmed snapshot is stale; review and confirm the latest data before verification.')
        else:
            stored=invoice.validation_errors or [] if invoice else []
            current=[*stored,*historical,*financial_reconciliation(attempt.result)['warnings'],*line_financial_reconciliation(attempt.result)['warnings'],
                'Extraction is provisional. Confirm the reviewed data, then explicitly verify it.']
    from .classification import classification_assessment
    from .management_services import management_context, target_duplicates
    classification_fields = review.snapshot['invoice']['header'] if snapshot_matches else attempt.result
    return {'invoice':invoice,
        'current_duplicate_signals':target_duplicates(attempt.user,attempt.business,invoice,invoice.invoice_type,attempt) if invoice and invoice.status=='review' else attempt.duplicate_signals if not invoice else [],
        'classification':classification_assessment(attempt.business,classification_fields,invoice.invoice_type if invoice else attempt.result.get('upload_invoice_type','purchase')) if not invoice or invoice.status not in {'verified','paid'} else None,
        **(management_context(invoice,attempt.business,attempt) if invoice else {}),
        'current_notices':list(dict.fromkeys(current)),
        'extracted_invoice_date':attempt.result.get('invoice_date'),
        'extracted_invoice_confidence':attempt.result.get('confidence',{}).get('invoice_date',{}),
        'date_invoice':invoice,
        'date_uploaded_at':invoice.created_at if invoice else attempt.created_at,
        'extracted_due_date':attempt.result.get('due_date'),
        'extracted_due_confidence':attempt.result.get('confidence',{}).get('due_date',{}),
        'saved_due_date':invoice.due_date if invoice else None,
        'due_date_confirmed':snapshot_matches,
        'stored_notice_count':len(invoice.validation_errors or []) if invoice else 0,
        'historical_warning_count':len(historical)}


def validate_review_due_date(attempt,data):
    candidate=attempt.result.get('due_date')
    if candidate and not data.get('due_date') and not data.get('clear_due_date_confirmed'):
        raise ValidationError(f'Extracted due date is {candidate}. Restore it or explicitly acknowledge leaving it blank after reviewing the original.')


def validate_review_dates(data):
    if not data.get('invoice_date'):raise ValidationError('A valid reviewed invoice date is required; an upload/storage date is not source evidence.')
    day=Invoice._meta.get_field('invoice_date').clean(data['invoice_date'],None)
    due=Invoice._meta.get_field('due_date').clean(data.get('due_date'),None)
    if due and due<day:raise ValidationError('Due date cannot precede invoice date.')


def calculate_review(data,rows):
    if data['other_charges']!=0:raise ValidationError('Classify additional charges as reviewed line items; unclassified header charges cannot be accepted automatically.')
    result=calculate_invoice(rows,supplier_state=data['supplier_state_code'],place_of_supply=data['place_of_supply'],pricing_mode=data['pricing_mode'],supply_category=data['supply_category'],invoice_discount_type=data['invoice_discount_type'],invoice_discount=data['invoice_discount_value'],adjustment=data['rounding_adjustment'])
    for field in MONEY:
        entered=data['rounding_adjustment'] if field=='other_charges' else data[field]
        if result[field]!=entered:raise ValidationError(f'{field.replace("_"," ").title()} differs from the server-calculated value {result[field]}. Correct the inputs or the extracted amount; it was not silently replaced.')
    return result


@transaction.atomic
def save_review(*,request,attempt_id,data,rows,key,previous_review):
    business=BusinessProfile.objects.select_for_update().get(user=request.user)
    attempt=Attempt.objects.select_for_update().get(pk=attempt_id,user=request.user,business=business)
    if not data.get('confirm'):raise ValidationError('Explicit human confirmation is required.')
    validate_review_dates(data)
    validate_review_due_date(attempt,data)
    assert_original(attempt)
    existing=Review.objects.filter(review_key=key,user=request.user,attempt=attempt).first()
    if existing:return existing.invoice
    if attempt.status in {'uploaded','processing'}:raise ValidationError('Extraction is still processing; wait before reviewing.')
    if attempt.result.get('supply_category') in {'reverse_charge','export','sez'}:raise ValidationError('This supply category requires manual tax review; automatic extraction acceptance is unsupported.')
    if attempt.result.get('detected_document_kind') in {'credit note','debit note','quotation','proforma invoice'}:raise ValidationError('This is not an accounting invoice. Use its dedicated document workflow instead.')
    invoice=Invoice.objects.select_for_update().get(pk=attempt.invoice_id,user=request.user) if attempt.invoice_id else Invoice(user=request.user,batch=attempt.batch,invoice_type=data['invoice_type'],entry_mode='uploaded')
    if invoice.invoice_type in {'purchase','expense'}:
        from expenses.duplicates import assert_expense_identity
        assert_expense_identity(request.user,invoice,data,attempt.file_sha256,data.get('acknowledge_duplicates'))
    if invoice.pk and (invoice.status!='review' or latest_attempt(invoice).pk!=attempt.pk):raise ValidationError('Only the latest unverified extraction may be reviewed.')
    latest=Review.objects.filter(invoice=invoice).first() if invoice.pk else None
    if (latest.pk if latest else None)!=previous_review:raise ValidationError('A newer review exists. Reload before changing it.')
    result=calculate_review(data,rows)
    validate_document_gstins(data['supplier_gstin'],data['buyer_gstin'],data['supplier_state_code'],result['tax_amount'])
    validate_registration_state(business.gstin,business.state,'Business profile')
    own=data['supplier_gstin'] if invoice.invoice_type=='sales' else data['buyer_gstin']
    if own and business.gstin and own!=business.gstin:raise ValidationError('The invoice identifies another business as your supplier/buyer role; review its classification.')
    signals=duplicate_signals(request.user,business,invoice.invoice_type,data,attempt.file_sha256,invoice.pk)
    from .manual_services import number_exists
    if any(s['kind']=='document_identity' for s in signals) or (invoice.invoice_type=='sales' and number_exists(request.user,data['invoice_number'],invoice.pk)):
        raise ValidationError('This invoice identity already exists. Correct it or retain the duplicate without verification.')
    if signals and not data['acknowledge_duplicates']:raise ValidationError('Review and explicitly acknowledge the owned duplicate signals before saving.')
    party=data.get('party')
    if invoice.invoice_type=='sales' and not data.get('buyer_name','').strip():raise ValidationError('Buyer name is required for a sales invoice.')
    opposite=data['buyer_gstin'] if invoice.invoice_type=='sales' else data['supplier_gstin']
    if business.gstin and opposite==business.gstin and own!=business.gstin:raise ValidationError('Your business appears in the opposite invoice role; review sales/purchase classification.')
    if party:
        valid={'customer','both'} if invoice.invoice_type=='sales' else {'vendor','both'}
        if party.business_id!=business.pk or party.party_type not in valid or not party.is_active:raise ValidationError('Choose an active owned party for this invoice role.')
        validate_registration_state(party.gstin,party.state,'Selected party')
        prefix='buyer' if invoice.invoice_type=='sales' else 'supplier'
        if data[prefix+'_gstin']!=party.gstin or data[prefix+'_name']!=party.name:raise ValidationError('Reviewed party name/GSTIN must match the selected directory entry; no hidden substitution was performed.')
    for field in ('invoice_number','invoice_date','due_date','supplier_name','supplier_gstin','buyer_name','buyer_gstin','pricing_mode','supply_category','supplier_state_code','place_of_supply','invoice_discount_type','invoice_discount_value'):setattr(invoice,field,data[field])
    for field in MONEY:setattr(invoice,field,result[field])
    for field in ('invoice_discount_amount','total_discount_amount'):setattr(invoice,field,result[field])
    invoice.calculation_version=1;invoice.treatment_breakdown={k:str(v) for k,v in result['treatment_breakdown'].items()}
    invoice.buyer_party=party if invoice.invoice_type=='sales' else None;invoice.supplier_party=party if invoice.invoice_type!='sales' else None
    # The uploaded identities are authoritative; never replace with current directory text.
    supplier_snapshot={'name':data['supplier_name'],'gstin':data['supplier_gstin'],'address_line':data.get('supplier_address','')}
    buyer_snapshot={'name':data['buyer_name'],'gstin':data['buyer_gstin'],'address_line':data.get('buyer_address','')}
    invoice.business_snapshot=supplier_snapshot if invoice.invoice_type=='sales' else buyer_snapshot
    invoice.party_snapshot=buyer_snapshot if invoice.invoice_type=='sales' else supplier_snapshot
    invoice.validation_errors=[];invoice.status='review';invoice.source_file.name=attempt.source_file.name;invoice.full_clean();invoice.save();invoice.line_items.all().delete()
    line_fields=('discount','invoice_discount_allocated','taxable_amount','tax_amount','cgst_amount','sgst_amount','igst_amount','total_amount')
    for values,calculated in zip(rows,result['lines']):
        line=InvoiceLineItem(invoice=invoice,**{k:values.get(k,'') for k in ('description','hsn_sac_code','quantity','unit','rate','tax_rate','tax_treatment','discount_type')},discount_value=values['discount'],**{k:calculated[k] for k in line_fields});line.full_clean();line.save()
    if attempt.invoice_id is None:attempt.invoice=invoice;attempt.save(update_fields=['invoice'])
    invoice.refresh_from_db()
    review=Review(user=request.user,business=business,invoice=invoice,attempt=attempt,review_key=key,snapshot={'invoice':invoice_snapshot(invoice),'duplicate_signals':signals,'duplicates_acknowledged':bool(data['acknowledge_duplicates']),'due_date_clear_confirmed':bool(data.get('clear_due_date_confirmed'))});review.full_clean();review.save()
    record_event(request=request,business=business,action='update',module='extraction',description='Human-confirmed extraction saved; invoice still requires separate verification.',obj=invoice,metadata={'attempt_id':attempt.pk,'review_id':review.pk})
    return invoice


@transaction.atomic
def verify_review(request,invoice):
    business=BusinessProfile.objects.select_for_update().get(user=request.user)
    invoice=Invoice.objects.select_for_update().get(pk=invoice.pk,user=request.user)
    attempt=latest_attempt(invoice);assert_original(attempt);review=Review.objects.filter(invoice=invoice,attempt=attempt,user=request.user).first()
    if request.POST.get('confirm')!='on':raise ValidationError('Explicit confirmation is required before financial verification.')
    if invoice.status!='review' or review is None or review.snapshot.get('invoice')!=invoice_snapshot(invoice):raise ValidationError('Confirm the latest reviewed data before verifying; stale or altered records cannot be accepted.')
    validate_review_dates({'invoice_date':invoice.invoice_date,'due_date':invoice.due_date})
    current_signals=duplicate_signals(request.user,business,invoice.invoice_type,{k:getattr(invoice,k) for k in ('invoice_number','invoice_date','total_amount','supplier_name','supplier_gstin','buyer_name','buyer_gstin')},attempt.file_sha256,invoice.pk)
    if current_signals!=review.snapshot.get('duplicate_signals'):raise ValidationError('Duplicate signals changed since review; review and confirm again.')
    if attempt.status=='processing':raise ValidationError('Extraction is still processing.')
    if invoice.invoice_type in {'purchase','expense'}:
        from expenses.duplicates import assert_expense_identity
        assert_expense_identity(request.user,invoice,{k:getattr(invoice,k) for k in ('invoice_number','supplier_gstin','invoice_date','total_amount')},attempt.file_sha256,review.snapshot.get('duplicates_acknowledged'))
    data={k:getattr(invoice,k) for k in ('supplier_state_code','place_of_supply','pricing_mode','supply_category','invoice_discount_type','invoice_discount_value',*MONEY)}
    data.update(other_charges=Decimal('0'),rounding_adjustment=invoice.other_charges)
    rows=[{**line,'discount':line['discount_value']} for line in invoice.line_items.values('description','quantity','rate','tax_rate','tax_treatment','discount_value','discount_type')]
    calculate_review(data,rows)
    validate_document_gstins(invoice.supplier_gstin,invoice.buyer_gstin,invoice.supplier_state_code,invoice.total_tax)
    validate_registration_state(business.gstin,business.state,'Business profile')
    party=invoice.buyer_party if invoice.invoice_type=='sales' else invoice.supplier_party
    if party:validate_registration_state(party.gstin,party.state,'Selected party')
    invoice.status='verified';invoice.save(update_fields=['status','updated_at'])
    record_event(request=request,business=business,action='verify',module='invoices',description='Explicitly reviewed uploaded invoice verified.',obj=invoice,metadata={'attempt_id':attempt.pk,'review_id':review.pk})
    return invoice
