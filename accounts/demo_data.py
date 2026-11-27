"""Synthetic manual-testing dataset, using existing lifecycle/calculation services."""
from datetime import date
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path
import json
import secrets
import uuid
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction, connection
from django.test import Client, RequestFactory
from django.urls import reverse
from accounts.models import BusinessProfile, BusinessParty
from audit.services import record_event
from invoices.models import Invoice, InvoicePayment, CommercialDocument, InvoiceAdjustmentNote
from invoices.manual_services import save_draft, finalize_invoice
from invoices.commercial_services import save_commercial, transition_commercial
from invoices.adjustment_services import save_adjustment, transition_adjustment
from invoices.ledger import invoice_outstanding, party_ledger_entries
from expenses.models import ExpenseCategory, RecurringExpense, ExpenseITCReview
from expenses.forms import ExpenseForm
from expenses.views import _save_expense
from expenses.services import review_itc
from expenses.recurrence import generate_due_expenses
from taxation import preparation
from config.demo_safety import require_demo

DAY = date(2026, 11, 1)


def gstin(prefix):
    from invoices.gstin import ALPHABET
    total = 0
    for index, char in enumerate(reversed(prefix)):
        value = ALPHABET.index(char) * (2 if index % 2 == 0 else 1)
        total += value // 36 + value % 36
    return prefix + ALPHABET[(36 - total % 36) % 36]


def request_for(user):
    request = RequestFactory().post('/demo-setup/', REMOTE_ADDR='127.0.0.1')
    request.user = user
    return request


def line(rate='1000', tax='18', **changes):
    return dict(description='Fictional demo service', hsn_sac_code='9983', quantity=D('1'),
        unit='Hours', rate=D(rate), discount=D('0'), discount_type='fixed',
        tax_rate=D(tax), tax_treatment='taxable', **changes)


def header(party, day=DAY, **changes):
    values = dict(party=party, invoice_number='', invoice_date=day, due_date=date(2026, 11, 30),
        supplier_state_code='27', place_of_supply='27', pricing_mode='exclusive', supply_category='standard',
        invoice_discount_type='fixed', invoice_discount_value=D('0'), adjustment=D('0'),
        payment_terms='Synthetic local example only; no payment requested.', notes='Fictional demo record.',
        terms_conditions='Not a tax document or evidence of supply.')
    values.update(changes)
    return values


def receipt_bytes(reference, supplier, business, amount='1000', tax='90', total='1180'):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    output = BytesIO(); page = canvas.Canvas(output, pagesize=A4)
    rows = ['SYNTHETIC DEMO RECEIPT — NOT A REAL TRANSACTION', 'TAX INVOICE',
        f'Invoice No: {reference}', 'Invoice Date: 01/11/2026', f'Supplier: {supplier.name}',
        f'Supplier GSTIN: {supplier.gstin}', f'Bill To: {business.business_name}', f'Buyer GSTIN: {business.gstin}',
        'Description HSN Qty Rate Taxable GST', f'Demo service 9983 1 {amount} {amount} 18%',
        f'Taxable Amount: INR {amount}', f'CGST: INR {tax}', f'SGST: INR {tax}', f'Grand Total: INR {total}']
    text = page.beginText(40, 800)
    for row in rows: text.textLine(row)
    page.drawText(text); page.save(); return output.getvalue()


def counts():
    from django.apps import apps
    return {model._meta.label: model.objects.count() for model in apps.get_models()
        if model._meta.app_label in {'accounts', 'invoices', 'expenses', 'taxation', 'audit'} or model._meta.label == 'auth.User'}


def verify_inventory(data):
    require_demo()
    current = counts()
    # Manual testing may add records; a rerun must never erase or reseed them.
    for label, minimum in data['counts'].items():
        if current.get(label, 0) < minimum:
            raise ValidationError('Demo records were removed; refusing automatic repair or reset.')
    for record in data['invoices']:
        inv = Invoice.objects.get(pk=record['id'])
        if inv.invoice_number != record['number'] or inv.user_id != record['user_id']:
            raise ValidationError('Demo invoice identity changed; manual review required.')
    return current


