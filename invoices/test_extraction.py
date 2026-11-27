from datetime import date,timedelta
from decimal import Decimal
from io import BytesIO
import json,os,tempfile,uuid,hashlib,time
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import signing
from django.test import TestCase,SimpleTestCase,Client,override_settings
from django.urls import reverse
from django.utils import timezone
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.pagesizes import A4
from PIL import Image
from pytesseract import TesseractNotFoundError
from accounts.models import BusinessProfile,BusinessParty
from expenses.models import ExpenseDetail
from audit.models import AuditEvent
from .models import Invoice,InvoicePayment,InvoiceExtractionAttempt as Attempt,InvoiceExtractionReview as Review
from .extraction import parse_document,date_candidates,extract_invoice_data,_extract_local,_document_text,_ocr_image,OCRUnavailableError
from .gstin import gstin_evidence
from .file_security import validate_document
from .document_worker import run_worker
from .extraction_services import verify_review,invoice_snapshot,duplicate_signals
from .manual_calculations import calculate_invoice
from analytics.services import build_analytics_context

D=Decimal
TEXT='''TAX INVOICE
Supplier: Synthetic Supplier
Supplier Address: Test address Mumbai
Supplier GSTIN: 27AAACS9002A1ZW
Buyer: Synthetic Buyer
Buyer GSTIN: 27AAACS9000A1ZY
Invoice Number: INV-0009/26-A
Invoice Date: 20/11/2026
Due Date: 10 Dec 2026
PO Number: PO-009
Supplier State Code: 27
Place of Supply: 27
Pricing: exclusive
Supply Category: standard
Subtotal: INR 1000.00
Taxable Value: INR 1000.00
CGST 9%: INR 90.00
SGST 9%: INR 90.00
IGST: INR 0.00
Other Charges: INR 0.00
Rounding Adjustment: INR 0.00
Discount: INR 0.00
Grand Total: INR 1180.00
1 Cloud consulting 9983 1 1000.00 1000.00 18%
'''


def text_pdf(text=TEXT,pages=1):
 out=BytesIO();canvas=Canvas(out,pagesize=A4)
 for n in range(pages):
  block=canvas.beginText(35,800);block.setFont('Helvetica',9)
  for line in text.splitlines():block.textLine(line)
  canvas.drawText(block);canvas.showPage()
 canvas.save();return out.getvalue()


def parsed(text=TEXT,method='embedded'):
 return parse_document([{'page':1,'text':text,'method':method,'tables':[]}],'digital_pdf')


def image_bytes(fmt='PNG',rotation=0):
 image=Image.new('RGB',(500,400),'white')
 if rotation:image=image.rotate(rotation,expand=True)
 out=BytesIO();image.save(out,fmt);return out.getvalue()


