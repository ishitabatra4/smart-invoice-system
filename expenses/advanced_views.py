from pathlib import Path
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import FileResponse,Http404
from django.shortcuts import render,redirect,get_object_or_404
from django.views.decorators.http import require_http_methods,require_GET
from django.views.decorators.cache import never_cache
from .models import ExpenseAttachmentRevision,private_expense_storage
from .forms import ITCReviewForm,PurchaseExpenseForm
from .services import managed_invoices,review_itc,advanced_ready,link_purchase
from .views import _business,_ensure_default_categories


@login_required
@require_http_methods(['GET','POST'])
def receipt_upload(request):
    business=_business(request)
    if business is None:return redirect('profile')
    if request.method=='POST':
        upload=request.FILES.get('receipt')
        if not upload:messages.error(request,'Choose a PDF, PNG or JPEG receipt.')
        else:
            from invoices.views import _process_invoice_file,InvoiceFileIssue
            try:
                invoice,warnings=_process_invoice_file(request,upload,'expense',business,None)
                from invoices.extraction_services import latest_attempt
                return redirect('extraction_review',attempt_id=latest_attempt(invoice).pk)
            except (ValidationError,InvoiceFileIssue) as exc:messages.error(request,' '.join(exc.messages) if isinstance(exc,ValidationError) else str(exc))
    return render(request,'expenses/receipt_upload.html')


@login_required
@require_http_methods(['GET','POST'])
def purchase_link(request):
    business=_business(request)
    if business is None:return redirect('profile')
    _ensure_default_categories(business)
    form=PurchaseExpenseForm(request.POST if request.method=='POST' else None,business=business)
    if request.method=='POST' and form.is_valid():
        try:detail=link_purchase(request,form.cleaned_data['invoice'],form.cleaned_data['category'],form.cleaned_data['confirm'])
        except ValidationError as exc:form.add_error(None,exc)
        else:return redirect('expense_detail',expense_id=detail.invoice_id)
    return render(request,'expenses/advanced_form.html',{'form':form,'title':'Attribute existing purchase to expense register','help':'The same purchase remains authoritative for purchase totals, input GST, payments and payables. The expense register is an overlapping management view; do not add its totals to purchase totals.'})


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def itc_review(request,expense_id):
    invoice=get_object_or_404(managed_invoices(request.user),pk=expense_id)
    form=ITCReviewForm(request.POST if request.method=='POST' else None)
    if request.method=='POST' and form.is_valid():
        try:review_itc(request,invoice,**form.cleaned_data)
        except ValidationError as exc:form.add_error(None,exc)
        else:return redirect('expense_detail',expense_id=invoice.pk)
    return render(request,'expenses/advanced_form.html',{'form':form,'title':'ITC preparation review','help':'Human review only. No government registration verification, official match, credit claim or return filing is implied. Blocked credits, mixed/private use and special supplies require specialist assessment. Source changes invalidate reviewed totals.'})


@login_required
@never_cache
@require_GET
def receipt_revision(request,revision_id):
    if not advanced_ready():raise Http404
    revision=get_object_or_404(ExpenseAttachmentRevision,pk=revision_id,invoice__user=request.user)
    if revision.storage_kind=='private':storage=private_expense_storage
    else:
        from invoices.models import Invoice
        storage=Invoice._meta.get_field('source_file').storage
    try:source=storage.open(revision.file_name,'rb')
    except OSError:raise Http404('Receipt unavailable.') from None
    import hashlib
    digest=hashlib.sha256();size=0
    for chunk in source.chunks():
        size+=len(chunk)
        if size>10*1024*1024:source.close();raise Http404('Retained receipt exceeds the accepted limit.')
        digest.update(chunk)
    if digest.hexdigest()!=revision.sha256:source.close();raise Http404('Retained receipt hash changed.')
    source.seek(0)
    ext=Path(revision.file_name).suffix.lower();mime={'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg'}.get(ext,'application/octet-stream')
    response=FileResponse(source,as_attachment=True,filename='retained-receipt'+ext,content_type=mime);response['X-Content-Type-Options']='nosniff';response['Content-Security-Policy']="sandbox; frame-ancestors 'self'";return response
