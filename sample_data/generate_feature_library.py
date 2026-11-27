"""Additional fictional feature fixtures. Pure file generation; never imports Django models."""
import argparse
import csv
import hashlib
import json
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from invoices.manual_calculations import calculate_invoice
from invoices.gstin import ALPHABET, gstin_evidence
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent / 'feature_acceptance'
MONEY = ('taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount')


def gstin(prefix):
    total = 0
    for index, char in enumerate(reversed(prefix)):
        product = ALPHABET.index(char) * (2 if index % 2 == 0 else 1)
        total += product // 36 + product % 36
    result = prefix + ALPHABET[(36 - total % 36) % 36]
    assert gstin_evidence(result)['checksum_valid']
    return result


PARTIES = [
    {'key': 'business', 'name': 'Sandbox Feature Lab (Fictional)', 'party_type': 'business', 'gstin': gstin('27ABCDE1234F1Z'), 'state_code': '27', 'email': 'business@example.test'},
    {'key': 'customer', 'name': 'Synthetic Feature Customer', 'party_type': 'customer', 'gstin': gstin('27FGHIJ2468K1Z'), 'state_code': '27', 'email': 'customer@example.test'},
    {'key': 'interstate_customer', 'name': 'Synthetic Interstate Customer', 'party_type': 'customer', 'gstin': gstin('29ABCDE9876G1Z'), 'state_code': '29', 'email': 'customer-interstate@example.test'},
    {'key': 'vendor', 'name': 'Synthetic Feature Supplier', 'party_type': 'vendor', 'gstin': gstin('27PQRST5678L1Z'), 'state_code': '27', 'email': 'supplier@example.test'},
    {'key': 'interstate_vendor', 'name': 'Synthetic Interstate Supplier', 'party_type': 'vendor', 'gstin': gstin('29LMNOP4321A1Z'), 'state_code': '29', 'email': 'interstate@example.test'},
    {'key': 'both', 'name': 'Synthetic Customer and Supplier', 'party_type': 'both', 'gstin': gstin('27UVWXY1357B1Z'), 'state_code': '27', 'email': 'both@example.test'},
]
P = {item['key']: item for item in PARTIES}


def line(rate='1000.00', tax='18', quantity='1', treatment='taxable', **updates):
    result = {'description': 'Synthetic service', 'hsn_sac_code': '9983', 'quantity': quantity, 'unit': 'Nos', 'rate': rate, 'tax_rate': tax, 'tax_treatment': treatment, 'discount_type': 'fixed', 'discount': '0'}
    result.update(updates)
    return result


def cases():
    definitions = [
        ('EXCLUSIVE-18', 'sales', [line()], {}),
        ('INCLUSIVE-ROUNDING', 'expense', [line('1180.03')], {'pricing_mode': 'inclusive'}),
        ('INTERSTATE-18', 'purchase', [line(quantity='2')], {'supplier_state': '29'}),
        ('MIXED-RATES', 'sales', [line('100', rate) for rate in ('0','5','12','18','28')] + [line('100','0',treatment=t) for t in ('exempt','nil_rated','non_gst')], {}),
        ('DISCOUNTS', 'sales', [line('500','18','2',discount_type='percent',discount='5')], {'invoice_discount_type': 'percent', 'invoice_discount': '10'}),
        ('CLASSIFICATIONS', 'purchase', [line('100','0',treatment=t) for t in ('taxable','exempt','nil_rated','non_gst')], {}),
        ('HISTORICAL-PAYMENT', 'sales', [line()], {'invoice_date': '2026-10-01'}),
        ('BOTH-SALE', 'sales', [line('100','0')], {'party_key': 'both'}),
        ('BOTH-PURCHASE', 'purchase', [line('80','0')], {'party_key': 'both'}),
        ('PURCHASE-18', 'purchase', [line()], {}),
        ('MULTIPAGE', 'sales', [line('10',description=f'Synthetic item {i:02}') for i in range(1,61)], {}),
        ('ROUND-OFF', 'purchase', [line()], {'adjustment': '0.50'}),
    ]
    result = []
    for key, kind, rows, options in definitions:
        options = dict(options)
        item = {'case_id': key, 'reference_number': 'FEATURE-'+key, 'invoice_type': kind, 'invoice_date': options.pop('invoice_date','2026-11-01'), 'due_date': '2026-11-30', 'party_key': options.pop('party_key','customer' if kind=='sales' else 'interstate_vendor' if key=='INTERSTATE-18' else 'vendor'), 'lines': rows, 'supplier_state': options.pop('supplier_state','27'), 'place_of_supply': '27', 'pricing_mode': options.get('pricing_mode','exclusive'), 'options': options}
        calculated = calculate_invoice(rows,item['supplier_state'],item['place_of_supply'],**options)
        item['expected'] = {k:str(calculated[k]) for k in MONEY}
        item['expected_treatment_breakdown'] = {k:str(v) for k,v in calculated['treatment_breakdown'].items()}
        item['expected_line_values'] = [{k:str(row[k]) for k in (*MONEY,'tax_amount','invoice_discount_allocated')} for row in calculated['lines']]
        result.append(item)
    return result