def add_overdue_example(inventory):
    """One extra sales example keeps an overdue receivable available immediately."""
    require_demo()
    if any(row['scenario']=='overdue-sale' for row in inventory['invoices']):
        return
    user=get_user_model().objects.get(username='lumen-owner')
    business=BusinessProfile.objects.get(user=user)
    customer=BusinessParty.objects.filter(business=business,party_type='customer').order_by('pk').first()
    inv=save_draft(request=request_for(user),business=business,invoice_type='sales',
        data=header(customer,date(2026,10,15),due_date=date(2026,10,31)),line_data=[line()],
        creation_key=uuid.uuid5(uuid.NAMESPACE_URL,'smart-invoice-isolated-demo/overdue-sale'))
    if inv.document_state=='draft':finalize_invoice(request=request_for(user),invoice_id=inv.pk)
    inv.refresh_from_db()
    inventory['invoices'].append({'scenario':'overdue-sale','id':inv.pk,'number':inv.invoice_number,
        'user_id':user.pk,'type':'sales','status':inv.status,'total':str(inv.total_amount)})


@transaction.atomic
def populate():
    root = require_demo()
    if get_user_model().objects.exists() or Invoice.objects.exists():
        raise ValidationError('Only a clean demo database can be populated.')
    users = []
    temporary_passwords = {}
    for username in ('lumen-owner', 'cobalt-owner', 'security-reviewer'):
        password = secrets.token_urlsafe(32)
        user = get_user_model().objects.create_user(username, f'{username}@example.test', password)
        users.append(user); temporary_passwords[username] = password
    reviewer = users[2]; reviewer.is_staff = True; reviewer.save(update_fields=['is_staff'])
    reviewer.user_permissions.add(Permission.objects.get(content_type__app_label='audit', codename='manage_security'))
    businesses = []
    for index, name in enumerate(('Lumen Demo Trading', 'Cobalt Demo Services')):
        business = BusinessProfile(user=users[index], business_name=name, legal_name=name,
            business_type='proprietorship', gstin=gstin(f'27ABCDE{8100+index}F1Z'),
            state='Maharashtra', email=f'business-{index+1}@example.test')
        business.full_clean(); business.save(); businesses.append(business)
    customers, suppliers = [], []
    for role, names, destination in (
        ('customer', ['Amber Demo Retail', 'Birch Demo Studio', 'Cloud Demo Books', 'Dawn Demo Labs', 'Elm Demo Market'], customers),
        ('vendor', ['Fable Demo Supplies', 'Grove Demo Networks', 'Harbor Demo Rentals', 'Iris Demo Software', 'Juniper Demo Logistics'], suppliers)):
        for index, name in enumerate(names):
            owner = businesses[0] if index < 4 else businesses[1]
            party = BusinessParty(business=owner, name=name, party_type=role, state='Maharashtra',
                email=f'{role}-{index+1}@example.test',
                gstin=gstin(f'27ABCDE{8200+index}F1Z') if role == 'vendor' else '')
            party.full_clean(); party.save(); destination.append(party)
    both = BusinessParty(business=businesses[0], name='Kite Demo Exchange', party_type='both',
        email='kite@example.test', gstin=gstin('27ABCDE8300F1Z'), state='Maharashtra')
    both.full_clean(); both.save()
    inventory = {'version': 1, 'accounts': [{'username': u.username, 'email': u.email, 'id': u.pk} for u in users],
        'businesses': [{'id': b.pk, 'name': b.business_name} for b in businesses], 'invoices': [],
        'commercial': [], 'adjustments': [], 'samples': [], 'checks': {}}
    scenarios = {}
    # 18 sales and 10 purchases; all numbering comes from existing application services.
    for kind, count in [('sales', 18), ('purchase', 10)]:
        for index in range(count):
            owner = 1 if index >= (15 if kind == 'sales' else 8) else 0
            party = (customers[4] if owner else customers[index % 4]) if kind == 'sales' else (suppliers[4] if owner else suppliers[index % 4])
            day = date(2026, 10, 1 + index) if index < 6 else DAY
            data = header(party, day)
            rows = [line(str(1000 + index * 100))]
            scenario = f'{kind}-{index+1:02}'
            if index == 0:
                scenario = 'historical' if kind == 'sales' else 'intra-purchase'
                rows = [line('1000', '0' if kind == 'sales' else '18')]
            elif index == 1:
                scenario = 'mixed-sale' if kind == 'sales' else 'mixed-purchase'
                data['party'] = both; rows = [line('100' if kind == 'sales' else '80', '0')]
            elif index == 2:
                scenario = 'intra-sale' if kind == 'sales' else 'inter-purchase'; rows = [line()]
                if kind == 'purchase': data['place_of_supply'] = '29'
            elif index == 3:
                scenario = 'inter-sale' if kind == 'sales' else 'inclusive-purchase'; rows = [line()]
                if kind == 'sales': data['place_of_supply'] = '29'
                else: data['pricing_mode'] = 'inclusive'; rows = [line('1180.03')]
            elif index == 4:
                scenario = 'discount-sale' if kind == 'sales' else 'mixed-rates-purchase'
                if kind == 'sales':
                    rows[0]['discount'] = D('10'); rows[0]['discount_type'] = 'percent'
                    data['invoice_discount_type'] = 'percent'; data['invoice_discount_value'] = D('5')
                else: rows = [line('1000', '18'), line('500', '5')]
            elif index == 5:
                scenario = 'inclusive-sale' if kind == 'sales' else scenario
                data['pricing_mode'] = 'inclusive'; rows = [line('1180.03')]
            if kind == 'purchase': data['invoice_number'] = f'DEMO-P-{index+1:03}'
            req = request_for(users[owner])
            inv = save_draft(request=req, business=businesses[owner], invoice_type=kind,
                data=data, line_data=rows, creation_key=uuid.uuid4())
            if not (kind == 'sales' and index == 17):
                finalize_invoice(request=req, invoice_id=inv.pk)
            inv.refresh_from_db(); scenarios[scenario] = inv
            inventory['invoices'].append({'scenario': scenario, 'id': inv.pk, 'number': inv.invoice_number,
                'user_id': inv.user_id, 'type': kind, 'status': inv.status, 'total': str(inv.total_amount)})
    # Notes apply to separate originals, leaving golden checkpoints untouched.
    for kind in ('credit', 'debit'):
        for index, source in enumerate((scenarios['sales-07'], scenarios['purchase-07'])):
            note = save_adjustment(request=request_for(source.user), invoice_id=source.pk, kind=kind,
                creation_key=uuid.uuid4(), data=dict(document_date=date(2026,11,5), reason='Fictional demo correction',
                pricing_mode='exclusive', tax_effect='gst', supplier_reference=f'DEMO-{kind.upper()}-{index+1}',
                eligibility_reviewed=True, annual_return_date=None, invoice_discount_type='fixed', invoice_discount_value=D('0'), notes='Synthetic only'),
                rows=[{'original_line': source.line_items.first(), 'mode':'value', 'quantity':D('0'), 'amount':D('100')}])
            transition_adjustment(request=request_for(source.user), note_id=note.pk, action='finalize')
            inventory['adjustments'].append({'id':note.pk,'number':note.number,'kind':kind,'original_id':source.pk,'total':str(note.total_amount)})
    for index, kind in enumerate(('quotation','quotation','quotation','proforma','proforma')):
        owner = 1 if index == 2 else 0
        data = header(customers[4] if owner else customers[0], DAY, due_date=date(2027,3,1))
        document = save_commercial(request=request_for(users[owner]), kind=kind, data=data,
            line_data=[line()], creation_key=uuid.uuid4())
        if index != 2:
            transition_commercial(request=request_for(users[owner]), document_id=document.pk, action='issue')
        if index == 1:
            transition_commercial(request=request_for(users[owner]), document_id=document.pk, action='accept', notes='Fictional customer decision')
        inventory['commercial'].append({'id':document.pk,'number':document.number,'kind':kind})
    categories = [ExpenseCategory.objects.create(business=b, name='Demo Operations') for b in businesses]
    expense_invoices = []
    for index in range(16):
        owner = 1 if index >= 13 else 0; supplier = suppliers[4] if owner else suppliers[index % 4]
        reference = f'DEMO-EXP-{index+1:03}'
        inclusive = index == 1
        data = {'title': f'Fictional operating expense {index+1}', 'category': categories[owner].pk,
            'description':'Synthetic manual-testing expense', 'expense_date':DAY.isoformat(),
            'due_date':'2026-11-15','reference_number':reference,'supplier_party':supplier.pk,
            'supplier_name':'','supplier_gstin':'','supplier_email':'','supplier_phone':'',
            'supplier_state_code':'27','place_of_supply':'27','supply_category':'standard','tax_treatment':'taxable',
            'amount':'1180.03' if inclusive else str(1000+index*25),'gst_rate':'18',
            'gst_inclusive':'inclusive' if inclusive else 'exclusive','discount':'0',
            'payment_status':'unpaid','amount_paid':'0','notes':'Fictional demo only', 'acknowledge_duplicates':'on'}
        taxable = '1000.03' if inclusive else str(1000+index*25)
        tax = '90.00' if inclusive else str((D(taxable)*D('.09')).quantize(D('.01')))
        total = '1180.03' if inclusive else str(D(taxable)+D(tax)*2)
        attachment = SimpleUploadedFile(reference+'.pdf', receipt_bytes(reference,supplier,businesses[owner],taxable,tax,total),content_type='application/pdf')
        form = ExpenseForm(data, {'attachment': attachment}, business=businesses[owner])
        if not form.is_valid(): raise ValidationError(str(form.errors))
        invoice = _save_expense(request_for(users[owner]), form)
        if invoice is None: raise ValidationError(str(form.errors))
        expense_invoices.append(invoice)
        inventory['samples'].append({'expense_id':invoice.pk,'receipt_reference':reference,'storage':invoice.expense_detail.attachment.name})
    schedules = []
    for index, business in enumerate(businesses):
        start = date(2026,11,1)
        schedule = RecurringExpense(business=business, category=categories[index], supplier=suppliers[0 if index==0 else 4],
            title=f'Demo inclusive subscription {index+1}', taxable_amount=D('1180.03'), gst_rate=D('18'),gst_inclusive=True,
            frequency='monthly',start_date=start,next_due_date=start,schedule_day=1,supplier_state_code='27',
            place_of_supply='27',supply_category='standard',tax_treatment='taxable')
        schedule.full_clean(); schedule.save(); schedules.append(schedule)
        generated = generate_due_expenses(business, as_of=start, actor=users[index], allow_backfill=True)
        assert len(generated)==1
        assert not generate_due_expenses(business, as_of=start, actor=users[index], allow_backfill=True)
    inventory['checks']['recurrence_idempotent'] = True
    # Reviews are explicit decisions on synthetic evidence, not a statutory eligibility claim.
    for index, status in enumerate(('confirmed','potential','ineligible','pending','reversed')):
        inv = expense_invoices[index]
        if status == 'reversed':
            review_itc(request_for(inv.user),inv,'confirmed','Synthetic prior confirmation for reversal exercise.',True)
        review_itc(request_for(inv.user),inv,status,'Synthetic owner review for demo; no statutory ITC claim.',True)
    # Payments use normal form/ledger endpoints and must persist exactly once.
    for index, (inv, amount, day) in enumerate([
        (scenarios['historical'],D('1000'),date(2026,11,10)),
        (scenarios['intra-sale'],D('500'),date(2026,11,5)),
        (scenarios['sales-08'],scenarios['sales-08'].total_amount,date(2026,11,10)),
        (scenarios['purchase-08'],D('500'),date(2026,11,10)),
        (scenarios['intra-purchase'],D('1180'),date(2026,11,10)),
        (expense_invoices[0],D('500'),date(2026,11,10)),
        (expense_invoices[2],expense_invoices[2].total_amount,date(2026,11,10))]):
        client = Client(); client.force_login(inv.user)
        endpoint = reverse('expense_payment',args=[inv.pk]) if inv.invoice_type=='expense' else reverse('party_detail',args=[inv.buyer_party_id or inv.supplier_party_id])
        before = InvoicePayment.objects.count()
        result = client.post(endpoint, {'invoice':inv.pk,'payment_date':day.isoformat(),'amount':str(amount),
            'payment_mode':'bank_transfer','reference_id':f'DEMO-PAY-{index+1:03}','notes':'Fictional recorded payment only'})
        if result.status_code != 302 or InvoicePayment.objects.count()!=before+1:
            raise ValidationError('Normal application payment workflow rejected a demo payment.')
    # Actual extraction on supplied documents remains provisional: no OCR confirmation/finalization.
    from invoices.extraction_services import process_upload
    from invoices.extraction import extract_invoice_data
    for relative in ('sample_data/feature_acceptance/documents/EXCLUSIVE-18.pdf','sample_data/feature_acceptance/ocr/invoice.png'):
        path = settings.BASE_DIR / relative
        inv, warnings = process_upload(request=request_for(users[0]),business=businesses[0],batch=None,
            invoice_type='expense',upload=SimpleUploadedFile(path.name,path.read_bytes()),extractor=extract_invoice_data)
        assert inv.status=='review'
        inventory['samples'].append({'source':relative,'provisional_invoice_id':inv.pk,'extraction_attempt_id':inv.extraction_attempts.first().pk})
    # Reconciliation uploads use normal parser, matching and scoped persistence endpoint.
    records = []
    for index, inv in enumerate((scenarios['intra-purchase'],scenarios['inter-purchase'])):
        records.append({'supplier_gstin':inv.supplier_gstin,'invoice_number':inv.invoice_number,
            'invoice_date':str(inv.invoice_date),'taxable_value':str(inv.taxable_amount),
            'cgst_amount':str(inv.cgst_amount),'sgst_amount':str(inv.sgst_amount),
            'igst_amount':str(inv.igst_amount+(D('5') if index else D('0'))),
            'total_tax':str(inv.total_tax+(D('5') if index else D('0')))})
    client = Client(); client.force_login(users[0])
    response=client.post(reverse('reconciliation_upload'), {'return_period':'2026-10',
        'source_file':SimpleUploadedFile('demo-reconciliation.json',json.dumps(records).encode(),content_type='application/json')})
    if response.status_code!=302: raise ValidationError('Reconciliation upload rejected demo source.')
    period = preparation.create_period(request_for(users[0]),'monthly',date(2026,10,1),date(2026,10,31))
    inputs = dict(ordinary_domestic=True,aato_band='up_to_5cr',import_runs=[],registration={str(i.pk):{'status':'unregistered','reason':'Fictional unregistered customer'} for i in Invoice.objects.filter(user=users[0],invoice_type='sales',status__in=['verified','paid'],invoice_date__range=[period.start_date,period.end_date]) if not i.buyer_gstin})
    preview,_,_=preparation.preview(period,'gstr1',inputs)
    first=preparation.prepare(request_for(users[0]),period,'gstr1',inputs,uuid.uuid4(),preparation.digest(preview),None,True)
    revised=preparation.prepare(request_for(users[0]),period,'gstr1',inputs,uuid.uuid4(),preparation.digest(preview),first.pk,True)
    preparation.transition(request_for(users[0]),revised,'reviewed','Synthetic source review; not filed.')
    annual=preparation.create_period(request_for(users[0]),'financial_year',date(2025,4,1),date(2026,3,31))
    income=dict(taxpayer='resident_individual',regime='new',resident_confirmed=True,ordinary_only=True,taxable_income='1600000',deductions='0',assessment_year='2026-27')
    data,_,_=preparation.preview(annual,'income_tax',income)
    income_paper=preparation.prepare(request_for(users[0]),annual,'income_tax',income,uuid.uuid4(),preparation.digest(data),None,True)
    inventory['tax_papers']=[{'id':p.pk,'kind':p.kind,'revision':p.revision} for p in (first,revised,income_paper)]
    # Trigger supported detection rules, retaining genuine synthetic event -> alert history.
    from audit.security import transition_alert
    from audit.models import SecurityAlert
    for event_type in ('AUTH_LOGIN_FAILURE','FILE_UPLOAD_REJECTED','ACCESS_PERMISSION_DENIED'):
        for _ in range(5):
            record_event(request=request_for(users[0]),business=businesses[0],action='security',module='demo',
                description='Synthetic local acceptance scenario, not a real incident.',success=False,
                event_type=event_type,principal='synthetic-'+event_type,source='operator')
    alerts=list(SecurityAlert.objects.order_by('pk'))
    if len(alerts)<3: raise ValidationError('Synthetic supported rules did not produce alerts.')
    transition_alert(request_for(reviewer),alerts[-2].pk,'INVESTIGATING','Synthetic reviewer investigation.')
    transition_alert(request_for(reviewer),alerts[-1].pk,'RESOLVED','Synthetic review completed; not an actual incident.')
    # Confirm email authentication using temporary random secrets, then remove all usable seed passwords.
    for user in users:
        client=Client()
        if not client.login(email=user.email,password=temporary_passwords[user.username]):
            raise ValidationError('Email authentication failed for synthetic user.')
    inventory['checks'].update(checkpoints(scenarios,both))
    inventory['checks']['email_login_all_three']=True
    for user in users:
        user.set_unusable_password();user.save(update_fields=['password'])
    add_overdue_example(inventory)
    inventory['counts']=counts()
    return inventory


