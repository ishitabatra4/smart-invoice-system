"""Locked adjustment lifecycle. No original invoice or payment row is ever saved here."""
from datetime import date
from decimal import Decimal
import re
from django.core.exceptions import ValidationError
from django.db import connection, transaction, DatabaseError
from django.db.models import Sum, Max
from django.utils import timezone
from accounts.models import BusinessProfile
from audit.services import record_event
from .models import Invoice, InvoiceAdjustmentNote as Note, InvoiceAdjustmentLine as Line, AdjustmentNoteSequence
from .manual_services import year_start
from .manual_calculations import money, decimal, calculate_invoice, validate_supply

ZERO = Decimal('0.00')
COMPONENTS = ('taxable_amount','cgst_amount','sgst_amount','igst_amount','total_amount')


def adjustments_available():
    try: return Note._meta.db_table in connection.introspection.table_names()
    except DatabaseError: return False


def eligible_source(invoice, business):
    if (invoice.user_id != business.user_id or invoice.invoice_type not in {'sales','purchase'}
        or invoice.entry_mode != 'manual' or invoice.document_state != 'finalized'
        or invoice.status not in {'verified','paid'} or invoice.calculation_version != 1
        or invoice.supply_category != 'standard'):
        raise ValidationError('Only finalized versioned standard sales/purchase invoices can be adjusted. Review unsupported historical records manually.')
    if invoice.status == 'paid':
        from .ledger import payment_total
        if payment_total(invoice) == 0:
            raise ValidationError('The paid marker has no recorded payment evidence; reconcile payments before adjusting this invoice.')
    party = invoice.buyer_party if invoice.invoice_type == 'sales' else invoice.supplier_party
    if not party or party.business_id != business.pk:
        raise ValidationError('The original invoice must have an owned customer/supplier.')
    validate_supply(invoice.supplier_state_code, invoice.place_of_supply, invoice.supply_category)
    lines=list(invoice.line_items.all())
    if not lines or invoice.other_charges != 0 or not invoice.business_snapshot or not invoice.party_snapshot:
        raise ValidationError('Incomplete historical details or invoice rounding/other charges require manual review.')
    for line in lines:
        if (line.quantity <= 0 or line.tax_treatment not in {'taxable','exempt','nil_rated','non_gst'} or line.taxable_amount <= 0
            or any(getattr(line,k) is None or getattr(line,k)<0 for k in COMPONENTS)
            or line.tax_amount != line.cgst_amount+line.sgst_amount+line.igst_amount
            or line.total_amount != line.taxable_amount+line.tax_amount):
            raise ValidationError('Original line allocations are incomplete or inconsistent; manual review is required.')
    if any(sum((getattr(line,k) for line in lines),ZERO)!=getattr(invoice,k) for k in COMPONENTS):
        raise ValidationError('Original header and line amounts differ; manual review is required.')
    return party,lines


def _number(business,kind,direction,day):
    start=year_start(business,day)
    seq,_=AdjustmentNoteSequence.objects.select_for_update().get_or_create(business=business,kind=kind,direction=direction,year_start=start)
    prefix={('credit','issued'):'CN',('debit','issued'):'DN',('credit','received'):'RC',('debit','received'):'RD'}[(kind,direction)]
    while seq.next_value<=999999:
        number=f'{prefix}/{start.year%100:02}{(start.year+1)%100:02}/{seq.next_value:06}'
        seq.next_value+=1
        if not Note.objects.filter(business=business,number=number).exists():
            seq.save(update_fields=['next_value']);return number,start
    raise ValidationError('Adjustment series exhausted.')


def _audit(request,note,action,description):
    record_event(request=request,business=note.business,action=action,module='adjustments',description=description,obj=note,related_obj=note.original_invoice,
        after={'number':note.number,'status':note.status,**{k:getattr(note,k) for k in COMPONENTS}})