def csv_bytes(headers, rows):
    stream = StringIO(newline=''); writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


def text_pdf(item, missing=False, ambiguous=False):
    output=BytesIO(); pdf=canvas.Canvas(output,pagesize=A4,invariant=1)
    pdf.setTitle('FICTIONAL LOCAL TEST - '+item['case_id'])
    supplier=P['business'] if item['invoice_type']=='sales' else P[item['party_key']]
    buyer=P['customer'] if item['invoice_type']=='sales' else P['business']
    if item['party_key']=='both':
        if item['invoice_type']=='sales':buyer=P['both']
        else:supplier=P['both']
    top=['TAX INVOICE','FICTIONAL TEST DOCUMENT - NOT FOR FILING OR PAYMENT',
         'Supplier Name: '+supplier['name'],'Supplier GSTIN: '+supplier['gstin'],
         'Buyer Name: '+buyer['name'],'Buyer GSTIN: '+buyer['gstin']]
    if not missing:top += ['Invoice Number: '+item['reference_number']]
    top += ['Invoice Date: '+('03/04/2026' if ambiguous else date.fromisoformat(item['invoice_date']).strftime('%d-%b-%Y')),'Due Date: 30-Nov-2026',
            'Supplier State Code: '+item['supplier_state'],'Place of Supply: '+item['place_of_supply'],
            'Pricing Mode: '+item['pricing_mode'],'Supply Category: standard']
    pdf.setFont('Helvetica',9); y=A4[1]-36
    for text in top:pdf.drawString(32,y,text);y-=16
    pdf.drawString(32,y,'Item   Description   HSN   Qty   Rate   Taxable Value   GST');y-=18
    for i,(row,values) in enumerate(zip(item['lines'],item['expected_line_values']),1):
        if y<145:
            pdf.showPage();pdf.setFont('Helvetica',9);y=A4[1]-40;pdf.drawString(32,y,'FICTIONAL TEST - line items continued');y-=22
        text=f"{i} {row['description']} {row['hsn_sac_code']} {row['quantity']} {Decimal(row['rate']):.2f} {Decimal(values['taxable_amount']):.2f} {row['tax_rate']}%"
        pdf.drawString(32,y,text);y-=17
    y-=12
    for label,key in [('Taxable Amount','taxable_amount'),('CGST','cgst_amount'),('SGST','sgst_amount'),('IGST','igst_amount'),('Grand Total','total_amount')]:
        pdf.drawString(32,y,f'{label}: INR {item["expected"][key]}');y-=17
    pdf.drawString(32,max(28,y),'Tax treatments/discounts must be reviewed using the accompanying JSON. Never auto-finalize OCR.')
    pdf.save();return output.getvalue()