class ExtractionRuleTests(SimpleTestCase):
 def test_labelled_identity_dates_and_references(self):
  data=parsed();self.assertEqual(data['invoice_number'],'INV-0009/26-A');self.assertEqual(data['invoice_date'],'2026-11-20');self.assertEqual(data['due_date'],'2026-12-10');self.assertEqual(data['purchase_order'],'PO-009');self.assertEqual(data['supplier_name'],'Synthetic Supplier')
 def test_gstin_structure_checksum_and_registration_distinction(self):
  for value in ['27AAACS9000A1ZY','27AAACS9002A1ZW']:
   e=gstin_evidence(value.lower());self.assertTrue(e['structural_valid']);self.assertTrue(e['checksum_valid']);self.assertEqual(e['registration'],'not_verified')
  for value in ['99AAACS9000A1ZY','27AAACS9000A1Z0','27AAB','27AAACS9000A0ZY']:self.assertFalse(gstin_evidence(value)['checksum_valid'])
 def test_invalid_gstin_not_repaired_or_given_high_confidence(self):
  data=parsed(TEXT.replace('27AAACS9002A1ZW','27AAACS9002A1ZO'));self.assertEqual(data['supplier_gstin'],'27AAACS9002A1ZO');self.assertEqual(data['confidence']['supplier_gstin']['level'],'low')
 def test_unlabelled_gstins_do_not_assign_by_order(self):
  data=parsed('GSTIN: 27AAACS9000A1ZY\nGSTIN: 27AAACS9002A1ZW');self.assertIsNone(data['supplier_gstin']);self.assertIsNone(data['buyer_gstin']);self.assertEqual(len(data['unassigned_gstins']),2)
 def test_ambiguous_and_invalid_dates_stay_unknown(self):
  data=parsed(TEXT.replace('20/11/2026','03/04/2026'));self.assertIsNone(data['invoice_date']);self.assertEqual(data['confidence']['invoice_date']['level'],'conflicting');self.assertEqual(set(date_candidates('03/04/2026')),{'2026-04-03','2026-03-04'})
  self.assertEqual(date_candidates('31/02/2026'),[])
 def test_supported_date_formats(self):
  for raw,expected in [('20/11/2026','2026-11-20'),('20-11-2026','2026-11-20'),('2026-11-20','2026-11-20'),('20 Nov 2026','2026-11-20')]:self.assertEqual(date_candidates(raw),[expected])
 def test_conflicting_headers_and_missing_values_not_zero(self):
  data=parsed(TEXT+'Grand Total: INR 1200.00\n');self.assertIsNone(data['total_amount']);self.assertEqual(data['confidence']['total_amount']['level'],'conflicting')
  empty=parsed('No financial fields');self.assertIsNone(empty['taxable_amount']);self.assertEqual(empty['confidence']['taxable_amount']['level'],'missing')
 def test_raw_mismatch_preserved_not_reconciled(self):
  data=parsed(TEXT.replace('1180.00','1200.00'));self.assertEqual(data['total_amount'],D('1200'));self.assertEqual(data['confidence']['total_amount']['level'],'conflicting')
 def test_ocr_confidence_is_rule_based_not_probability(self):
  data=parsed(method='ocr');self.assertEqual(data['confidence']['invoice_number']['level'],'medium');self.assertNotIn('probability',data['confidence']['invoice_number']);self.assertNotIn('percentage',data['confidence']['invoice_number'])
 def test_line_candidates_do_not_invent_tax_totals_or_discounts(self):
  row=parsed()['line_items'][0];self.assertEqual(row['quantity'],'1');self.assertIsNone(row['tax_amount']);self.assertIsNone(row['total_amount']);self.assertIsNone(row['discount']);self.assertEqual(row['tax_treatment'],'taxable')
 def test_structured_table_and_zero_rate_classification_unknown(self):
  table=[['Description','HSN','Qty','Unit','Rate','Discount','GST %','Taxable Value','Total'],['Support','9983','1','Hr','1000','10%','0','900','900']]
  row=parse_document([{'page':1,'text':TEXT,'method':'embedded','tables':[table]}],'digital_pdf')['line_items'][0]
  self.assertEqual(row['discount_type'],'percent');self.assertIsNone(row['tax_treatment']);self.assertEqual(row['total_amount'],'900')
 def test_po_irn_eway_not_invoice_numbers(self):
  data=parsed('PO Number: PO-1\nIRN: '+'A'*64+'\nE-way Bill: 123456789012');self.assertIsNone(data['invoice_number'])
 def test_digital_pdf_worker_extracts_pages_and_positions(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'source.pdf';p.write_bytes(text_pdf(pages=2));data=extract_invoice_data(p)
  self.assertEqual(data['document_type'],'digital_pdf');self.assertEqual(len(data['pages']),2);self.assertTrue(data['pages'][0]['positions']);self.assertIn('\f',data['extracted_text']);self.assertEqual(data['invoice_date'],'2026-11-20')
 def test_png_jpeg_missing_engine_retains_failed_metadata(self):
  for ext,fmt in [('png','PNG'),('jpg','JPEG')]:
   with tempfile.TemporaryDirectory() as folder:
    p=Path(folder)/('source.'+ext);p.write_bytes(image_bytes(fmt))
    with patch('invoices.extraction.pytesseract.image_to_string',side_effect=TesseractNotFoundError()):data=_extract_local(p)
   self.assertEqual(data['pages'][0]['error'],'ocr_unavailable');self.assertIn('unavailable',' '.join(data['validation_errors']))
 def test_mixed_pdf_ocr_per_page_no_duplicate_text(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'mixed.pdf';out=BytesIO();canvas=Canvas(out,pagesize=A4);canvas.drawString(40,780,'Invoice Number TEXT-PAGE-1');canvas.showPage();canvas.showPage();canvas.save();p.write_bytes(out.getvalue())
   with patch('invoices.extraction._ocr_image',return_value='Invoice Number SCAN-PAGE-2') as ocr:data=_extract_local(p)
  self.assertEqual(ocr.call_count,1);self.assertEqual(data['document_type'],'mixed_pdf');self.assertEqual(data['extracted_text'].count('TEXT-PAGE-1'),1)
 def test_scanned_pdf_fallback_and_ocr_page_budget(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'scan.pdf';Image.new('RGB',(500,400),'white').save(p,'PDF')
   with patch('invoices.extraction._ocr_image',return_value=TEXT):data=_extract_local(p)
  self.assertEqual(data['document_type'],'scanned_pdf');self.assertEqual(data['pages'][0]['method'],'ocr')
 def test_orientation_and_original_bytes_preserved(self):
  image=Image.new('RGB',(200,400));seen=[]
  with patch('invoices.extraction.shutil.which',return_value='/test/tesseract'),patch('invoices.extraction.pytesseract.image_to_osd',return_value='Rotate: 90'),patch('invoices.extraction.pytesseract.image_to_string',side_effect=lambda img,**kw:seen.append(img.size) or TEXT):_ocr_image(image)
  self.assertGreater(seen[0][0],seen[0][1]);self.assertEqual(image.size,(200,400))
 def test_low_resolution_and_ocr_timeout(self):
  with patch('invoices.extraction.pytesseract.image_to_string',return_value=TEXT) as ocr:_ocr_image(Image.new('RGB',(100,100)))
  self.assertEqual(ocr.call_args.kwargs['timeout'],30)
  with patch('invoices.extraction.pytesseract.image_to_string',side_effect=RuntimeError('timeout')):
   with self.assertRaises(RuntimeError):_ocr_image(Image.new('RGB',(100,100)))
 def test_worker_timeout_kills_group_and_temp_cleanup(self):
  import subprocess
  fake=__import__('unittest').mock.Mock();fake.pid=123;fake.communicate.side_effect=[subprocess.TimeoutExpired('worker',1),(b'',b'')]
  with patch('invoices.document_worker.subprocess.Popen',return_value=fake) as popen,patch('invoices.document_worker.os.killpg') as kill:
   with self.assertRaises(ValidationError):run_worker(SimpleUploadedFile('test.pdf',text_pdf()),'extract')
   kill.assert_called_once();source=Path(popen.call_args.args[0][4]);self.assertFalse(source.exists());self.assertFalse(source.parent.exists())
 def test_corrupt_extension_size_pixel_and_page_limits(self):
  for name,data in [('x.pdf',b'%PDF broken'),('x.png',text_pdf()),('x.exe',b'executable')]:
   with self.assertRaises(ValidationError):validate_document(SimpleUploadedFile(name,data))
  with override_settings(DOCUMENT_MAX_PAGES=1),self.assertRaises(ValidationError):validate_document(SimpleUploadedFile('x.pdf',text_pdf(pages=2)))
  with override_settings(DOCUMENT_MAX_PIXELS=100),self.assertRaises(ValidationError):validate_document(SimpleUploadedFile('x.png',image_bytes()))
 def test_encrypted_and_active_pdf_rejected(self):
  from reportlab.lib.pdfencrypt import StandardEncryption
  out=BytesIO();c=Canvas(out,encrypt=StandardEncryption('secret'));c.drawString(10,20,'Private');c.save()
  with self.assertRaises(ValidationError):validate_document(SimpleUploadedFile('locked.pdf',out.getvalue()))
  out=BytesIO();c=Canvas(out);c.addJS('app.alert("unsafe")') if hasattr(c,'addJS') else None
  # PDF catalog active action constructed without executing it.
  from reportlab.pdfbase.pdfdoc import PDFDictionary,PDFString,PDFName
  c._doc.Catalog.OpenAction=PDFDictionary({'S':PDFName('JavaScript'),'JS':PDFString('app.alert("unsafe")')});c.drawString(10,20,'Active document');c.save()
  with self.assertRaises(ValidationError):validate_document(SimpleUploadedFile('active.pdf',out.getvalue()))


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class ExtractionWorkflowTests(TestCase):
 def setUp(self):
  self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup);override=override_settings(MEDIA_ROOT=self.directory.name);override.enable();self.addCleanup(override.disable)
  self.user=get_user_model().objects.create_user('ocr-owner',email='ocr@example.test');self.other=get_user_model().objects.create_user('ocr-other',email='other@example.test')
  self.business=BusinessProfile.objects.create(user=self.user,business_name='Synthetic Buyer',gstin='27AAACS9000A1ZY');BusinessProfile.objects.create(user=self.other,business_name='Other synthetic')
  self.vendor=BusinessParty.objects.create(business=self.business,party_type='vendor',name='Synthetic Supplier',gstin='27AAACS9002A1ZW')
  self.client.force_login(self.user)
 def upload(self,data=None,kind='purchase',body=None):
  with patch('invoices.views.extract_invoice_data',return_value=data or parsed()):
   response=self.client.post(reverse('invoice_upload'),{'invoice_type':kind,'source_file':SimpleUploadedFile('invoice.pdf',body or text_pdf(),content_type='application/pdf')})
  return response,Attempt.objects.filter(user=self.user).first()
 def payload(self,attempt,**updates):
  page=self.client.get(reverse('extraction_review',args=[attempt.pk]));self.assertEqual(page.status_code,200)
  data={'token':page.context['form'].initial['token'],'invoice_number':'INV-0009/26-A','invoice_date':'2026-11-20','due_date':'2026-12-10','supplier_name':'Synthetic Supplier','supplier_gstin':'27AAACS9002A1ZW','supplier_address':'Test address Mumbai','buyer_name':'Synthetic Buyer','buyer_gstin':'27AAACS9000A1ZY','buyer_address':'Test buyer','party':self.vendor.pk,'supplier_state_code':'27','place_of_supply':'27','pricing_mode':'exclusive','supply_category':'standard','invoice_discount_type':'fixed','invoice_discount_value':'0','taxable_amount':'1000','cgst_amount':'90','sgst_amount':'90','igst_amount':'0','other_charges':'0','rounding_adjustment':'0','total_amount':'1180','confirm':'on','acknowledge_duplicates':'on',
  'items-TOTAL_FORMS':'1','items-INITIAL_FORMS':'1','items-MIN_NUM_FORMS':'1','items-MAX_NUM_FORMS':'100','items-0-description':'Cloud consulting','items-0-hsn_sac_code':'9983','items-0-quantity':'1','items-0-unit':'Hr','items-0-rate':'1000','items-0-discount':'0','items-0-discount_type':'fixed','items-0-tax_rate':'18','items-0-tax_treatment':'taxable'};data.update(updates);return data
 def review(self,attempt,**updates):
  return self.client.post(reverse('extraction_review',args=[attempt.pk]),self.payload(attempt,**updates))
 def test_original_hash_candidates_review_status_and_no_auto_party_or_payments(self):
  response,a=self.upload();self.assertEqual(response.status_code,302)
  with a.source_file.open('rb') as source:self.assertEqual(hashlib.sha256(source.read()).hexdigest(),a.file_sha256)
  self.assertEqual(a.invoice.status,'review');self.assertEqual(BusinessParty.objects.count(),1);self.assertFalse(InvoicePayment.objects.exists());self.assertFalse(Review.objects.exists());self.assertTrue(a.result['confidence'])
 def test_confirmation_and_separate_verification(self):
  _,a=self.upload();before=build_analytics_context(self.user,date(2026,1,1),date(2026,12,31),'custom')['totals']
  response=self.review(a);self.assertEqual(response.status_code,302,str(response.context['form'].errors) if response.context else response.content[:200]);a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review');self.assertEqual(a.invoice.calculation_version,1);self.assertEqual(Review.objects.count(),1)
  self.assertEqual(before,build_analytics_context(self.user,date(2026,1,1),date(2026,12,31),'custom')['totals'])
  self.client.post(reverse('invoice_verify',args=[a.invoice_id]));a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review')
  self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'verified');self.assertFalse(InvoicePayment.objects.exists())
  self.assertEqual(build_analytics_context(self.user,date(2026,1,1),date(2026,12,31),'custom')['totals']['purchases'],D('1180'))
 def test_unreviewed_verify_and_legacy_edit_cannot_bypass_review(self):
  _,a=self.upload();self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review')
  self.assertEqual(self.client.post(reverse('invoice_edit',args=[a.invoice_id]),{'total_amount':'1'}).url,reverse('extraction_review',args=[a.pk]))
 def test_confirm_required_and_header_mismatch_not_silently_replaced(self):
  _,a=self.upload();response=self.review(a,confirm='');self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists())
  response=self.review(a,total_amount='1200');self.assertEqual(response.status_code,200);self.assertContains(response,'server-calculated');self.assertFalse(Review.objects.exists())
 def test_invalid_gstin_date_and_tax_classification_rejected(self):
  _,a=self.upload()
  for changes in [{'supplier_gstin':'27AAACS9002A1ZO'},{'invoice_date':'03/04/2026'},{'items-0-tax_treatment':''},{'cgst_amount':'180','sgst_amount':'0'},{'other_charges':'20'}]:
   response=self.review(a,**changes);self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists())
 def test_preview_no_write_and_posted_totals_not_trusted(self):
  _,a=self.upload();response=self.review(a,preview='1',total_amount='999');self.assertEqual(response.status_code,200);self.assertEqual(response.context['calculated']['total_amount'],D('1180'));self.assertFalse(Review.objects.exists());a.invoice.refresh_from_db();self.assertEqual(a.invoice.total_amount,D('1180'))
 def test_review_key_replay_and_stale_review_rejected(self):
  _,a=self.upload();data=self.payload(a);self.assertEqual(self.client.post(reverse('extraction_review',args=[a.pk]),data).status_code,302);self.assertEqual(self.client.post(reverse('extraction_review',args=[a.pk]),data).status_code,302);self.assertEqual(Review.objects.count(),1)
  # A different old signed review must not overwrite the newly confirmed snapshot.
  from .extraction_forms import REVIEW_SALT
  data['token']=signing.dumps({'user':self.user.pk,'attempt':a.pk,'previous':None,'key':str(uuid.uuid4())},salt=REVIEW_SALT)
  self.assertEqual(self.client.post(reverse('extraction_review',args=[a.pk]),data).status_code,200);self.assertEqual(Review.objects.count(),1)
 def test_completed_evidence_and_reviews_are_immutable(self):
  _,a=self.upload();raw=json.dumps(a.result,sort_keys=True);self.review(a);a.refresh_from_db();self.assertEqual(json.dumps(a.result,sort_keys=True),raw)
  a.result={'tampered':True}
  with self.assertRaises(ValidationError):a.save()
  review=Review.objects.get();review.snapshot={}
  with self.assertRaises(ValidationError):review.save()
 def test_tampered_saved_invoice_blocked_at_verification(self):
  _,a=self.upload();self.review(a);Invoice.objects.filter(pk=a.invoice_id).update(total_amount=D('1'))
  self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review')
 def test_failure_preserves_original_for_manual_review(self):
  with patch('invoices.views.extract_invoice_data',side_effect=OCRUnavailableError('no engine')):
   response=self.client.post(reverse('invoice_upload'),{'invoice_type':'purchase','source_file':SimpleUploadedFile('scan.pdf',text_pdf())})
  a=Attempt.objects.get();self.assertEqual(a.status,'failed');self.assertTrue(a.source_file.storage.exists(a.source_file.name));self.assertEqual(a.invoice.status,'review');self.assertEqual(response.status_code,302)
  self.assertEqual(self.review(a).status_code,302)
 def test_exact_file_duplicate_signal_but_no_merge(self):
  body=text_pdf();_,first=self.upload(body=body)
  data=parsed(TEXT.replace('INV-0009/26-A','INV-DIFFERENT'))
  _,second=self.upload(data,body=body);self.assertEqual(Invoice.objects.count(),2);self.assertIn('exact_file',[s['kind'] for s in second.duplicate_signals]);self.assertNotEqual(first.pk,second.pk)
 def test_document_duplicate_retained_without_overwriting_invoice(self):
  body=text_pdf();_,first=self.upload(body=body);snapshot=invoice_snapshot(first.invoice)
  response,a=self.upload(body=body);self.assertEqual(response.status_code,200);self.assertEqual(a.status,'blocked');self.assertIsNone(a.invoice_id);self.assertTrue(a.source_file.storage.exists(a.source_file.name));self.assertEqual(Invoice.objects.count(),1);first.invoice.refresh_from_db();self.assertEqual(invoice_snapshot(first.invoice),snapshot)
 def test_duplicate_acknowledgement_and_false_positive_prevention(self):
  _,a=self.upload();data=parsed(TEXT.replace('INV-0009/26-A','INV-OTHER'));_,b=self.upload(data)
  response=self.review(b,invoice_number='INV-OTHER',acknowledge_duplicates='');self.assertEqual(response.status_code,302) # different document bytes, no amount-only warning
  signals=duplicate_signals(self.user,self.business,'purchase',{'total_amount':'1180.00'},'not-a-hash');self.assertEqual(signals,[])
 def test_cross_business_duplicate_information_never_leaks(self):
  _,a=self.upload();other_business=self.other.business_profile
  self.assertEqual(duplicate_signals(self.other,other_business,'purchase',parsed(),a.file_sha256),[])
 def test_private_source_owner_and_csrf(self):
  _,a=self.upload();response=self.client.get(reverse('extraction_source',args=[a.pk]));self.assertEqual(response.status_code,200);self.assertIn('no-store',response['Cache-Control']);self.assertEqual(response['X-Content-Type-Options'],'nosniff');response.close()
  self.client.force_login(self.other)
  for endpoint in ['extraction_review','extraction_source']:self.assertEqual(self.client.get(reverse(endpoint,args=[a.pk])).status_code,404)
  self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,404)
  csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.user);self.assertEqual(csrf.post(reverse('extraction_review',args=[a.pk]),{}).status_code,403)
  self.assertEqual(Client().get(reverse('extraction_history')).status_code,302)
 def test_review_token_scoped_and_party_owner_checked(self):
  _,a=self.upload();data=self.payload(a);data['token']=data['token'][:-1]+'!';self.assertEqual(self.client.post(reverse('extraction_review',args=[a.pk]),data).status_code,200)
  foreign=BusinessParty.objects.create(business=self.other.business_profile,name='Foreign',party_type='vendor')
  self.assertEqual(self.review(a,party=foreign.pk).status_code,200);self.assertFalse(Review.objects.exists())
 def test_retry_appends_history_requires_confirm_and_preserves_file(self):
  _,a=self.upload();self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{}).status_code,400)
  with patch('invoices.extraction.extract_invoice_data',return_value=parsed()):response=self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'})
  self.assertEqual(response.status_code,302);self.assertEqual(Attempt.objects.count(),2);a.refresh_from_db();latest=Attempt.objects.first();self.assertNotEqual(latest.pk,a.pk);self.assertEqual(latest.source_file.name,a.source_file.name);self.assertEqual(a.status,'review')
  self.assertEqual(self.client.get(reverse('extraction_review',args=[a.pk])).status_code,400)
 def test_retry_verified_invoice_blocked_and_history_not_rewritten(self):
  _,a=self.upload();self.review(a);self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'})
  self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,400);self.assertEqual(Attempt.objects.count(),1)
 def test_expense_and_purchase_do_not_auto_create_expense_records(self):
  _,a=self.upload(kind='expense');self.assertEqual(a.invoice.invoice_type,'expense');self.assertFalse(ExpenseDetail.objects.exists());self.assertEqual(self.review(a).status_code,302);self.assertFalse(ExpenseDetail.objects.exists());self.assertEqual(a.invoice.status,'review')
 def test_noninvoice_document_cannot_be_confirmed_as_tax_invoice(self):
  data=parsed(TEXT.replace('TAX INVOICE','CREDIT NOTE'));_,a=self.upload(data);self.assertEqual(self.review(a).status_code,200);self.assertFalse(Review.objects.exists())
 def test_no_raw_text_in_audit(self):
  _,a=self.upload();self.review(a)
  for event in AuditEvent.objects.filter(user=self.user):self.assertNotIn('Cloud consulting',json.dumps(event.metadata));self.assertNotIn('Supplier Address',json.dumps(event.after))
 def test_missing_schema_is_safe(self):
  with patch('invoices.extraction_services.history_ready',return_value=False):response=self.client.post(reverse('invoice_upload'),{'invoice_type':'purchase','source_file':SimpleUploadedFile('test.pdf',text_pdf())})
  self.assertEqual(response.status_code,200);self.assertFalse(Attempt.objects.exists())
 def test_inclusive_rounding_review_uses_saved_pricing_mode(self):
  _,a=self.upload();response=self.review(a,pricing_mode='inclusive',taxable_amount='1000.03',total_amount='1180.03',**{'items-0-rate':'1180.03'})
  self.assertEqual(response.status_code,302);a.invoice.refresh_from_db();self.assertEqual(a.invoice.pricing_mode,'inclusive');self.assertEqual(a.invoice.taxable_amount,D('1000.03'))
  self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'verified')
 def test_mixed_rates_and_document_discount_reconcile(self):
  _,a=self.upload();data=self.payload(a,invoice_discount_type='percent',invoice_discount_value='10',taxable_amount='1350',cgst_amount='92.25',sgst_amount='92.25',total_amount='1534.50',**{'items-TOTAL_FORMS':'2','items-1-description':'Hosting','items-1-hsn_sac_code':'9983','items-1-quantity':'1','items-1-unit':'Hr','items-1-rate':'500','items-1-discount':'0','items-1-discount_type':'fixed','items-1-tax_rate':'5','items-1-tax_treatment':'taxable'})
  response=self.client.post(reverse('extraction_review',args=[a.pk]),data);self.assertEqual(response.status_code,302);a.invoice.refresh_from_db();self.assertEqual(a.invoice.line_items.count(),2);self.assertEqual(a.invoice.total_amount,D('1534.50'))
 def test_interstate_split_and_explicit_exempt_review(self):
  _,a=self.upload();response=self.review(a,place_of_supply='29',cgst_amount='0',sgst_amount='0',igst_amount='180');self.assertEqual(response.status_code,302)
  a.invoice.refresh_from_db();self.assertEqual(a.invoice.igst_amount,D('180'))
  response=self.review(a,total_amount='1000',cgst_amount='0',sgst_amount='0',igst_amount='0',**{'items-0-tax_rate':'0','items-0-tax_treatment':'exempt'});self.assertEqual(response.status_code,302);a.invoice.refresh_from_db();self.assertEqual(a.invoice.treatment_breakdown['exempt'],'1000.00')
 def test_rounding_exact_tolerance_and_unknown_classification(self):
  _,a=self.upload();self.assertEqual(self.review(a,rounding_adjustment='-0.01',total_amount='1179.99').status_code,302)
  self.assertEqual(self.review(a,rounding_adjustment='-0.01',total_amount='1180.00').status_code,200)
  self.assertEqual(self.review(a,**{'items-0-tax_treatment':''}).status_code,200)
 def test_original_file_tampering_blocks_confirmation(self):
  _,a=self.upload()
  with a.source_file.open('wb') as source:source.write(text_pdf('Different safe but altered document'))
  response=self.review(a);self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists())
 def test_review_snapshot_appends_without_rewriting_prior_confirmation(self):
  _,a=self.upload();self.review(a);first=Review.objects.get();snapshot=json.dumps(first.snapshot,sort_keys=True)
  self.assertEqual(self.review(a,**{'items-0-description':'Corrected description'}).status_code,302);self.assertEqual(Review.objects.count(),2);first.refresh_from_db();self.assertEqual(json.dumps(first.snapshot,sort_keys=True),snapshot)
 def test_stale_or_active_processing_does_not_falsely_complete(self):
  _,a=self.upload();Attempt.objects.filter(pk=a.pk).update(status='processing',completed_at=None);Invoice.objects.filter(pk=a.invoice_id).update(status='processing')
  self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,409)
  Attempt.objects.filter(pk=a.pk).update(started_at=timezone.now()-timedelta(minutes=3))
  with patch('invoices.extraction.extract_invoice_data',return_value=parsed()):self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,302)
  a.refresh_from_db();self.assertEqual(a.error_category,'interrupted');self.assertEqual(Attempt.objects.count(),2)
 def test_stale_first_upload_without_invoice_can_be_recovered_manually(self):
  _,a=self.upload();Attempt.objects.filter(pk=a.pk).update(invoice=None,status='processing',started_at=timezone.now()-timedelta(minutes=3))
  self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,302);a.refresh_from_db();self.assertEqual(a.status,'failed');self.assertEqual(a.error_category,'interrupted')
 def test_retry_hourly_limit(self):
  _,a=self.upload()
  with patch('invoices.extraction.extract_invoice_data',return_value=parsed()):
   for _ in range(2):
    a=Attempt.objects.first();self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,302)
  a=Attempt.objects.first();self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,429)
 def test_two_unknown_identifiers_do_not_collide_on_storage_placeholders(self):
  data=parsed(TEXT.replace('Invoice Number: INV-0009/26-A','Invoice identification unreadable'))
  self.upload(data);self.upload(data,body=text_pdf('Second unreadable source invoice'))
  self.assertEqual(Invoice.objects.count(),2);self.assertTrue(all(a.status!='processing' for a in Attempt.objects.all()))
 def test_preview_does_not_require_premature_confirmation(self):
  _,a=self.upload();response=self.review(a,preview='1',confirm='');self.assertEqual(response.status_code,200);self.assertIsNotNone(response.context['calculated']);self.assertFalse(Review.objects.exists())
 def test_real_text_upload_worker_preserves_source_and_requires_confirmation(self):
  response=self.client.post(reverse('invoice_upload'),{'invoice_type':'purchase','source_file':SimpleUploadedFile('text.pdf',text_pdf())})
  self.assertEqual(response.status_code,302);a=Attempt.objects.get();self.assertEqual(a.status,'review');self.assertEqual(a.method,'digital_pdf');self.assertEqual(a.result['invoice_number'],'INV-0009/26-A');self.assertFalse(Review.objects.exists())
 def test_unsafe_document_rejected_before_any_source_record(self):
  response=self.client.post(reverse('invoice_upload'),{'invoice_type':'purchase','source_file':SimpleUploadedFile('fake.png',b'%PDF-not-an-image')});self.assertEqual(response.status_code,200);self.assertFalse(Attempt.objects.exists());self.assertFalse(Invoice.objects.exists())
 def test_valid_token_from_another_attempt_cannot_confirm(self):
  _,a=self.upload();data=self.payload(a);_,b=self.upload(parsed(TEXT.replace('INV-0009/26-A','INV-SECOND')))
  response=self.client.post(reverse('extraction_review',args=[b.pk]),data);self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists())
 def test_buyer_required_for_sales_and_opposite_role_blocked(self):
  data=parsed(TEXT.replace('Synthetic Supplier','Synthetic Buyer').replace('27AAACS9002A1ZW','27AAACS9000A1ZY').replace('Buyer GSTIN: 27AAACS9000A1ZY','Buyer GSTIN: 27AAACS9002A1ZW'))
  _,a=self.upload(data,kind='sales');response=self.review(a,buyer_name='',party='',supplier_gstin='27AAACS9000A1ZY',buyer_gstin='');self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists())