def _calculate(note,rows,invoice,lines):
    if not 1<=len(rows)<=100: raise ValidationError('Provide between one and 100 adjustment lines.')
    sources={line.pk:line for line in lines};seen=set();results=[]
    credits=Note.objects.filter(original_invoice=invoice,kind='credit',status='finalized').exclude(pk=note.pk)
    if note.kind=='credit' and credits.exclude(tax_effect=note.tax_effect).exists():
        raise ValidationError('Mixing financial-only and GST credits against one invoice requires manual review.')
    used_by_line={}
    if note.kind=='credit':
        used_by_line={row['original_line_id']:row for row in Line.objects.filter(document__in=credits).values('original_line_id').annotate(**{k:Sum(k) for k in (*COMPONENTS,'quantity')})}
    for row in rows:
        if decimal(row.get('amount') or 0)<0:raise ValidationError('Adjustment values cannot be negative.')
        source=sources.get(row['original_line'].pk)
        if not source or source.pk in seen: raise ValidationError('Choose each original invoice line only once.')
        seen.add(source.pk);mode=row['mode'];quantity=decimal(row.get('quantity') or 0);amount=money(row.get('amount') or 0)
        if mode not in {'quantity','value'} or (mode=='quantity' and quantity<=0) or (mode=='value' and (quantity!=0 or amount<=0)):
            raise ValidationError('Quantity adjustments require a positive quantity; value adjustments require zero quantity and a positive value.')
        if note.kind=='debit' and amount<=0: raise ValidationError('Enter an explicit positive additional value or unit rate; original inclusive rates are not inferred.')
        if amount<0 or (note.kind=='credit' and mode=='quantity' and amount!=0):raise ValidationError('Use nonnegative inputs; quantity credits do not accept a separate value override.')
        if quantity != quantity.quantize(Decimal('.001')): raise ValidationError('Quantity supports three decimal places.')
        values={key:getattr(source,key) for key in ('description','hsn_sac_code','unit','tax_rate','tax_treatment')}
        values.update(original_line=source,mode=mode,input_amount=amount,quantity=quantity,rate=source.rate,discount=ZERO,discount_type='fixed',discount_value=ZERO,invoice_discount_allocated=ZERO)
        if note.kind=='credit':
            previous=used_by_line.get(source.pk,{})
            used={k:previous.get(k) or ZERO for k in COMPONENTS}
            usedqty=previous.get('quantity') or ZERO
            if mode=='quantity' and quantity>source.quantity-usedqty: raise ValidationError('Credit quantity exceeds remaining original quantity.')
            base=money(source.taxable_amount*quantity/source.quantity) if mode=='quantity' else (money(amount*source.taxable_amount/source.total_amount) if note.pricing_mode=='inclusive' and note.tax_effect=='gst' else amount)
            if base<=0 or base>source.taxable_amount-used['taxable_amount']: raise ValidationError('Credit value exceeds remaining original value.')
            values['taxable_amount']=base
            for k in ('cgst_amount','sgst_amount','igst_amount'):
                values[k]=ZERO if note.tax_effect=='financial' else money(getattr(source,k)*(used['taxable_amount']+base)/source.taxable_amount)-used[k]
                if values[k]<0 or values[k]>getattr(source,k)-used[k]: raise ValidationError('Credit tax exceeds remaining saved tax component.')
            values['tax_amount']=sum((values[k] for k in ('cgst_amount','sgst_amount','igst_amount')),ZERO)
            values['total_amount']=base+values['tax_amount']
        else:
            # Additional charge retains saved tax treatment/rate/state; no directory inference.
            engine_row={**values,'quantity':quantity if mode=='quantity' else Decimal('1'),'rate':amount if amount else source.rate,'discount':decimal(row.get('discount') or 0),'discount_type':row.get('discount_type') or 'fixed'}
            if note.tax_effect=='financial': engine_row.update(tax_rate=ZERO,tax_treatment='non_gst')
            result=calculate_invoice([engine_row],supplier_state=invoice.supplier_state_code,place_of_supply=invoice.place_of_supply,
                pricing_mode=note.pricing_mode,supply_category=invoice.supply_category,allowed_rates=(source.tax_rate,ZERO),
                invoice_discount_type=note.invoice_discount_type,invoice_discount=ZERO)
            values.update({k:result['lines'][0][k] for k in ('discount','invoice_discount_allocated','taxable_amount','tax_amount','cgst_amount','sgst_amount','igst_amount','total_amount')})
            values.update(discount_type=engine_row['discount_type'],discount_value=engine_row['discount'])
        if note.tax_effect=='financial': values.update(tax_rate=ZERO,tax_treatment='non_gst')
        results.append(values)
    # Document discounts on debits must allocate across all lines together.
    if note.kind=='debit':
        engine_rows=[{**v,'quantity':v['quantity'] if v['mode']=='quantity' else Decimal('1'),'rate':v['input_amount'] or v['original_line'].rate,'discount':v['discount_value']} for v in results]
        calculation=calculate_invoice(engine_rows,supplier_state=invoice.supplier_state_code,place_of_supply=invoice.place_of_supply,pricing_mode=note.pricing_mode,
            supply_category=invoice.supply_category,allowed_rates=tuple({v['tax_rate'] for v in results}),invoice_discount_type=note.invoice_discount_type,invoice_discount=note.invoice_discount_value)
        for values,result in zip(results,calculation['lines']):
            values.update({k:result[k] for k in ('discount','invoice_discount_allocated','taxable_amount','tax_amount','cgst_amount','sgst_amount','igst_amount','total_amount')})
    for k in COMPONENTS:setattr(note,k,sum((v[k] for v in results),ZERO))
    note.invoice_discount_amount=sum((v['invoice_discount_allocated'] for v in results),ZERO)
    note.total_discount_amount=note.invoice_discount_amount+sum((v['discount'] for v in results),ZERO)
    note.subtotal=note.taxable_amount+note.total_discount_amount
    note.treatment_breakdown={name:str(sum((v['taxable_amount'] for v in results if v['tax_treatment']==name),ZERO)) for name in ('taxable','exempt','nil_rated','non_gst')}
    return results