def invoice_image(item):
    image=Image.new('RGB',(1654,2339),'white');draw=ImageDraw.Draw(image)
    fontpath=next((p for p in [Path('/System/Library/Fonts/Supplemental/Arial.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists()),None)
    font=ImageFont.truetype(str(fontpath),30) if fontpath else ImageFont.load_default(size=30)
    texts=['TAX INVOICE','FICTIONAL LOCAL TEST - NOT FOR FILING', 'Supplier Name: '+P['vendor']['name'],'Supplier GSTIN: '+P['vendor']['gstin'], 'Buyer Name: '+P['business']['name'],'Buyer GSTIN: '+P['business']['gstin'],'Invoice Number: '+item['reference_number'],'Invoice Date: 01-Nov-2026','Supplier State Code: 27','Place of Supply: 27','Pricing Mode: exclusive','Supply Category: standard','1 Synthetic service 9983 1 1000.00 1000.00 18%','Taxable Amount: INR 1000.00','CGST: INR 90.00','SGST: INR 90.00','IGST: INR 0.00','Grand Total: INR 1180.00']
    for i,text in enumerate(texts):draw.text((65,70+i*55),text,fill='black',font=font)
    return image


def image_pdf(images):
    out=BytesIO();pdf=canvas.Canvas(out,pagesize=A4,invariant=1)
    for image in images:
        pdf.drawImage(ImageReader(image),0,0,width=A4[0],height=A4[1]);pdf.showPage()
    pdf.save();return out.getvalue()


def generate(output=ROOT):
    output=Path(output)
    if output.exists():raise FileExistsError('Destination already exists; nothing overwritten. Use --verify or a new --output folder.')
    output.mkdir(parents=True)
    files={}
    def put(name,data,usage):
        path=output/name;path.parent.mkdir(parents=True,exist_ok=True)
        if isinstance(data,str):data=data.encode('utf-8')
        with path.open('xb') as target:target.write(data)
        files[name]={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'usage':usage}
    def js(name,value,usage='Reference only; not an application import format.'):
        put(name,json.dumps(value,indent=2,ensure_ascii=False,default=str)+'\n',usage)
    data=cases();by_id={c['case_id']:c for c in data}
    js('manual_entry/invoice_cases.json',data)
    put('manual_entry/parties.csv',csv_bytes(list(PARTIES[0]),PARTIES),'Manual party/profile reference; fictional .test email addresses only.')
    expected=[{'case_id':c['case_id'],**c['expected'],**{'treatment_'+k:v for k,v in c['expected_treatment_breakdown'].items()}} for c in data]
    put('expected/invoice_values.csv',csv_bytes(list(expected[0]),expected),'Expected saved GST engine values; independent cases, not a combined ledger.')
    for item in data:put('documents/'+item['case_id']+'.pdf',text_pdf(item),'Upload as matching invoice type, review fields/lines; alternative to manual-entry case, not an additional transaction.')
    put('negative/missing_number.pdf',text_pdf(by_id['PURCHASE-18'],missing=True),'Uploadable negative fixture: missing invoice number; mandatory review.')
    put('negative/ambiguous_date.pdf',text_pdf(by_id['PURCHASE-18'],ambiguous=True),'Uploadable negative fixture: ambiguous day/month; do not silently choose.')
    original=files['documents/EXCLUSIVE-18.pdf']
    put('negative/exact_duplicate.pdf',(output/'documents/EXCLUSIVE-18.pdf').read_bytes(),'Upload after EXCLUSIVE-18 original; exact SHA-256 duplicate.')
    put('negative/corrupt.pdf',b'%PDF-1.7\nINTENTIONALLY CORRUPT FICTIONAL TEST\n','Intentionally invalid; rejection expected, not an invoice.')
    put('negative/corrupt.jpg',b'INTENTIONALLY INVALID IMAGE - FICTIONAL TEST\n','Intentionally invalid; rejection expected.')
    js('negative/validation_and_rendering.json',[
        {'case':'NEGATIVE-QUANTITY','input':{'quantity':'-1'},'expected':'Field validation rejects; no saved financial action'},
        {'case':'DISCOUNT-OVER-VALUE','input':{'rate':'100','discount':'101'},'expected':'Calculation rejects'},
        {'case':'UNSUPPORTED-RATE','input':{'tax_rate':'17'},'expected':'Rate selection/engine rejects'},
        {'case':'OVERPAYMENT','base_case':'EXCLUSIVE-18','payment_amount':'1180.01','expected':'Reject with zero payment rows created'},
        {'case':'FORMULA-NAME','party_name':'=1+1','expected':'Display as text; exported CSV/XLSX text must be formula-neutralized'},
        {'case':'HTML-NAME','party_name':'<b>Synthetic test</b>','expected':'Escape markup; do not interpret as HTML'},
        {'case':'BAD-CHECKSUM','gstin':P['vendor']['gstin'][:-1]+('0' if P['vendor']['gstin'][-1]!='0' else '1'),'expected':'Local checksum validation fails'},
        {'case':'DATE-UNDERFLOW','query':{'end_date':'0001-01-01'},'expected':'Validation error; KNOWN CURRENT BUG: OverflowError'},
    ])
    receipt=dict(by_id['PURCHASE-18']);receipt['reference_number']='FEATURE-RECEIPT-OCR'
    image=invoice_image(receipt)
    for name,fmt,img,options in [('invoice.png','PNG',image,{}),('invoice.jpg','JPEG',image,{'quality':90}),('rotated.png','PNG',image.rotate(90,expand=True),{}),('low_quality.jpg','JPEG',image.resize((500,707)).filter(ImageFilter.GaussianBlur(.8)),{'quality':35})]:
        out=BytesIO();img.save(out,format=fmt,**options);put('ocr/'+name,out.getvalue(),'Upload alternative of FEATURE-RECEIPT-OCR; compare with ground_truth.json. Never count alternatives as extra expenses.')
    put('ocr/scanned.pdf',image_pdf([image]),'Image-only scanned PDF; OCR required, not embedded text.')
    continuation=Image.new('RGB',image.size,'white');d=ImageDraw.Draw(continuation);d.text((60,90),'FICTIONAL CONTINUATION - NO ADDITIONAL TRANSACTION',fill='black',font=ImageFont.load_default(size=28))
    put('ocr/multipage_scanned.pdf',image_pdf([image,continuation]),'Two image-only pages of one invoice; do not add the header total twice.')
    from pypdfium2 import PdfDocument
    text_doc=PdfDocument((output/'documents/PURCHASE-18.pdf').read_bytes());scan_doc=PdfDocument((output/'ocr/scanned.pdf').read_bytes())
    try:
        text_doc.import_pages(scan_doc);out=BytesIO();text_doc.save(out);put('ocr/mixed_text_scan.pdf',out.getvalue(),'Two distinct source invoice references deliberately mixed: require conflict review, never merge automatically.')
    finally:text_doc.close();scan_doc.close()
    js('ocr/ground_truth.json',{'invoice_number':receipt['reference_number'],'invoice_date':'2026-11-01','supplier_name':P['vendor']['name'],'supplier_gstin':P['vendor']['gstin'],'buyer_gstin':P['business']['gstin'],**receipt['expected'],'line_items':receipt['lines'],'variants':'Use one image/PDF alternative per clean scenario. Mixed PDF intentionally has conflicting invoice numbers. Low-quality/rotation accuracy is not guaranteed; record raw errors.'})
    js('commercial/quotations_proformas.json',[
        {'case_id':'QUOTE-ACCEPT-CONVERT','kind':'quotation','customer_key':'customer','source_case':'EXCLUSIVE-18','document_date':'2026-11-01','valid_until':'2026-11-30','steps':['Save draft','Issue','Accept','Convert once using actual assigned IDs/numbers'],'expected_estimate':by_id['EXCLUSIVE-18']['expected'],'financial_recognition':'None until conversion creates/finalizes a sales invoice; repeating conversion must not duplicate it'},
        {'case_id':'PROFORMA-CANCEL','kind':'proforma','customer_key':'customer','source_case':'INTERSTATE-18','customer_key':'interstate_customer','supplier_state':'27','place_of_supply':'29','document_date':'2026-11-01','valid_until':'2026-11-30','steps':['Save','Issue','Cancel'],'expected_estimate':by_id['INTERSTATE-18']['expected'],'financial_recognition':'No payments, sales or receivables'},
        {'case_id':'QUOTE-EXPIRED','kind':'quotation','document_date':'2026-10-01','valid_until':'2026-10-31','source_case':'EXCLUSIVE-18','expected':'Expiry depends on current local date; not a live email test'},
    ])
    js('adjustments/credit_debit_workflows.json',{'requires':'Create EXCLUSIVE-18 manually, finalize it with saved versioned lines/snapshots. Upload-only invoices are not eligible originals. Select actual assigned IDs/numbers.', 'base_total':'1180.00','steps':[
        {'step':'Credit value-only adjustment','document_date':'2026-11-05','reason':'Synthetic local value correction','pricing_mode':'exclusive','mode':'value','amount':'200.00','kind':'credit','tax_effect':'gst','expected':{'taxable_amount':'200.00','cgst_amount':'18.00','sgst_amount':'18.00','igst_amount':'0.00','total_amount':'236.00'},'net_original_amount':'944.00'},
        {'step':'Debit value-only adjustment','document_date':'2026-11-10','reason':'Synthetic local extra value','pricing_mode':'exclusive','mode':'value','amount':'100.00','kind':'debit','tax_effect':'gst','expected':{'taxable_amount':'100.00','cgst_amount':'9.00','sgst_amount':'9.00','igst_amount':'0.00','total_amount':'118.00'},'net_original_amount':'1062.00'},
        {'step':'Cancel credit with dated reversal','cancellation_date':'2026-11-15','net_original_amount':'1298.00','expected':'Original invoice values/payments unchanged; dated reversal event, not a fake refund'},
    ],'received_note_variant':'Repeat against manually verified PURCHASE-18; supplier reference required; system assigns received-note number.','negative':'A credit above remaining original value must be rejected. Eligibility attestation is a review decision, not statutory certification.'})
    js('expenses/recurrence_itc_and_attribution.json',{'inclusive_schedule':{'title':'Synthetic month-end subscription','supplier_key':'vendor','category':'Synthetic subscription','amount':'1180.03','gst_rate':'18','gst_inclusive':True,'supplier_state_code':'27','place_of_supply':'27','supply_category':'standard','tax_treatment':'taxable','frequency':'monthly','start_date':'2026-10-31','expected_dates':['2026-10-31','2026-11-30','2026-12-31'],'expected_each_occurrence':by_id['INCLUSIVE-ROUNDING']['expected'],'checks':['Explicit catch-up for historical dates','Generate twice: no duplicate periods','Edit only notes: saved taxable/GST/grand total unchanged']},'attribution':{'source_case':'PURCHASE-18','steps':['Verify purchase','Attribute existing purchase to expense category','Compare financial report totals'],'expected':'One financial invoice; never sum purchase and attributed-register values as two costs'},'itc':{'source_case':'PURCHASE-18','supporting_document':'documents/PURCHASE-18.pdf','steps':['Pending review','Owner-reviewed potential/confirmed or ineligible with reason','Change source and verify review becomes stale','Reverse previously confirmed review where supported'],'expected':'Review evidence and saved GST remain distinct from government-confirmed ITC. Checkbox attestation requires actual independent review; no auto-claim.'}})
    js('payments/reporting_regressions.json',{'historical_paid':{'source_case':'HISTORICAL-PAYMENT','payment_date':'2026-11-10','amount':'1180.00','reference_id':'FEATURE-LATE-PAYMENT-001','expected_october_31_outstanding':'1180.00','expected_november_30_outstanding':'0.00','known_current_bug':'Current paid-status shortcut can incorrectly show October as zero.'},'both_party':{'sources':['BOTH-SALE','BOTH-PURCHASE'],'sale_debit':'100.00','purchase_credit':'80.00','net_debit_minus_credit':'20.00','separate_receivable':'100.00','separate_payable':'80.00','known_current_display':'Running balance may display 180.00; do not treat it as net settlement.'},'partial':{'source_case':'EXCLUSIVE-18','payments':[{'payment_date':'2026-11-05','amount':'500.00','reference_id':'FEATURE-PARTIAL-001'},{'payment_date':'2026-11-10','amount':'680.00','reference_id':'FEATURE-PARTIAL-002'}],'after_first_outstanding':'680.00','after_second_outstanding':'0.00','duplicate_reference':'Repeating a reference for the same invoice must not create another payment.'},'date_controls':{'selected_preset':'this_month','entered_start':'2026-10-01','entered_end':'2026-10-31','expected':'Choose Custom explicitly until the identified date-control issue is fixed. Current presets may ignore entered dates.'}})
    rows=[{'supplier_gstin':P['vendor']['gstin'],'invoice_number':by_id['PURCHASE-18']['reference_number'],'invoice_date':'2026-11-01','taxable_value':'1000.00','cgst_amount':'90.00','sgst_amount':'90.00','igst_amount':'0.00','total_tax':'180.00'}]
    js('tax/reconciliation_exact.json',rows,'Uploadable reconciliation JSON; period 2026-11; use only for the matching PURCHASE-18 uploaded supplier number.')
    workbook=Workbook();sheet=workbook.active;sheet.title='Reconciliation';sheet.append(list(rows[0]));sheet.append(list(rows[0].values()));out=BytesIO();workbook.save(out)
    put('tax/reconciliation_exact.xlsx',out.getvalue(),'Same rows as JSON: choose one format per reconciliation run, not both.')
    js('tax/preparation_inputs.json',{'gst_period':{'kind':'monthly','start_date':'2026-11-01','end_date':'2026-11-30','cases':['EXCLUSIVE-18','PURCHASE-18'],'aato_band':'up_to_5cr','recipient_registration_by_case':{'EXCLUSIVE-18':{'status':'registered','reason':'Fictional local test evidence only; not an official lookup'}},'expected_book_output_components':{'cgst':'90.00','sgst':'90.00'},'expected_book_input_components':{'cgst':'90.00','sgst':'90.00'},'workflow':['Confirm standard domestic scope independently','Review recipient registration evidence','Preview included/excluded sources','Prepare reviewed snapshot','Review / revise without changing old snapshot'],'limitations':'Book components only, not statutory liability/utilization. Select actual invoice and import-run IDs; no prefilled attestation.'},'income_estimate':{'period':{'kind':'financial_year','start_date':'2026-04-01','end_date':'2027-03-31'},'business_type':'proprietorship','taxpayer':'resident_individual','regime':'new','assessment_year':'not_applicable','taxable_income':'1600000.00','expected_current_application_tax':'124800.00','expected_status':'estimated','expected_scope':'Current application rule regression only, not independently verified statutory tax','deductions':'0.00','resident_confirmed':'Independent review required','ordinary_only':'Independent review required','expected':'Compare with current application rule bundle; not accounting profit or filing advice. Unsupported old regime/company/high income must require manual review.'}})
    js('security/local_manual_scenarios.json',[
        {'id':'TENANT-ISOLATION','setup':'Two separate local demo accounts/businesses; no shared financial records.','action':'Try a known other-demo invoice/source/PDF/audit/export URL while signed in as the first demo user.','expected':'404/403 without resource-existence disclosure; no cross-business content'},
        {'id':'FAILED-LOGIN','action':'Five wrong-password attempts for the same explicitly created synthetic identity on the isolated local app.','expected':'One AUTH_FAILURE_BURST alert per scope/bucket, not an invented account lock. Existing per-IP request throttle may apply.'},
        {'id':'ALERT-REVIEW','action':'Use authorized security staff to acknowledge/investigate/resolve with reasons.','expected':'History retained; ordinary owner cannot dismiss; repeated same status creates no duplicate decision'},
        {'id':'SESSION-REVOKE','action':'Sign in to the synthetic account in two local browser profiles; revoke other observed sessions after password confirmation.','expected':'Other observed session rejected on next request; own current session stays usable. Never put raw tokens in sample files.'},
        {'id':'EXPORT-REDaction','action':'Use harmless names from negative/validation_and_rendering.json, then export CSV/XLSX/PDF.','expected':'Escaped HTML, formula-safe spreadsheet text, no secrets; verify filtered IDs'},
        {'id':'INTEGRITY','action':'Run read-only verify_audit_security; any tamper experiment must use a disposable copy only.','expected':'Legacy unsealed coverage is separate. Never change the main audit log or protected backups.'},
    ])
    pagination=[{'case_id':f'PAGE-{i:03}','invoice_type':'sales','date':'2026-11-01','customer_key':'customer','description':f'Synthetic pagination item {i:03}','quantity':'1','rate':'100.00','tax_rate':'18','expected_total':'118.00'} for i in range(1,61)]
    put('ui/pagination_60_manual_references.csv',csv_bytes(list(pagination[0]),pagination),'Manual-entry references only, not a CSV importer. Expected pages 25/25/10 for a clean isolated dataset of these 60 invoices.')
    js('ui/checklist.json',{'widths':[375,430,768,1280,1440],'checks':['Keyboard sidebar and current page','Long party names and row scrolling','Search/status/type filters with pagination','One feedback message','Repeated submit blocked without losing action values','Custom reporting period','Draft/cancelled PDF watermark','60-line PDF pagination','Saved financial components match CSV/JSON','No browser acceptance claim until actually tested'],'browser':'Existing Safari only if remote automation is enabled by the user; do not change preferences automatically.'})
    matrix=[
        {'feature':'GST/manual entry','data':'manual_entry/invoice_cases.json','expected':'expected/invoice_values.csv','known_bug':'no'},
        {'feature':'PDF/upload/extraction','data':'documents/; ocr/','expected':'ocr/ground_truth.json; explicit review','known_bug':'OCR accuracy varies'},
        {'feature':'Quotes/proformas','data':'commercial/quotations_proformas.json','expected':'No accounting until supported conversion','known_bug':'no'},
        {'feature':'Credit/debit notes','data':'adjustments/credit_debit_workflows.json','expected':'944 -> 1062 -> 1298 net after specified steps','known_bug':'no'},
        {'feature':'Expenses/recurring/ITC','data':'expenses/recurrence_itc_and_attribution.json','expected':'1180.03 inclusive, no duplicate periods/attribution costs','known_bug':'no'},
        {'feature':'Historical reporting','data':'payments/reporting_regressions.json','expected':'October outstanding 1180, November 0','known_bug':'paid status shortcut'},
        {'feature':'Mixed-role party ledger','data':'payments/reporting_regressions.json','expected':'100 receivable / 80 payable; net debit-credit 20','known_bug':'combined running balance'},
        {'feature':'Reconciliation/tax preparation','data':'tax/','expected':'Book input/output components 90 CGST + 90 SGST each for exact two-case setup','known_bug':'No statutory verification claimed'},
        {'feature':'Security/session/tenant access','data':'security/local_manual_scenarios.json','expected':'Scoped denial/review/revocation; no raw tokens','known_bug':'date underflow negative case'},
        {'feature':'UI/pagination/accessibility','data':'ui/','expected':'25/25/10 for 60 references; five viewport widths','known_bug':'preset dates may be ignored'},
    ]
    put('feature_matrix.csv',csv_bytes(['feature','data','expected','known_bug'],matrix),'Feature-to-fixture index; reference only.')
    guide=GUIDE
    put('README.md',guide,'Start here: setup order, per-feature instructions, known bugs and safe-use boundaries.')
    manifest={'version':1,'fictional':True,'no_database_import':True,'currency':'INR','numbers':'Sales/quotation/proforma/note numbers and database IDs are assigned by the app. reference_number identifies upload documents only.','files':files}
    with (output/'manifest.json').open('x') as target:json.dump(manifest,target,indent=2)
    return manifest


GUIDE = '''# Feature acceptance samples — fictional and isolated only

This extends the older sample library without changing its files or expected totals.
Use a NEW isolated demo database/media directory and demo business. Nothing here is
an instruction or program to import records into the main development database.
Never send real emails, make payments, file returns or contact any sample identity.
Emails use reserved example.test addresses. GSTINs have valid local checksums for
positive tests but are fabricated, not confirmed registrations or proof of eligibility.

## How to use
1. Read manual_entry/parties.csv and create the fictional business/parties in your
   isolated app. The business row is a profile, not a BusinessParty; use business
   type proprietorship for the income-estimate example. Use a separate second demo account for authorization tests. No
   passwords, account IDs, live sender settings or security tokens are supplied.
2. Pick ONE scenario at a time. manual_entry/invoice_cases.json has actual form values,
   calculation options and expected saved values. Sales numbers are AUTO-ASSIGNED:
   record the number/ID the app actually returns. These JSON/CSV files are references,
   NOT supported bulk import formats or Django fixtures.
3. documents/*.pdf and ocr/*.{pdf,png,jpg} are uploadable alternatives. Choose the
   matching invoice type, extract, review every field/line, confirm, save, and verify
   separately. Do not count PDF/PNG/JPEG alternatives as different expenses. Images
   and scans require existing OCR; no OCR accuracy guarantee or auto-correction.
4. For credit/debit notes, create/finalize the original MANUALLY using the JSON values
   so saved Phase 4 lines/snapshots exist; uploaded invoices are not eligible originals.
   Select the actual original invoice/line IDs. Follow adjustments/*.json. Commercial
   estimates are separate from accounting invoices until the supported conversion.
5. Enter payment references from payments/reporting_regressions.json once through the
   payment form, only after the matching invoice exists. They are TEST transactions
   in the disposable demo database, never real payments. Read known-bug expectations
   before interpreting the historical or mixed-party running balance.
6. Recurring expenses use expenses/*.json: explicit historical catch-up, month-end
   dates, no duplicate periods and inclusive 1180.03 notes-only editing. Purchase
   attribution must not double-count one purchase as another expense. ITC confirmation
   requires independent evidence and review; do not blindly tick attestations.
7. tax/reconciliation_exact.json and .xlsx are the ONLY uploadable structured-data
   references here: same rows, choose ONE, return period 2026-11. They match the upload
   supplier reference FEATURE-PURCHASE-18. A manually created supplier purchase may
   use that supplier number; sales/note numbers are never hardcoded. Use actual app
   record/run IDs in tax working-paper forms; snapshots need explicit review.
8. ui/pagination_60_manual_references.csv is a reference list, NOT an importer. Create
   only in a disposable local dataset to check 25/25/10 pages. User/security lifecycle
   scenarios are manual instructions, not fabricated audit history or production load
   tests. Existing automated Django tests remain the faster option for large batches.

## Expected data and negative cases
Expected invoice components and treatment classifications are in expected/invoice_values.csv.
Each case is independent. Never add this pack to the old library's published golden
KPI totals without explicitly defining a new scenario. Taxable Amount here is the
saved value before GST; classified taxable/exempt/nil/non-GST values are separate.
Discount and inclusive pricing must be reviewed explicitly after OCR. Round-off is
not an unclassified charge. The MULTIPAGE PDF has 60 lines; line extraction can be
incomplete and requires review. Do not silently accept inaccurate OCR output.

negative/ contains deliberately missing, ambiguous, duplicate and corrupt documents,
plus harmless validation/rendering inputs. These must be rejected or left for review;
they are not valid invoices. No malicious executable, external URL or exploit payload
is included. The mixed text/scan PDF intentionally combines conflicting references
and must not be merged/accepted automatically.

Known current review findings are deliberately preserved as repro data: an October
invoice paid in November must remain outstanding at October end; the current paid
shortcut can report zero. A party with a 100 sale and 80 purchase has separate 100
receivable/80 payable or a 20 signed net, not a net settlement of 180. Very early date
filters may currently crash. Preset dashboard periods may ignore manually edited
dates unless Custom is selected. Generating data does not fix these bugs.

## Coverage
Manual sales/purchases; inclusive/exclusive and inter-state GST; rates/treatments;
discounts/round-off; multipage PDF, draft/cancelled watermarks via app lifecycle;
OCR text/scans/PNG/JPEG/rotation/low quality/missing/conflicts/corruption/duplicates;
expenses/receipts/recurrence/purchase attribution/ITC; quotations/proformas/conversion;
issued/received credit/debit notes and reversal; payments/overpayment/duplicate refs;
reconciliation and tax working-paper/income-estimate inputs; tenant isolation,
alerts/sessions/read-only audit integrity; UI filters/pagination/responsive checklists.
Live email, physical printing, official GST/ITC verification and statutory filing are
NOT tested or authorized. Email delivery should use locmem in isolated testing; any
live recipient/sender/send count needs separate explicit approval.

## Reproduce/verify
From the repository root:
  env/bin/python sample_data/generate_feature_library.py --verify
  env/bin/python sample_data/generate_feature_library.py --output /private/tmp/new-sample-pack
Generation refuses ANY existing destination, writes files exclusively and never
imports Django models or modifies databases. --verify only checks manifest hashes.
'''


def verify(output=ROOT):
    output=Path(output);manifest=json.loads((output/'manifest.json').read_text())
    for name,info in manifest['files'].items():
        path=output/name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=info['sha256']:raise ValueError('File differs: '+name)
    return len(manifest['files'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT);parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    if args.verify:print('Verified',verify(args.output),'sample files; no database access.')
    else:print('Created',len(generate(args.output)['files']),'sample files; no database access.')