def checkpoints(scenarios,both):
    inv=Invoice.objects.get(pk=scenarios['historical'].pk)
    values={'historical_october_outstanding':str(invoice_outstanding(inv,as_of=date(2026,10,31))),
        'historical_november_outstanding':str(invoice_outstanding(inv,as_of=date(2026,11,30)))}
    assert D(values['historical_october_outstanding'])==D('1000') and D(values['historical_november_outstanding'])==0
    last=party_ledger_entries(both.business.user,both)[-1]
    assert last['receivable_balance']==D('100') and last['payable_balance']==D('80')
    for name, amounts in [('intra-sale',('1000','90','90','0','1180')),('inter-sale',('1000','0','0','180','1180'))]:
        invoice=scenarios[name]; actual=tuple(getattr(invoice,k) for k in ('taxable_amount','cgst_amount','sgst_amount','igst_amount','total_amount'))
        assert actual==tuple(D(v) for v in amounts)
    values.update(mixed_receivable='100.00',mixed_payable='80.00',intra_total='1180.00',inter_total='1180.00')
    return values


@transaction.atomic
def add_acceptance_examples(inventory):
    """Append missing demo lifecycle states without changing existing examples."""
    require_demo()
    if inventory.get('acceptance_examples'):
        return
    from invoices.commercial_services import convert_commercial
    owner = get_user_model().objects.get(username='lumen-owner')
    business = BusinessProfile.objects.get(user=owner)
    customer = BusinessParty.objects.filter(business=business, party_type='customer', is_active=True).order_by('pk').first()
    request = request_for(owner)
    examples = []
    for kind in ('quotation', 'proforma'):
        for state in ('draft', 'rejected', 'cancelled', 'converted', 'expired'):
            key = uuid.uuid5(uuid.NAMESPACE_URL, 'smart-invoice-demo/acceptance/' + kind + '/' + state)
            document = save_commercial(request=request, kind=kind,
                data=header(customer, date(2026,10,1) if state == 'expired' else DAY,
                            due_date=date(2026,10,31) if state == 'expired' else date(2027,3,1)),
                line_data=[line()], creation_key=key)
            if document.status == 'draft' and state in {'rejected', 'converted'}:
                transition_commercial(request=request, document_id=document.pk, action='issue')
                transition_commercial(request=request, document_id=document.pk, action='reject' if state == 'rejected' else 'accept', notes='Fictional demo lifecycle only')
                if state == 'converted':
                    # Conversion creates a draft only; no additional financial invoice is finalized.
                    convert_commercial(request=request, document_id=document.pk, invoice_date=DAY)
            elif document.status == 'draft' and state == 'cancelled':
                transition_commercial(request=request, document_id=document.pk, action='cancel')
            document.refresh_from_db()
            examples.append({'model':'commercial', 'id':document.pk, 'number':document.number,
                             'scenario':kind+'-'+state, 'status':document.effective_status,
                             'converted_invoice_id':document.converted_invoice_id})
    source = Invoice.objects.filter(user=owner, entry_mode='manual', document_state='finalized', invoice_type='sales', cgst_amount__gt=0).order_by('pk').first()
    for kind in ('credit', 'debit'):
        for state in ('draft', 'cancelled'):
            note = save_adjustment(request=request, invoice_id=source.pk, kind=kind,
                creation_key=uuid.uuid5(uuid.NAMESPACE_URL, 'smart-invoice-demo/acceptance/'+kind+'/'+state),
                data=dict(document_date=date(2026,11,5), reason='Fictional draft/cancel demonstration',
                          pricing_mode='exclusive', tax_effect='gst', supplier_reference='',
                          eligibility_reviewed=True, annual_return_date=None, invoice_discount_type='fixed',
                          invoice_discount_value=D('0'), notes='Synthetic; not a live financial adjustment'),
                rows=[{'original_line':source.line_items.first(), 'mode':'value', 'quantity':D('0'), 'amount':D('10')}])
            if state == 'cancelled' and note.status == 'draft':
                transition_adjustment(request=request, note_id=note.pk, action='cancel', reason='Fictional cancelled-draft demonstration')
            note.refresh_from_db()
            examples.append({'model':'adjustment','id':note.pk,'number':note.number,
                             'scenario':kind+'-'+state,'status':note.status,'original_id':source.pk})
    inventory['acceptance_examples'] = examples
    inventory['counts'] = counts()


