import uuid
from datetime import date
from io import BytesIO
from decimal import Decimal
import csv
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from config.report_limits import validate_report_range, MAX_REPORT_INVOICES, MAX_REPORT_NOTES, MAX_REPORT_RELATED_ROWS, MAX_EXPORT_ROWS
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.http import content_disposition_header
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.cache import never_cache
from accounts.models import BusinessProfile
from audit.models import AuditEvent
from audit.services import record_event
from .models import Invoice, InvoiceAdjustmentNote as Note, AdjustmentNoteEmailDelivery as History
from .adjustment_services import adjustments_available, save_adjustment, transition_adjustment
from .adjustment_reporting import adjusted_invoice_amount, overpayment, note_events
from .adjustment_forms import AdjustmentForm, AdjustmentLineSet, AdjustmentEmailForm, CREATE_SALT, EMAIL_SALT
from .adjustment_pdf import generate_adjustment_pdf, adjustment_pdf_filename
from .pdf_generator import PDFGenerationError, render_invoice_pdf, invoice_pdf_data
from .email_delivery import send_adjustment_email, default_recipient, eligible_for_email


def owned(request,pk):
    if not adjustments_available():raise Http404('Adjustment tables require approval and migration.')
    return get_object_or_404(Note.objects.select_related('original_invoice','business','party'),pk=pk,user=request.user,business__user=request.user)


@login_required
@require_GET
def adjustment_list(request):
    ready=adjustments_available();notes=Note.objects.filter(user=request.user,business__user=request.user).select_related('original_invoice','party') if ready else Note.objects.none()
    for name in ['kind','status','direction']:
        if request.GET.get(name):notes=notes.filter(**{name:request.GET[name]})
    if request.GET.get('q'):
        term=request.GET['q'][:200];notes=notes.filter(Q(number__icontains=term)|Q(original_invoice__invoice_number__icontains=term)|Q(party_snapshot__name__icontains=term))
    sort=request.GET.get('sort','-document_date');notes=notes.order_by(sort if sort in ['document_date','-document_date','number','-number','total_amount','-total_amount'] else '-document_date','-pk')
    return render(request,'invoices/adjustment_list.html',{'page':Paginator(notes,25).get_page(request.GET.get('page')),'ready':ready})


@login_required
@require_http_methods(['GET','POST'])
def adjustment_form(request,note_id=None):
    if not adjustments_available():return HttpResponse('Adjustment tables require migration approval.',status=503)
    business=get_object_or_404(BusinessProfile,user=request.user);note=owned(request,note_id) if note_id else None
    if note and note.status!='draft':return HttpResponse('Only drafts can be edited.',status=400)
    initial={'creation_token':signing.dumps({'user':request.user.pk,'note':note.pk if note else None,'key':str(uuid.uuid4())},salt=CREATE_SALT)}
    if request.GET.get('invoice'):initial['original_invoice']=request.GET['invoice']
    form=AdjustmentForm(request.POST if request.method=='POST' else None,business=business,note=note,initial=initial)
    invoice=note.original_invoice if note else None
    selected=request.POST.get('original_invoice') if request.method=='POST' else request.GET.get('invoice')
    if selected:
        try:invoice=Invoice.objects.filter(pk=int(selected),user=request.user).first()
        except (ValueError,TypeError):invoice=None
    if invoice and not note and request.method=='GET':form.initial['pricing_mode']=invoice.pricing_mode
    rows=[{'original_line':l.original_line_id,'mode':l.mode,'quantity':l.quantity,'amount':l.input_amount,'discount':l.discount_value,'discount_type':l.discount_type} for l in note.line_items.all()] if note else []
    lines=AdjustmentLineSet(request.POST if request.method=='POST' else None,initial=rows,form_kwargs={'invoice':invoice},prefix='items');preview=None
    if request.method=='POST':
        valid=form.is_valid();line_valid=lines.is_valid()
        if valid and line_valid:
            try:
                result=save_adjustment(request=request,invoice_id=form.cleaned_data['original_invoice'].pk,kind=form.cleaned_data['kind'],data=form.cleaned_data,
                    rows=[line.cleaned_data for line in lines if line.cleaned_data and not line.cleaned_data.get('DELETE')],creation_key=form.creation_key,note_id=note.pk if note else None,preview='preview' in request.POST)
                if 'preview' in request.POST:preview=result[0]
                else:return redirect('adjustment_detail',note_id=result.pk)
            except ValidationError as exc:form.add_error(None,exc)
    return render(request,'invoices/adjustment_form.html',{'form':form,'lines':lines,'note':note,'preview':preview,'selected_invoice':invoice})


