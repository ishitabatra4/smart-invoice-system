"""Saved-value presentation through the existing bounded ReportLab renderer."""
import hashlib
import json
from .models import Invoice, InvoiceLineItem
from .pdf_generator import invoice_pdf_data, render_invoice_pdf, invoice_pdf_filename, MAX_LINES, PDFGenerationError


def generate_adjustment_pdf(note):
    lines=list(note.line_items.all()[:MAX_LINES+1])
    if len(lines)>MAX_LINES:raise PDFGenerationError('Adjustment exceeds PDF item limit.')
    names=('pricing_mode','supply_category','supplier_state_code','place_of_supply','calculation_version','invoice_discount_type','invoice_discount_value','invoice_discount_amount','total_discount_amount','taxable_amount','cgst_amount','sgst_amount','igst_amount','other_charges','total_amount','treatment_breakdown','business_snapshot','party_snapshot','payment_terms','notes','terms_conditions')
    adapter=Invoice(pk=0,user_id=note.user_id,invoice_type=note.original_snapshot['type'],entry_mode='manual',document_state=note.status,status='cancelled' if note.status=='cancelled' else 'draft' if note.status=='draft' else 'verified',invoice_number=note.number,invoice_date=note.document_date,**{key:getattr(note,key) for key in names})
    fields=[f.name for f in InvoiceLineItem._meta.fields if f.name not in {'id','invoice','created_at'}]
    adapter._prefetched_objects_cache={'payments':[],'line_items':[InvoiceLineItem(invoice=adapter,**{k:getattr(line,k) for k in fields}) for line in lines]}
    data=invoice_pdf_data(adapter);data.update(adjustment=True,direction=note.direction,title=note.get_kind_display().upper(),type=note.get_kind_display())
    data['notices'].insert(0,f"Original invoice {note.original_snapshot['number']} dated {note.original_snapshot['date']}. Reason: {note.reason}. Direction: {note.direction}. Tax effect: {note.tax_effect}.")
    if note.direction=='received':data['notices'].insert(0,'Received supplier note reference: '+note.supplier_reference+'. Internal tracking copy, not a supplier-issued original.')
    data['notices'].append('Value-only rows show zero quantity and do not change original item quantities.')
    data['notices'].append('GST preparation record; not a return filing or eligibility guarantee. Signature / statutory review may be required.')
    if note.status=='cancelled':data['notices'].append('Cancellation reversal date: '+str(note.cancellation_date)+'. '+note.cancellation_reason)
    data["revision"]=hashlib.sha256(json.dumps({k:v for k,v in data.items() if k!="revision"},default=str,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    return render_invoice_pdf(data)


def adjustment_pdf_filename(note):return invoice_pdf_filename(note)