@transaction.atomic
def save_adjustment(*,request,invoice_id,kind,data,rows,creation_key,note_id=None,preview=False):
    business=BusinessProfile.objects.select_for_update().get(user=request.user)
    invoice=Invoice.objects.select_for_update().get(pk=invoice_id,user=request.user)
    party,lines=eligible_source(invoice,business)
    if kind not in {'credit','debit'}: raise ValidationError('Choose credit or debit.')
    if note_id:
        note=Note.objects.select_for_update().get(pk=note_id,user=request.user)
        if note.status!='draft' or note.original_invoice_id!=invoice.pk or note.kind!=kind: raise ValidationError('Only this original draft can be edited.')
    else:
        note=Note.objects.filter(creation_key=creation_key,user=request.user).first()
        if note:
            if note.original_invoice_id!=invoice.pk or note.kind!=kind:raise ValidationError('Creation confirmation belongs to another invoice.')
            return note
        note=Note(user=request.user,business=business,original_invoice=invoice,party=party,kind=kind,direction='issued' if invoice.invoice_type=='sales' else 'received',creation_key=creation_key)
    for field in ('document_date','reason','pricing_mode','tax_effect','supplier_reference','notes','eligibility_reviewed','annual_return_date','invoice_discount_type','invoice_discount_value'):
        if field in data:setattr(note,field,data[field])
    if not invoice.invoice_date<=note.document_date<=timezone.localdate():raise ValidationError('Note date must be between the original invoice date and today.')
    if note.direction=='received' and not re.fullmatch(r'[A-Za-z0-9/-]{1,16}',note.supplier_reference):raise ValidationError('Enter the actual supplier note reference (up to 16 letters, digits, / or -).')
    if note.kind=='credit' and (note.invoice_discount_value!=0 or any(decimal(r.get('discount') or 0)!=0 for r in rows)):
        raise ValidationError('Enter the final credit value; original saved discounts are already included. Additional credit discounts are not supported.')
    if note.annual_return_date and note.annual_return_date>timezone.localdate():raise ValidationError('An actual annual-return filing date cannot be in the future.')
    if not note.reason.strip():raise ValidationError('Enter an adjustment reason.')
    if note.pk and year_start(business,note.document_date)!=note.year_start: raise ValidationError('Keep this numbered draft within its reserved financial year.')
    note.supplier_state_code,note.place_of_supply=invoice.supplier_state_code,invoice.place_of_supply
    note.supply_category=invoice.supply_category;note.business_snapshot=invoice.business_snapshot;note.party_snapshot=invoice.party_snapshot
    note.original_snapshot={'number':invoice.invoice_number,'date':invoice.invoice_date.isoformat(),'type':invoice.invoice_type,**{k:str(getattr(invoice,k)) for k in COMPONENTS}}
    values=_calculate(note,rows,invoice,lines)
    if preview:return note,values
    if not note.pk:note.number,note.year_start=_number(business,kind,note.direction,note.document_date)
    note.full_clean();note.save();note.line_items.all().delete()
    for row in values:
        line=Line(document=note,**row);line.full_clean();line.save()
    _audit(request,note,'update' if note_id else 'create','Adjustment draft saved; no accounting effect.')
    return note


