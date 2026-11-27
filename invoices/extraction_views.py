from datetime import timedelta
import uuid
import hashlib
from pathlib import Path
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse,FileResponse,Http404
from django.shortcuts import get_object_or_404,redirect,render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET,require_POST,require_http_methods
from django.db import transaction
from django.utils import timezone
from .models import Invoice,InvoiceExtractionAttempt as Attempt,InvoiceExtractionReview as Review
from .extraction_services import history_ready,latest_attempt,save_review,calculate_review,finish_attempt,serialized,extraction_notice_context
from .extraction_forms import ExtractionReviewForm,ExtractionLineSet,REVIEW_SALT
from .manual_calculations import calculate_invoice


def owned(request,pk):
    if not history_ready():raise Http404('Extraction review requires the approved migration.')
    return get_object_or_404(Attempt.objects.select_related('invoice','business'),pk=pk,user=request.user,business__user=request.user)


@login_required
@require_GET
def extraction_history(request):
    records=Attempt.objects.filter(user=request.user,business__user=request.user) if history_ready() else Attempt.objects.none()
    return render(request,'invoices/extraction_history.html',{'page':Paginator(records,25).get_page(request.GET.get('page')),'ready':history_ready()})


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def extraction_review(request,attempt_id):
    attempt=owned(request,attempt_id)
    if attempt.invoice_id and (attempt.invoice.status!='review' or latest_attempt(attempt.invoice).pk!=attempt.pk):return HttpResponse('Only the latest unverified extraction can be reviewed.',status=400)
    if attempt.status in {'uploaded','processing'}:return HttpResponse('Extraction is processing. Do not submit a review yet.',status=409)
    prior=Review.objects.filter(invoice=attempt.invoice,user=request.user).first() if attempt.invoice_id else None
    initial={key:value for key,value in attempt.result.items() if key in ExtractionReviewForm.base_fields}
    rows=attempt.result.get('line_items',[])
    if prior and prior.attempt_id==attempt.pk:
        header=prior.snapshot['invoice']['header'];initial.update(header,other_charges='0',rounding_adjustment=header['other_charges'])
        initial['clear_due_date_confirmed']=prior.snapshot.get('due_date_clear_confirmed',False)
        rows=[{**row,'discount':row['discount_value']} for row in prior.snapshot['invoice']['lines']]
    from .management_services import classification_revision
    initial['token']=signing.dumps({'user':request.user.pk,'attempt':attempt.pk,'previous':prior.pk if prior else None,'key':str(uuid.uuid4()),'classification_revision':classification_revision(attempt.invoice) if attempt.invoice_id else 0},salt=REVIEW_SALT)
    for row in rows:
        row.setdefault('discount_type','fixed')
    form=ExtractionReviewForm(request.POST if request.method=='POST' else None,user=request.user,attempt=attempt,initial=initial)
    lines=ExtractionLineSet(request.POST if request.method=='POST' else None,initial=rows,prefix='items',form_kwargs={'pricing_mode':request.POST.get('pricing_mode','exclusive') if request.method=='POST' else initial.get('pricing_mode') or 'exclusive'})
    calculated=None
    if request.method=='POST':
        header_valid=form.is_valid();line_valid=lines.is_valid()
        if header_valid and line_valid:
            data=form.cleaned_data;data['invoice_type']=attempt.invoice.invoice_type if attempt.invoice_id else attempt.result.get('upload_invoice_type','purchase')
            values=[f.cleaned_data for f in lines if f.cleaned_data and not f.cleaned_data.get('DELETE')]
            try:
                # Preview shows the authoritative alternative but does not overwrite entered amounts.
                calculated=calculate_invoice(values,supplier_state=data['supplier_state_code'],place_of_supply=data['place_of_supply'],pricing_mode=data['pricing_mode'],supply_category=data['supply_category'],invoice_discount_type=data['invoice_discount_type'],invoice_discount=data['invoice_discount_value'],adjustment=data['rounding_adjustment'])
                if 'preview' not in request.POST:
                    invoice=save_review(request=request,attempt_id=attempt.pk,data=data,rows=values,key=form.review_key,previous_review=form.previous_review)
                    messages.success(request,'Confirmed draft saved. No revenue, GST liability or payment was created. Verify separately when ready.')
                    return redirect('invoice_detail',invoice_id=invoice.pk)
            except ValidationError as exc:form.add_error(None,exc)
    return render(request,'invoices/extraction_review.html',{'attempt':attempt,'form':form,'lines':lines,'confidence_rows':[{'label':key.replace('_',' ').title(),'value':(str(len(attempt.result.get('line_items',[])))+' candidate lines; review the editable rows above.' if key=='line_items' else attempt.result.get(key)),**value} for key,value in attempt.result.get('confidence',{}).items()],'calculated':calculated,
        **extraction_notice_context(attempt.invoice,attempt,prior)})