class AdditionalResourceTests(SimpleTestCase):
 def test_worker_concurrency_is_limited_to_two(self):
  from .document_worker import worker_slot
  with worker_slot(),worker_slot():
   with self.assertRaises(ValidationError):
    with worker_slot():pass
 def test_ocr_page_budget_is_enforced(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'source.pdf';Image.new('RGB',(100,100),'white').save(p,'PDF')
   with override_settings(DOCUMENT_OCR_MAX_PAGES=0),patch('invoices.extraction._ocr_image') as ocr:data=_extract_local(p)
   ocr.assert_not_called();self.assertEqual(data['pages'][0]['error'],'ocr_timeout')
 def test_document_text_limit_is_reported_not_silently_complete(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'source.pdf';p.write_bytes(text_pdf())
   with override_settings(DOCUMENT_MAX_TEXT=60,DOCUMENT_PAGE_MAX_TEXT=60):data=_extract_local(p)
  self.assertLessEqual(len(data['pages'][0]['text']),60);self.assertTrue(any('limit' in warning for warning in data['validation_errors']))
 def test_partial_money_and_missing_invoice_number_not_invented(self):
  data=parsed(TEXT.replace('Grand Total: INR 1180.00','Grand Total: INR 1180O0').replace('Invoice Number: INV-0009/26-A','Invoice Number:\nInvoice Date: 20/11/2026'))
  self.assertIsNone(data['total_amount']);self.assertIsNone(data['invoice_number'])
 def test_multiple_table_rows_keep_pages_and_no_rate_zero_inference(self):
  table=[['Description','Qty','Rate','GST %','Total'],['Consulting','1','100','18','118'],['Exempt?','2','20','0','40']]
  data=parse_document([{'page':1,'text':TEXT,'method':'embedded','tables':[table]},{'page':2,'text':'Additional header text, no repeated rows','method':'embedded','tables':[]}],'digital_pdf')
  self.assertEqual(len(data['line_items']),2);self.assertIsNone(data['line_items'][1]['tax_treatment']);self.assertEqual(data['line_items'][0]['page'],1)
 def test_exif_orientation_is_applied_without_changing_original(self):
  image=Image.new('RGB',(120,200));exif=image.getexif();exif[274]=6;image.info['exif']=exif.tobytes();seen=[]
  with patch('invoices.extraction.pytesseract.image_to_string',side_effect=lambda img,**kw:seen.append(img.size) or TEXT):_ocr_image(image)
  self.assertGreater(seen[0][0],seen[0][1]);self.assertEqual(image.size,(120,200))
 def test_external_urls_are_never_fetched(self):
  with self.assertRaises(OSError):extract_invoice_data('https://example.invalid/invoice.pdf')
 def test_oversized_and_animated_images_rejected(self):
  with self.assertRaises(ValidationError):validate_document(SimpleUploadedFile('x.pdf',b'x'*(10*1024*1024+1)))
  out=BytesIO();first=Image.new('RGB',(20,20),'white');first.save(out,format='PNG',save_all=True,append_images=[Image.new('RGB',(20,20),'black')])
  with self.assertRaises(ValidationError):validate_document(SimpleUploadedFile('animated.png',out.getvalue()))