@transaction.atomic
def transition_adjustment(*,request,note_id,action,reason='',cancellation_date=None):
    business=BusinessProfile.objects.select_for_update().get(user=request.user)
    note=Note.objects.select_for_update().select_related('original_invoice').get(pk=note_id,user=request.user,business=business)
    invoice=Invoice.objects.select_for_update().get(pk=note.original_invoice_id,user=request.user)
    if action=='finalize':
        if note.status=='finalized':return note
        if note.status!='draft':raise ValidationError('Only a draft can be finalized.')
        _,lines=eligible_source(invoice,business)
        if not invoice.invoice_date<=note.document_date<=timezone.localdate():raise ValidationError('Review the note date.')
        if note.tax_effect=='gst':
            if not note.eligibility_reviewed:raise ValidationError('Confirm GST eligibility, annual-return and ITC review before finalization.')
            if note.kind=='credit':
                supply_fy=invoice.invoice_date.year if invoice.invoice_date.month>=4 else invoice.invoice_date.year-1
                deadline=date(supply_fy+1,11,30)
                if timezone.localdate()>deadline or note.document_date>deadline or (note.annual_return_date and timezone.localdate()>=note.annual_return_date):raise ValidationError('GST credit deadline/annual return prevents automatic GST adjustment; request professional review.')
        latest=Note.objects.filter(original_invoice=invoice,finalized_at__isnull=False).exclude(pk=note.pk).aggregate(day=Max('document_date'),cancel=Max('cancellation_date'))
        if any(d and note.document_date<d for d in latest.values()):raise ValidationError('Cannot backdate before another recognized adjustment/reversal.')
        stored=list(note.line_items.all());rows=[{'original_line':line.original_line,'mode':line.mode,'quantity':line.quantity,'amount':line.input_amount,'discount':line.discount_value,'discount_type':line.discount_type} for line in stored]
        original={k:getattr(note,k) for k in COMPONENTS};values=_calculate(note,rows,invoice,lines)
        if any(original[k]!=getattr(note,k) for k in COMPONENTS) or any(any(getattr(line,k)!=v[k] for k in COMPONENTS) for line,v in zip(stored,values)):
            raise ValidationError('Saved adjustments differ from eligible allocation; review and save the draft.')
        note.status='finalized';note.finalized_at=timezone.now()
        description='Adjustment finalized; separate dated financial recognition recorded.'
    elif action=='cancel':
        if note.status=='cancelled':raise ValidationError('This note is already cancelled.')
        if not reason.strip() or len(reason)>500:raise ValidationError('Enter a cancellation reason (up to 500 characters).')
        day=cancellation_date or timezone.localdate()
        latest=Note.objects.filter(original_invoice=invoice,finalized_at__isnull=False).aggregate(day=Max('document_date'),cancel=Max('cancellation_date'))
        if day>timezone.localdate() or day<note.document_date or any(d and day<d for d in latest.values()):raise ValidationError('Reversal date must not precede recognized notes/reversals or exceed today.')
        note.status='cancelled';note.cancelled_at=timezone.now();note.cancellation_reason=reason;note.cancellation_date=day
        description='Adjustment cancelled; dated reversal retained.'
    else:raise ValidationError('Unknown adjustment action.')
    note.full_clean();note.save();_audit(request,note,'cancel' if action=='cancel' else 'verify',description);return note