@login_required
@require_GET
def adjustment_detail(request,note_id):
    note=owned(request,note_id)
    return render(request,'invoices/adjustment_detail.html',{'note':note,'net':adjusted_invoice_amount(note.original_invoice),'overpayment':overpayment(note.original_invoice),
        'deliveries':note.email_deliveries.select_related('initiated_by')[:20], 'activity':AuditEvent.objects.filter(user=request.user,object_type=Note._meta.label_lower,object_id=str(note.pk)).order_by('-created_at')[:50]})


@login_required
@require_POST
def adjustment_transition(request,note_id,action):
    note=owned(request,note_id)
    if request.POST.get('confirm')!='on':messages.error(request,'Explicitly confirm this action.');return redirect('adjustment_detail',note_id=note.pk)
    try:
        day=date.fromisoformat(request.POST['cancellation_date']) if request.POST.get('cancellation_date') else None
        transition_adjustment(request=request,note_id=note.pk,action=action,reason=request.POST.get('reason',''),cancellation_date=day)
    except (ValidationError,ValueError) as exc:messages.error(request,' '.join(exc.messages) if isinstance(exc,ValidationError) else 'Enter a valid reversal date.')
    return redirect('adjustment_detail',note_id=note.pk)


@login_required
@never_cache
@require_GET
def adjustment_pdf(request,note_id):
    note=owned(request,note_id)
    try:content=generate_adjustment_pdf(note)
    except PDFGenerationError:return HttpResponse('PDF generation requires manual review.',status=422)
    response=HttpResponse(content,content_type='application/pdf');response['Content-Disposition']=content_disposition_header(True,adjustment_pdf_filename(note));response['X-Content-Type-Options']='nosniff'
    record_event(request=request,business=note.business,action='export',module='adjustments',description='Adjustment PDF downloaded.',obj=note)
    return response


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def adjustment_email(request,note_id,delivery_id=None):
    note=owned(request,note_id)
    if not eligible_for_email(note):return HttpResponse('Only finalized notes can be emailed.',status=400)
    retry=get_object_or_404(History,pk=delivery_id,document=note,business=note.business,status='failed') if delivery_id else None
    initial={'recipient':retry.recipient if retry else default_recipient(note),'send_token':signing.dumps({'user':request.user.pk,'note':note.pk,'retry':retry.pk if retry else None,'key':str(uuid.uuid4())},salt=EMAIL_SALT)}
    form=AdjustmentEmailForm(request.POST if request.method=='POST' else None,user=request.user,invoice=note,retry=retry,initial=initial)
    if request.method=='POST' and form.is_valid():
        try:
            result=send_adjustment_email(user=request.user,document_id=note.pk,recipient=form.cleaned_data['recipient'],request_key=form.cleaned_data['send_token'],
                retry_id=retry.pk if retry else None,confirm_override=form.cleaned_data['confirm_override'],confirm_resend=form.cleaned_data['confirm_resend'],request=request)
            messages.info(request,'Email status: '+result.status+'. Backend acceptance does not confirm inbox receipt. No automatic retry.')
            return redirect('adjustment_detail',note_id=note.pk)
        except ValidationError as exc:form.add_error(None,exc)
    return render(request,'invoices/adjustment_email.html',{'note':note,'form':form,'retry':retry})