@login_required
@never_cache
@require_GET
def extraction_source(request,attempt_id):
    attempt=owned(request,attempt_id)
    try:handle=attempt.source_file.open('rb')
    except (OSError,ValueError):raise Http404('Original source is unavailable.') from None
    ext=Path(attempt.source_file.name).suffix.lower();mime={'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg'}.get(ext)
    if not mime:handle.close();raise Http404
    response=FileResponse(handle,as_attachment=request.GET.get('download')=='1',filename='invoice-source'+ext,content_type=mime)
    response['X-Content-Type-Options']='nosniff';response['Content-Security-Policy']="sandbox; frame-ancestors 'self'";return response


@login_required
@require_POST
def extraction_retry(request,attempt_id):
    attempt=owned(request,attempt_id)
    if request.POST.get('confirm')!='on':return HttpResponse('Explicit retry confirmation required.',status=400)
    with transaction.atomic():
        if not attempt.invoice_id:
            pending=Attempt.objects.select_for_update().get(pk=attempt.pk,user=request.user)
            if pending.status=='processing' and pending.started_at and pending.started_at<timezone.now()-timedelta(seconds=105):
                pending.status='failed';pending.error_category='interrupted';pending.completed_at=timezone.now();pending.save()
                return redirect('extraction_review',attempt_id=pending.pk)
            return HttpResponse('Review the retained upload; active processing or duplicate identity cannot be retried here.',status=409 if pending.status=='processing' else 400)
        invoice=Invoice.objects.select_for_update().get(pk=attempt.invoice_id,user=request.user)
        if invoice.status=='processing':
            active=latest_attempt(invoice)
            if active and active.started_at and active.started_at < timezone.now()-timedelta(seconds=105):
                active.status='failed';active.error_category='interrupted';active.completed_at=timezone.now();active.save()
                invoice.status='review';invoice.save(update_fields=['status','updated_at'])
            else:return HttpResponse('Extraction is still processing.',status=409)
        if invoice.status!='review':return HttpResponse('Only unverified review invoices can be retried.',status=400)
        if Attempt.objects.filter(invoice=invoice,created_at__gte=timezone.now()-timedelta(hours=1)).count()>=3:return HttpResponse('At most three extraction attempts per invoice per hour.',status=429)
        if latest_attempt(invoice).pk!=attempt.pk:return HttpResponse('Reload the latest extraction.',status=409)
        with attempt.source_file.open('rb') as source:digest=hashlib.sha256(source.read(10*1024*1024+1)).hexdigest()
        if digest!=attempt.file_sha256:return HttpResponse('Original file hash changed; do not retry.',status=409)
        invoice.status='processing';invoice.save(update_fields=['status','updated_at'])
        next_attempt=Attempt.objects.create(user=request.user,business=attempt.business,batch=attempt.batch,invoice=invoice,source_file=attempt.source_file.name,original_name=attempt.original_name,file_sha256=attempt.file_sha256,status='processing',started_at=timezone.now())
    from .extraction import extract_invoice_data
    finish_attempt(request,next_attempt,invoice.invoice_type,extract_invoice_data)
    return redirect('extraction_review',attempt_id=next_attempt.pk)