@transaction.atomic
def add_invoice_management_examples(inventory):
    """Stage three repeatable, unconfirmed examples for the new action pages."""
    require_demo()
    if inventory.get('invoice_management_examples'):
        return
    from invoices.extraction_services import process_upload
    from invoices.extraction import extract_invoice_data
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.pagesizes import A4
    user = get_user_model().objects.get(username='lumen-owner')
    business = BusinessProfile.objects.get(user=user)
    examples = []
    for reference, state, cancel in (('DEMO-TYPE-INTERSTATE','29',False),('DEMO-TYPE-EXPENSE','27',False),('DEMO-RESTORE-PROVISIONAL','27',True)):
        invoice = Invoice.objects.filter(user=user, invoice_number=reference).first()
        if invoice is None:
            supplier = BusinessParty.objects.filter(business=business, party_type__in=['vendor','both'], gstin__startswith=state, is_active=True).exclude(gstin=business.gstin).first()
            if supplier is None:
                supplier = BusinessParty.objects.create(business=business, name='Synthetic Management Supplier '+state,
                    party_type='vendor',gstin=gstin(state+'ABCDE8401F1Z'),state='Karnataka' if state=='29' else 'Maharashtra',email='management-supplier-'+state+'@example.test')
                supplier.full_clean()
            interstate = state != business.gstin[:2]
            stream=BytesIO();page=Canvas(stream,pagesize=A4,invariant=1);text=page.beginText(35,800);text.setFont('Helvetica',10)
            printed=['SYNTHETIC TEST ONLY - NOT A REAL TRANSACTION','TAX INVOICE',
                'Invoice Number: '+reference,'Invoice Date: 01-Nov-2026','Due Date: 30-Nov-2026',
                'Supplier: '+supplier.name,'Supplier GSTIN: '+supplier.gstin,
                'Buyer: '+business.business_name,'Buyer GSTIN: '+business.gstin,
                'Supplier State Code: '+state,'Place of Supply: '+business.gstin[:2],
                'Pricing: exclusive','Supply Category: standard','Taxable Value: INR 1000.00',
                'CGST: INR '+('0.00' if interstate else '90.00'),'SGST: INR '+('0.00' if interstate else '90.00'),
                'IGST: INR '+('180.00' if interstate else '0.00'),'Other Charges: INR 0.00','Rounding Adjustment: INR 0.00',
                'Grand Total: INR 1180.00','1 Synthetic service 9983 1 1000.00 1000.00 18%']
            for row in printed:text.textLine(row)
            page.drawText(text);page.save()
            invoice,_=process_upload(request=request_for(user),upload=SimpleUploadedFile(reference+'.pdf',stream.getvalue(),content_type='application/pdf'),invoice_type='sales',business=business,batch=None,extractor=extract_invoice_data)
            if cancel:
                client=Client();client.force_login(user)
                response=client.post(reverse('invoice_delete',args=[invoice.pk]))
                if response.status_code!=302:raise ValidationError('Synthetic cancellation did not complete.')
                invoice.refresh_from_db()
        examples.append({'id':invoice.pk,'number':invoice.invoice_number,'type':invoice.invoice_type,'status':invoice.status,
                         'scenario':reference,'attempt_id':invoice.extraction_attempts.first().pk})
    inventory['invoice_management_examples']=examples
    inventory['counts']=counts()