@login_required
@require_GET
def adjustment_report(request):
    if not adjustments_available():
        return HttpResponse('Adjustment reporting requires the approved schema.', status=503)
    today = timezone.localdate()
    year_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    try:
        start = date.fromisoformat(request.GET.get('start', year_start.isoformat()))
        end = date.fromisoformat(request.GET.get('end', today.isoformat()))
        validate_report_range(start, end)
    except (ValueError, ValidationError) as exc:
        return HttpResponse('Choose a valid reporting period: ' + (' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Use ISO dates.'), status=400)
    # Bound work before the analytics engine, prefetches and workbook allocation.
    sources = Invoice.objects.filter(user=request.user, status__in=['verified', 'paid'], invoice_date__range=(start, end))
    notes = Note.objects.filter(user=request.user, business__user=request.user, finalized_at__isnull=False).filter(Q(document_date__range=(start, end)) | Q(cancellation_date__range=(start, end)))
    if sources.count() > MAX_REPORT_INVOICES or notes.count() > MAX_REPORT_NOTES:
        return HttpResponse('Narrow dates or filters: report supports 2,000 source invoices and 1,000 adjustment notes.', status=400)
    source_ids = set(sources.values_list('pk', flat=True)) | set(notes.values_list('original_invoice_id', flat=True))
    from .models import InvoicePayment
    if (InvoicePayment.objects.filter(user=request.user, invoice_id__in=source_ids).count() > MAX_REPORT_RELATED_ROWS
        or Note.objects.filter(user=request.user, business__user=request.user, original_invoice_id__in=source_ids).count() > MAX_REPORT_RELATED_ROWS):
        return HttpResponse('Narrow this report: related payment or adjustment history exceeds 10,000 records.', status=400)
    from .adjustment_reporting import reporting_summary
    context=reporting_summary(request.user,start,end);events=context['adjustment_events'];context.update(events=events,start=start,end=end)
    if len(events) + 5 > MAX_EXPORT_ROWS:
        return HttpResponse('Narrow this export: maximum 10,000 rows including summary.', status=400)
    rows=[['Date','Note','Original invoice','Direction','Kind','Reversal','Tax effect','Value before GST','CGST','SGST','IGST','Total','GST preparation status']]
    for e in events:
        n=e['note'];rows.append([str(e['date']),n.number,n.original_snapshot['number'],n.direction,n.kind,str(e['reversal']),n.tax_effect,*[e[k] for k in ('taxable_amount','cgst_amount','sgst_amount','igst_amount','total_amount')],'Excluded from GST preparation; financial only' if n.tax_effect=='financial' else 'Preparation estimate; review eligibility and amendments'])
    kind=request.GET.get('export')
    if kind=='pdf':
        from .pdf_generator import render_report_pdf
        summary='Period '+str(start)+' to '+str(end)+'. Gross original sales INR '+str(context['gross_original']['sales'])+'; net sales INR '+str(context['totals']['sales'])+'; gross original purchases INR '+str(context['gross_original']['purchases'])+'; net purchases INR '+str(context['totals']['purchases'])+'. Signed note-date events include dated reversals. GST/ITC and amendments require review. Financial-only events are excluded from GST preparation. No filing or statutory compliance guarantee.'
        pdf_rows=[[str(e['date']),e['note'].number,e['note'].original_snapshot['number'],e['note'].kind+(' reversal' if e['reversal'] else ''),str(e['total_amount'])] for e in events]
        try: content=render_report_pdf('Credit / debit note preparation', ['Date','Note','Original invoice','Event','INR total'],pdf_rows,summary)
        except PDFGenerationError:return HttpResponse('Select a shorter PDF reporting period.',status=422)
        response=HttpResponse(content,content_type='application/pdf');response['Content-Disposition']=content_disposition_header(True,'adjustment-preparation.pdf')
        record_event(request=request,action='export',module='adjustments',description='Adjustment preparation PDF exported.')
        return response
    if kind in {'csv','xlsx'}:
        rows += [[],['Financial summary','Gross original','Signed credit events','Signed debit events','Net adjusted']]
        for side,key in [('sales','sales'),('purchase','purchases')]:
            rows.append([side,context['gross_original'][key],context['note_breakdown'][side]['credit'],context['note_breakdown'][side]['debit'],context['totals'][key]])
        from config.export_security import spreadsheet_value as safe_cell
        if kind=='csv':
            response=HttpResponse(content_type='text/csv');writer=csv.writer(response)
            for row in rows:writer.writerow([safe_cell(v) for v in row])
        else:
            from openpyxl import Workbook
            workbook=Workbook();sheet=workbook.active;sheet.title='Adjustment preparation'
            for row in rows:sheet.append([safe_cell(v) for v in row])
            output=BytesIO();workbook.save(output);response=HttpResponse(output.getvalue(),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition']=content_disposition_header(True,'adjustment-preparation.'+kind)
        record_event(request=request,action='export',module='adjustments',description='Adjustment preparation export downloaded.')
        return response
    return render(request,'invoices/adjustment_report.html',context)
