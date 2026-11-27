from datetime import date,timedelta
from decimal import Decimal as D
from io import BytesIO,StringIO
from pathlib import Path
import csv,hashlib,json,shutil,tempfile,threading
from unittest.mock import patch
from django.test import TestCase,TransactionTestCase,Client,override_settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.db import close_old_connections,connection
from django.utils import timezone
from openpyxl import load_workbook
import pdfplumber
from accounts.models import BusinessProfile,BusinessParty
from audit.models import AuditEvent
from invoices.models import Invoice,InvoicePayment,InvoiceLineItem,InvoiceExtractionAttempt,InvoiceExtractionReview
from invoices.test_extraction import TEXT,parsed,text_pdf
from invoices import test_extraction as extraction_fixtures
from invoices.test_files import valid_pdf
from invoices.manual_calculations import calculate_invoice
from analytics.services import build_analytics_context
from . import tests as expense_fixtures
from .models import ExpenseCategory,ExpenseDetail,ExpenseITCReview,ExpenseAttachmentRevision,RecurringExpense,RecurringExpenseGeneration,private_expense_storage
from .recurrence import generate_due_expenses,_next_date
from .services import itc_state,link_purchase,save_metadata,review_itc
from .duplicates import expense_duplicates
from .reporting import report_rows,export_report


class AdvancedExpenseTests(TestCase):
 setUp=expense_fixtures.ExpenseWorkflowTests.setUp
 expense_payload=expense_fixtures.ExpenseWorkflowTests.expense_payload
 create_expense=expense_fixtures.ExpenseWorkflowTests.create_expense
 record_payment=expense_fixtures.ExpenseWorkflowTests.record_payment
 def setUp(self):
  expense_fixtures.ExpenseWorkflowTests.setUp(self)
  self.business.gstin='27AAACS9000A1ZY';self.business.save()
  self.supplier.gstin='27AAACS9002A1ZW';self.supplier.name='Synthetic Supplier';self.supplier.save()
  self.other=get_user_model().objects.create_user('expense-other')
  self.other_business=BusinessProfile.objects.create(user=self.other,business_name='Other business')
  self.other_category=ExpenseCategory.objects.create(business=self.other_business,name='Other private category')
  self.files=[]
 def tearDown(self):
  for name in self.files:private_expense_storage.delete(name)
 def attach(self,**updates):
  invoice=self.create_expense(attachment=SimpleUploadedFile('receipt.pdf',valid_pdf()),**updates)
  self.files.append(invoice.expense_detail.attachment.name);return invoice
 def snapshot(self,invoice):
  invoice.refresh_from_db();return (list(Invoice.objects.filter(pk=invoice.pk).values()),list(invoice.line_items.values()))
 def metadata(self,invoice,**updates):
  data={'title':invoice.expense_detail.title,'category':invoice.expense_detail.category_id,'description':'','notes':'changed'};data.update(updates);return self.client.post(reverse('expense_edit',args=[invoice.pk]),data)
 def itc(self,invoice,status='confirmed',**updates):
  data={'status':status,'reason':'Owner independently reviewed source and eligibility conditions.','attested':'on'};data.update(updates);return self.client.post(reverse('expense_itc_review',args=[invoice.pk]),data)
 def purchase(self,**updates):
  invoice=self.create_expense(**updates);Invoice.objects.filter(pk=invoice.pk).update(invoice_type='purchase');ExpenseDetail.objects.filter(invoice=invoice).delete();invoice.refresh_from_db();return invoice
 def link(self,invoice,**updates):
  data={'invoice':invoice.pk,'category':self.category.pk,'confirm':'on'};data.update(updates);return self.client.post(reverse('expense_purchase_link'),data)
 def schedule(self,frequency='monthly',start=None,**updates):
  start=start or timezone.localdate();data=dict(business=self.business,title='Synthetic subscription',category=self.category,supplier=self.supplier,taxable_amount=D('1180.03'),gst_rate=D('18'),gst_inclusive=True,supplier_state_code='27',place_of_supply='27',supply_category='standard',tax_treatment='taxable',frequency=frequency,start_date=start,next_due_date=start,schedule_day=start.day);data.update(updates);return RecurringExpense.objects.create(**data)
 def upload(self,body=None,name='receipt.pdf',data=None):
  with patch('invoices.views.extract_invoice_data',return_value=data or parsed()):
   response=self.client.post(reverse('expense_receipt_upload'),{'receipt':SimpleUploadedFile(name,body or text_pdf())})
  return response,InvoiceExtractionAttempt.objects.filter(user=self.user).first()
 def confirm_payload(self,a):
  t=extraction_fixtures.ExtractionWorkflowTests();t.client=self.client;t.vendor=self.supplier;return t.payload(a)
 def test_metadata_preserves_all_recognized_financial_fields_and_lines(self):
  invoice=self.attach();before=self.snapshot(invoice);self.assertEqual(self.metadata(invoice).status_code,302);self.assertEqual(before,self.snapshot(invoice));self.assertEqual(invoice.expense_detail.notes,'changed')
 def test_financial_tampering_rejected_without_recalculation(self):
  invoice=self.create_expense();before=self.snapshot(invoice)
  for key,value in [('amount','900'),('gst_rate','5'),('reference_number','different'),('supplier_state_code','29')]:
   response=self.metadata(invoice,**{key:value});self.assertEqual(response.status_code,200);self.assertEqual(before,self.snapshot(invoice))
 def test_replacement_and_removal_keep_accessible_revision_bytes(self):
  invoice=self.attach();original=invoice.expense_detail.attachment.name
  response=self.metadata(invoice,attachment=SimpleUploadedFile('replacement.pdf',valid_pdf()));self.assertEqual(response.status_code,302);invoice.refresh_from_db();replacement=invoice.expense_detail.attachment.name;self.files.append(replacement)
  self.metadata(invoice,remove_attachment='on');self.assertEqual(ExpenseAttachmentRevision.objects.filter(invoice=invoice).count(),2)
  self.assertTrue(private_expense_storage.exists(original));self.assertTrue(private_expense_storage.exists(replacement))
  for revision in invoice.expense_attachments.all():
   response=self.client.get(reverse('expense_receipt_revision',args=[revision.pk]));self.assertEqual(response.status_code,200);self.assertEqual(hashlib.sha256(b''.join(response.streaming_content)).hexdigest(),revision.sha256)
 def test_attachment_history_is_owned_and_append_only(self):
  invoice=self.attach();revision=invoice.expense_attachments.get();self.client.force_login(self.other);self.assertEqual(self.client.get(reverse('expense_receipt_revision',args=[revision.pk])).status_code,404)
  revision.display_name='tamper'
  with self.assertRaises(ValidationError):revision.save()
 def test_receipt_preview_is_private_and_sandboxed(self):
  invoice=self.attach();r=self.client.get(reverse('expense_attachment',args=[invoice.pk])+'?inline=1');self.assertEqual(r.status_code,200);self.assertIn('no-store',r['Cache-Control']);self.assertIn('sandbox',r['Content-Security-Policy']);r.close()
 def test_unpaid_cancellation_requires_reason_preserves_document(self):
  invoice=self.attach();self.client.post(reverse('expense_cancel',args=[invoice.pk]));invoice.refresh_from_db();self.assertEqual(invoice.status,'verified')
  self.client.post(reverse('expense_cancel',args=[invoice.pk]),{'reason':'Duplicate booking cancelled explicitly'});invoice.refresh_from_db();self.assertEqual(invoice.status,'cancelled');self.assertTrue(private_expense_storage.exists(invoice.expense_detail.attachment.name))
 def test_generic_invoice_delete_cannot_bypass_cancellation_reason(self):
  invoice=self.create_expense();self.client.post(reverse('invoice_delete',args=[invoice.pk]));invoice.refresh_from_db();self.assertEqual(invoice.status,'verified')
 def test_paid_and_partial_expenses_cannot_be_cancelled(self):
  invoice=self.create_expense();self.record_payment(invoice,'1','REF');self.client.post(reverse('expense_cancel',args=[invoice.pk]),{'reason':'Attempt'});invoice.refresh_from_db();self.assertEqual(invoice.status,'verified');self.assertEqual(invoice.payments.count(),1)
 def test_cross_business_category_rejected(self):
  invoice=self.create_expense();r=self.metadata(invoice,category=self.other_category.pk);self.assertEqual(r.status_code,200);self.assertEqual(invoice.expense_detail.category_id,self.category.pk)
 def test_category_reactivation_preserves_identity(self):
  self.category.is_active=False;self.category.save();r=self.client.post(reverse('expense_category_edit',args=[self.category.pk]),{'name':'  Renamed   Rent  ','is_active':'on'});self.assertEqual(r.status_code,302);self.category.refresh_from_db();self.assertEqual(self.category.name,'Renamed Rent');self.assertTrue(self.category.is_active)
 def test_category_duplicate_case_and_whitespace_rejected(self):
  before=ExpenseCategory.objects.filter(business=self.business).count();self.client.post(reverse('expense_categories'),{'name':' rent '});self.assertEqual(ExpenseCategory.objects.filter(business=self.business,name__iexact='rent').count(),1)
 def test_category_name_length_rejected(self):
  r=self.client.post(reverse('expense_categories'),{'name':'A'*101});self.assertEqual(r.status_code,200);self.assertFalse(ExpenseCategory.objects.filter(name='A'*101).exists())
 def test_metadata_supplier_and_gst_fields_not_exposed_for_edit(self):
  invoice=self.create_expense();form=self.client.get(reverse('expense_edit',args=[invoice.pk])).context['form'];self.assertNotIn('amount',form.fields);self.assertNotIn('supplier_party',form.fields)
 def test_itc_defaults_pending_not_claimed(self):
  invoice=self.attach();state=itc_state(invoice);self.assertEqual(state['status'],'pending');self.assertEqual(state['amount'],0);self.assertFalse(ExpenseITCReview.objects.exists())
 def test_itc_confirmed_snapshot_and_reviewer(self):
  invoice=self.attach();before=self.snapshot(invoice);r=self.itc(invoice);self.assertEqual(r.status_code,302);review=invoice.itc_reviews.get();self.assertEqual(review.reviewer,self.user);self.assertTrue(review.fingerprint);self.assertEqual(itc_state(invoice)['amount'],D('180'));self.assertEqual(before,self.snapshot(invoice))
 def test_itc_confirmation_requires_attestation(self):
  invoice=self.attach();self.assertEqual(self.itc(invoice,attested='').status_code,200);self.assertFalse(ExpenseITCReview.objects.exists())
 def test_itc_reason_required(self):
  invoice=self.attach();self.assertEqual(self.itc(invoice,reason='').status_code,200);self.assertFalse(ExpenseITCReview.objects.exists())
 def test_itc_invalid_supplier_gstin_rejected(self):
  invoice=self.attach();Invoice.objects.filter(pk=invoice.pk).update(supplier_gstin='27AAACS9002A1Z0');self.assertEqual(self.itc(invoice).status_code,200);self.assertFalse(ExpenseITCReview.objects.exists())
 def test_itc_document_for_another_buyer_cannot_be_confirmed(self):
  invoice=self.attach();Invoice.objects.filter(pk=invoice.pk).update(buyer_gstin='27AAPFU0939F1ZV');self.assertEqual(self.itc(invoice).status_code,200);self.assertFalse(ExpenseITCReview.objects.exists())
 def test_itc_missing_business_gstin_rejected(self):
  invoice=self.attach();self.business.gstin=None;self.business.save();self.assertEqual(self.itc(invoice).status_code,200)
 def test_itc_missing_receipt_rejected(self):
  invoice=self.create_expense();self.assertEqual(self.itc(invoice).status_code,200)
 def test_itc_zero_tax_not_confirmed(self):
  invoice=self.attach(gst_rate='0',tax_treatment='exempt');self.assertEqual(self.itc(invoice).status_code,200)
 def test_itc_unversioned_history_cannot_be_auto_confirmed(self):
  invoice=self.attach();Invoice.objects.filter(pk=invoice.pk).update(calculation_version=None);self.assertEqual(self.itc(invoice).status_code,200)
 def test_itc_pending_potential_ineligible_transitions(self):
  invoice=self.attach()
  for state in ['pending','potential','ineligible']:
   self.assertEqual(self.itc(invoice,state).status_code,302);self.assertEqual(itc_state(invoice)['status'],state)
 def test_itc_reversal_is_append_only_and_excludes_amount(self):
  invoice=self.attach();self.itc(invoice);self.assertEqual(self.itc(invoice,'reversed').status_code,302);self.assertEqual(invoice.itc_reviews.count(),2);self.assertEqual(itc_state(invoice)['amount'],0)
  review=invoice.itc_reviews.first()
  with self.assertRaises(ValidationError):review.save()
 def test_itc_cannot_reverse_unconfirmed(self):
  invoice=self.attach();self.assertEqual(self.itc(invoice,'reversed').status_code,200);self.assertFalse(ExpenseITCReview.objects.exists())
 def test_itc_repeat_post_is_idempotent(self):
  invoice=self.attach();self.itc(invoice);self.itc(invoice);self.assertEqual(invoice.itc_reviews.count(),1)
 def test_itc_source_change_invalidates_current_amount(self):
  invoice=self.attach();self.itc(invoice);Invoice.objects.filter(pk=invoice.pk).update(total_amount=D('1181'));invoice.refresh_from_db();self.assertEqual(itc_state(invoice)['status'],'pending');self.assertEqual(itc_state(invoice)['amount'],0)
 def test_itc_payment_does_not_change_reviewed_gst(self):
  invoice=self.attach();self.itc(invoice);self.record_payment(invoice,'1180','FULL');invoice.refresh_from_db();self.assertEqual(invoice.status,'paid');self.assertEqual(itc_state(invoice)['status'],'confirmed')
 def test_itc_cross_business_post_rejected(self):
  invoice=self.attach();self.client.force_login(self.other);self.assertEqual(self.itc(invoice).status_code,404)
 def test_itc_csrf_and_authentication(self):
  invoice=self.attach();c=Client(enforce_csrf_checks=True);c.force_login(self.user);self.assertEqual(c.post(reverse('expense_itc_review',args=[invoice.pk]),{}).status_code,403);self.assertEqual(Client().get(reverse('expense_itc_review',args=[invoice.pk])).status_code,302)
 def test_itc_audit_contains_actor_not_raw_document(self):
  invoice=self.attach();self.itc(invoice);event=AuditEvent.objects.filter(object_id=str(invoice.pk),after__itc_status='confirmed').get();self.assertEqual(event.user,self.user);self.assertNotIn('Cloud consulting',str(event.metadata))
 def test_purchase_link_is_same_authoritative_invoice(self):
  invoice=self.purchase();before=self.snapshot(invoice);count=Invoice.objects.count();self.assertEqual(self.link(invoice).status_code,302);self.assertEqual(Invoice.objects.count(),count);self.assertEqual(before,self.snapshot(invoice));self.assertEqual(invoice.expense_detail.category_id,self.category.pk)
 def test_purchase_link_repeated_post_is_idempotent(self):
  invoice=self.purchase();self.link(invoice);self.link(invoice);self.assertEqual(ExpenseDetail.objects.filter(invoice=invoice).count(),1)
 def test_purchase_link_does_not_change_financial_analytics(self):
  invoice=self.purchase();before=build_analytics_context(self.user,date.today(),date.today(),'custom')['totals'];self.link(invoice);after=build_analytics_context(self.user,date.today(),date.today(),'custom')['totals'];self.assertEqual(before,after);self.assertEqual(after['expenses'],0);self.assertEqual(after['purchases'],D('1180'));self.assertEqual(after['input_gst'],D('180'));self.assertEqual(after['payable'],D('1180'))
 def test_linked_purchase_report_counts_once(self):
  invoice=self.purchase();self.link(invoice);form,rows,totals=report_rows(self.user,self.business,{'report_type':'linked'});self.assertTrue(form.is_valid());self.assertEqual(len(rows),1);self.assertEqual(totals['total'],D('1180'));self.assertEqual(rows[0]['source_type'],'purchase')
 def test_linked_purchase_payments_use_existing_ledger(self):
  invoice=self.purchase();self.link(invoice);self.record_payment(invoice,'500','ONE');self.record_payment(invoice,'680','TWO');invoice.refresh_from_db();self.assertEqual(invoice.status,'paid');self.assertEqual(invoice.payments.count(),2)
 def test_linked_purchase_cannot_be_cancelled_from_register(self):
  invoice=self.purchase();self.link(invoice);self.client.post(reverse('expense_cancel',args=[invoice.pk]),{'reason':'No'});invoice.refresh_from_db();self.assertEqual(invoice.status,'verified')
 def test_linking_unconfirmed_purchase_rejected(self):
  invoice=self.purchase();Invoice.objects.filter(pk=invoice.pk).update(status='review');self.assertEqual(self.link(invoice).status_code,200);self.assertFalse(ExpenseDetail.objects.filter(invoice=invoice).exists())
 def test_linking_without_confirmation_rejected(self):
  invoice=self.purchase();self.assertEqual(self.link(invoice,confirm='').status_code,200)
 def test_cross_business_purchase_category_rejected(self):
  invoice=self.purchase();self.assertEqual(self.link(invoice,category=self.other_category.pk).status_code,200)
 def test_cross_business_purchase_link_rejected(self):
  invoice=self.purchase();self.client.force_login(self.other);self.assertEqual(self.link(invoice,category=self.other_category.pk).status_code,200);self.assertFalse(ExpenseDetail.objects.filter(invoice=invoice).exists())
 def test_manual_expense_duplicate_purchase_reference_rejected(self):
  invoice=self.purchase();r=self.client.post(reverse('expense_add'),self.expense_payload());self.assertEqual(r.status_code,200);self.assertEqual(Invoice.objects.filter(user=self.user).count(),1)
 def test_equal_amount_with_different_reference_is_not_duplicate(self):
  self.create_expense();self.assertEqual(expense_duplicates(self.user,'OTHER',self.supplier.gstin,date.today(),D('1180')),[])
 def test_duplicate_detection_cannot_leak_other_business(self):
  self.create_expense();self.assertEqual(expense_duplicates(self.other,'RENT-001',self.supplier.gstin,date.today(),D('1180')),[])
 def test_receipt_upload_reuses_phase9_and_does_not_pay_or_claim(self):
  response,a=self.upload();self.assertEqual(response.status_code,302);self.assertEqual(a.invoice.invoice_type,'expense');self.assertEqual(a.invoice.status,'review');self.assertFalse(InvoicePayment.objects.exists());self.assertFalse(ExpenseITCReview.objects.exists());self.assertFalse(ExpenseDetail.objects.filter(invoice=a.invoice).exists())
 def test_receipt_ocr_confirm_still_requires_separate_verification(self):
  _,a=self.upload();payload=self.confirm_payload(a);r=self.client.post(reverse('extraction_review',args=[a.pk]),payload);self.assertEqual(r.status_code,302,str(r.context['form'].errors) if r.context else '');a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review');self.assertEqual(InvoiceExtractionReview.objects.count(),1)
 def test_receipt_missing_fields_stay_unknown(self):
  _,a=self.upload(data=parsed('Retail receipt with no reliable identifiers'));self.assertIsNone(a.result['supplier_gstin']);self.assertIsNone(a.result['invoice_number']);self.assertEqual(a.invoice.status,'review')
 def test_receipt_invalid_gstin_not_repaired(self):
  _,a=self.upload(data=parsed(TEXT.replace('27AAACS9002A1ZW','27AAACS9002A1Z0')));self.assertEqual(a.result['confidence']['supplier_gstin']['level'],'low');self.assertEqual(a.result['supplier_gstin'],'27AAACS9002A1Z0')
 def test_receipt_duplicate_retained_without_second_invoice(self):
  self.upload();response,a=self.upload();self.assertEqual(a.status,'blocked');self.assertEqual(Invoice.objects.filter(invoice_type='expense').count(),1)
 def test_receipt_unsafe_upload_creates_no_financial_record(self):
  r,a=self.upload(body=b'not a PDF');self.assertEqual(r.status_code,200);self.assertIsNone(a);self.assertFalse(Invoice.objects.exists())
 def test_receipt_edit_route_cannot_bypass_phase9_review(self):
  _,a=self.upload();before=self.snapshot(a.invoice);r=self.client.post(reverse('expense_edit',args=[a.invoice_id]),self.expense_payload());self.assertEqual(r.status_code,200);self.assertEqual(self.snapshot(a.invoice),before);self.assertFalse(InvoiceExtractionReview.objects.exists())
 def test_receipt_draft_category_metadata_does_not_recognize_finances(self):
  _,a=self.upload();r=self.client.post(reverse('expense_edit',args=[a.invoice_id]),{'title':'OCR draft cost','category':self.category.pk,'notes':'Review pending'});self.assertEqual(r.status_code,302);a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'review');self.assertEqual(a.invoice.expense_detail.category_id,self.category.pk);self.assertFalse(InvoicePayment.objects.exists());self.assertFalse(InvoiceExtractionReview.objects.exists())
 def test_receipt_unreviewed_draft_cannot_receive_payment(self):
  _,a=self.upload();self.record_payment(a.invoice,'1','NO');self.assertFalse(InvoicePayment.objects.exists())
 def test_mixed_gst_receipt_is_never_collapsed_by_metadata_edit(self):
  invoice=self.create_expense();InvoiceLineItem.objects.create(invoice=invoice,description='second line',quantity=1,rate=0,tax_rate=5,tax_treatment='taxable');before=self.snapshot(invoice);self.metadata(invoice);self.assertEqual(self.snapshot(invoice),before);self.assertEqual(invoice.line_items.count(),2)
 def test_mixed_positive_rate_expense_review_preserves_both_lines(self):
  body=TEXT.replace('1000.00\nCGST','1100.00\nCGST').replace('Subtotal: INR 1000.00','Subtotal: INR 1100.00').replace('CGST 9%: INR 90.00','CGST: INR 92.50').replace('SGST 9%: INR 90.00','SGST: INR 92.50').replace('Grand Total: INR 1180.00','Grand Total: INR 1285.00')+'2 Second synthetic service 9983 1 100.00 100.00 5%\n'
  _,a=self.upload(body=text_pdf(body),data=parsed(body));payload=self.confirm_payload(a)
  payload.update({'taxable_amount':'1100','cgst_amount':'92.50','sgst_amount':'92.50','total_amount':'1285','items-TOTAL_FORMS':'2','items-INITIAL_FORMS':'2','items-1-description':'Second synthetic service','items-1-hsn_sac_code':'9983','items-1-quantity':'1','items-1-unit':'Hr','items-1-rate':'100','items-1-discount':'0','items-1-discount_type':'fixed','items-1-tax_rate':'5','items-1-tax_treatment':'taxable'})
  r=self.client.post(reverse('extraction_review',args=[a.pk]),payload);self.assertEqual(r.status_code,302,str(r.context['form'].errors) if r.context else '')
  before=self.snapshot(a.invoice);self.client.post(reverse('expense_edit',args=[a.invoice_id]),{'title':'Mixed expense','category':self.category.pk,'notes':'Reviewed'});self.assertEqual(before,self.snapshot(a.invoice));self.assertEqual(a.invoice.line_items.count(),2)
  self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'verified');self.assertEqual(a.invoice.total_amount,D('1285'));_,rows,totals=report_rows(self.user,self.business,{});self.assertEqual(totals['gst'],D('185'));self.assertFalse(InvoicePayment.objects.exists())
 def test_recurring_historical_catchup_requires_explicit_choice(self):
  schedule=self.schedule(start=date(2024,1,31));self.assertEqual(generate_due_expenses(self.business,date(2024,1,31)),[]);self.assertEqual(len(generate_due_expenses(self.business,date(2024,1,31),allow_backfill=True)),1)
 def test_recurring_month_end_and_leap_year(self):
  s=self.schedule(start=date(2024,1,31));self.assertEqual(_next_date(s,date(2024,1,31)),date(2024,2,29));self.assertEqual(_next_date(s,date(2024,2,29)),date(2024,3,31))
 def test_recurring_quarterly_and_yearly_anchors(self):
  for frequency,start,expected in [('quarterly',date(2024,1,31),date(2024,4,30)),('yearly',date(2024,2,29),date(2025,2,28)),('weekly',date(2024,1,31),date(2024,2,7))]:
   s=self.schedule(frequency,start);self.assertEqual(_next_date(s,start),expected)
 def test_recurring_generation_is_bounded_and_idempotent(self):
  s=self.schedule('weekly',date(2024,1,1));rows=generate_due_expenses(self.business,date(2024,12,31),allow_backfill=True,max_occurrences=2);self.assertEqual(len(rows),2);self.assertEqual(s.generations.count(),2);self.assertEqual(len(generate_due_expenses(self.business,date(2024,1,1),allow_backfill=True)),0);self.assertFalse(InvoicePayment.objects.exists())
 def test_recurring_inclusive_saved_values_are_authoritative(self):
  self.schedule();invoice=generate_due_expenses(self.business)[0];self.assertEqual((invoice.taxable_amount,invoice.total_amount),(D('1000.03'),D('1180.03')));self.assertTrue(invoice.expense_detail.gst_inclusive)
 def test_recurring_pause_end_and_no_daily_invention(self):
  s=self.schedule(is_active=False);self.assertEqual(generate_due_expenses(self.business),[]);self.assertNotIn('daily',dict(RecurringExpense.FREQUENCIES))
 def test_recurring_unauthorized_actor_rejected(self):
  self.schedule()
  with self.assertRaises(ValidationError):generate_due_expenses(self.business,actor=self.other)
 def test_report_itc_summary_reconciles_with_row_values(self):
  invoice=self.attach();self.itc(invoice);form,rows,totals=report_rows(self.user,self.business,{'report_type':'itc'});self.assertTrue(form.is_valid());self.assertEqual(totals['itc']['confirmed'],D('180'));self.assertEqual(rows[0]['itc_status'],'confirmed')
 def test_report_itc_queries_do_not_grow_per_row(self):
  from django.db import connection
  from django.test.utils import CaptureQueriesContext
  first=self.attach();self.itc(first)
  with CaptureQueriesContext(connection) as one:report_rows(self.user,self.business,{'report_type':'itc'})
  for n in range(3):
   invoice=self.create_expense(reference_number=f'RENT-10{n}',amount=f'10{n+1}0.00',acknowledge_duplicates='on');self.itc(invoice)
  with CaptureQueriesContext(connection) as many:_,rows,totals=report_rows(self.user,self.business,{'report_type':'itc'})
  self.assertEqual(len(rows),4)
  from .services import itc_state
  legacy=[itc_state(Invoice.objects.get(pk=r['invoice'].pk)) for r in rows]  # unbatched original path
  self.assertEqual([r['itc_status'] for r in rows],[x['status'] for x in legacy])
  self.assertEqual(totals['itc']['confirmed'],sum((x['amount'] for x in legacy if x['status']=='confirmed'),D('0')))
  self.assertEqual(len(many.captured_queries),len(one.captured_queries))
 def test_export_itc_attribution_and_formula_safety(self):
  self.create_expense(title='=SUM(1,2)');self.supplier.name='=DANGEROUS()';self.supplier.save();Invoice.objects.filter(user=self.user).update(supplier_name='=DANGEROUS()');_,rows,totals=report_rows(self.user,self.business,{})
  csv_data=export_report(rows,'complete',totals,'csv').content.decode();self.assertIn('ITC preparation status',csv_data);self.assertNotIn(',=DANGEROUS()',csv_data)
  wb=load_workbook(BytesIO(export_report(rows,'complete',totals,'xlsx').content));self.assertNotEqual(wb.active['C2'].data_type,'f');self.assertIn('Source type',[c.value for c in wb.active[1]])
  pdf=export_report(rows,'complete',totals,'pdf').content
  with pdfplumber.open(BytesIO(pdf)) as doc:self.assertIn('ITC preparation',doc.pages[0].extract_text())
 def test_report_cross_business_filters_fail_closed(self):
  self.create_expense();form,rows,_=report_rows(self.user,self.business,{'category':self.other_category.pk});self.assertFalse(form.is_valid());self.assertEqual(rows,[])
 def test_itc_status_search_and_supplier_filters(self):
  invoice=self.attach();self.itc(invoice);r=self.client.get(reverse('expense_list'),{'itc':'confirmed','q':'RENT','supplier':self.supplier.pk});self.assertContains(r,invoice.invoice_number);r=self.client.get(reverse('expense_list'),{'itc':'ineligible'});self.assertNotContains(r,'RENT-001')
 def test_expense_pages_reject_foreign_objects(self):
  invoice=self.attach();self.client.force_login(self.other)
  for name in ['expense_detail','expense_edit','expense_attachment']:self.assertEqual(self.client.get(reverse(name,args=[invoice.pk])).status_code,404)


 @__import__('unittest').skipUnless(shutil.which('tesseract'),'Local real-OCR acceptance requires Tesseract')
 def test_real_pdf_receipt_uses_existing_ocr_pipeline(self):
  self.real_receipt('pdf')
 @__import__('unittest').skipUnless(shutil.which('tesseract'),'Local real-OCR acceptance requires Tesseract')
 def test_real_png_receipt_uses_existing_ocr_pipeline(self):
  self.real_receipt('png')
 @__import__('unittest').skipUnless(shutil.which('tesseract'),'Local real-OCR acceptance requires Tesseract')
 def test_real_jpeg_receipt_uses_existing_ocr_pipeline(self):
  self.real_receipt('jpeg')
 def real_receipt(self,kind):
  import pypdfium2 as pdfium
  from reportlab.pdfgen.canvas import Canvas
  from reportlab.lib.pagesizes import A4
  from reportlab.lib.utils import ImageReader
  out=BytesIO();canvas=Canvas(out,pagesize=A4);text=canvas.beginText(35,795);text.setFont('Helvetica',13)
  for line in TEXT.splitlines():text.textLine(line)
  canvas.drawText(text);canvas.save();doc=pdfium.PdfDocument(out.getvalue());page=doc[0];bitmap=page.render(scale=3);image=bitmap.to_pil().copy();bitmap.close();page.close();doc.close();out=BytesIO()
  if kind=='pdf':
   canvas=Canvas(out,pagesize=A4);canvas.drawImage(ImageReader(image),0,0,width=A4[0],height=A4[1]);canvas.save()
  else:image.save(out,'PNG' if kind=='png' else 'JPEG')
  response=self.client.post(reverse('expense_receipt_upload'),{'receipt':SimpleUploadedFile('receipt.'+kind,out.getvalue())});self.assertEqual(response.status_code,302);attempt=InvoiceExtractionAttempt.objects.get(user=self.user)
  for key,value in [('invoice_number','INV-0009/26-A'),('supplier_gstin','27AAACS9002A1ZW'),('invoice_date','2026-11-20')]:self.assertEqual(attempt.result[key],value)
  self.assertEqual(D(attempt.result['total_amount']),D('1180'));self.assertEqual(attempt.result['pages'][0]['method'],'ocr');self.assertEqual(attempt.invoice.status,'review');self.assertFalse(InvoicePayment.objects.exists());self.assertFalse(ExpenseITCReview.objects.exists())
 def test_stale_confirmed_itc_can_be_explicitly_reversed(self):
  invoice=self.attach();self.itc(invoice);Invoice.objects.filter(pk=invoice.pk).update(total_amount=D('1181'));self.assertEqual(self.itc(invoice,'reversed').status_code,302);invoice.refresh_from_db();self.assertEqual(itc_state(invoice)['status'],'reversed');self.assertEqual(itc_state(invoice)['amount'],0)
 def adjustment(self,invoice,kind='credit',amount='200'):
  from invoices.models import InvoiceAdjustmentNote
  return InvoiceAdjustmentNote.objects.create(user=self.user,business=self.business,original_invoice=invoice,party=self.supplier,kind=kind,direction='received',number='CN/2627/000001',supplier_reference='SUP-CN-1',year_start=date(2026,4,1),document_date=date.today(),reason='Synthetic financial adjustment',tax_effect='financial',status='finalized',finalized_at=timezone.now(),pricing_mode='exclusive',supplier_state_code='27',place_of_supply='27',taxable_amount=D(amount),total_amount=D(amount),original_snapshot={'type':'purchase'})
 def test_dashboard_distinguishes_weighted_gst_estimate_from_saved_gst(self):
  invoice=self.purchase();self.link(invoice);self.adjustment(invoice);self.record_payment(invoice,'980','NET-GST')
  response=self.client.get(reverse('expense_dashboard'),{'period':'today'})
  self.assertEqual(response.context['saved_gst'],D('180'))
  self.assertEqual(response.context['totals']['gst'],D('149.49'))
  self.assertContains(response,'Payment-weighted GST estimate')
  self.assertContains(response,'gross bill totals')
  self.assertNotContains(response,'GST paid on expenses')
 def test_credit_adjusted_purchase_payable_settles_net_balance(self):
  invoice=self.purchase();self.link(invoice);self.adjustment(invoice)
  self.record_payment(invoice,'980','NET');invoice.refresh_from_db();self.assertEqual(invoice.status,'paid');self.assertEqual(invoice.total_amount,D('1180'))
  r=self.client.get(reverse('expense_detail',args=[invoice.pk]));self.assertEqual(r.context['outstanding'],0);self.assertEqual(r.context['payment_state'],'paid')
 def test_debit_note_reopens_remaining_payable_on_paid_purchase(self):
  invoice=self.purchase();self.link(invoice);self.record_payment(invoice,'1180','INITIAL');self.adjustment(invoice,'debit','100');invoice.refresh_from_db()
  r=self.client.get(reverse('expense_detail',args=[invoice.pk]));self.assertEqual(r.context['outstanding'],D('100'));self.assertEqual(r.context['payment_state'],'partial');self.record_payment(invoice,'100','EXTRA');self.assertEqual(invoice.payments.count(),2)
 def test_duplicate_payment_reference_is_normalized_and_rejected(self):
  invoice=self.create_expense();self.record_payment(invoice,'100',' REF-1 ');self.record_payment(invoice,'100','ref-1');self.assertEqual(invoice.payments.count(),1)
 def test_identical_manual_receipt_cannot_be_recognized_twice(self):
  invoice=self.attach()
  with invoice.expense_detail.attachment.open('rb') as source:body=source.read()
  r=self.client.post(reverse('expense_add'),self.expense_payload(reference_number='DIFFERENT',attachment=SimpleUploadedFile('same.pdf',body)));self.assertEqual(r.status_code,200);self.assertEqual(Invoice.objects.filter(user=self.user).count(),1)
 def test_large_report_requires_narrower_filters_before_loading_rows(self):
  Invoice.objects.bulk_create([Invoice(user=self.user,invoice_type='expense',invoice_number=f'LIMIT-{i}',invoice_date=date.today(),total_amount=0,status='verified') for i in range(5001)])
  form,rows,_=report_rows(self.user,self.business,{})
  self.assertFalse(form.is_valid());self.assertEqual(rows,[]);self.assertIn('Report limit reached',str(form.errors))


class RecurringConcurrencyTests(TransactionTestCase):
 def test_two_workers_create_one_occurrence_without_skipping(self):
  user=get_user_model().objects.create_user('recurring-concurrent');business=BusinessProfile.objects.create(user=user,business_name='Synthetic');category=ExpenseCategory.objects.create(business=business,name='Utilities');today=timezone.localdate()
  s=RecurringExpense.objects.create(business=business,category=category,title='Internet',taxable_amount=100,gst_rate=0,supplier_state_code='27',place_of_supply='27',supply_category='standard',tax_treatment='exempt',frequency='monthly',start_date=today,next_due_date=today,schedule_day=today.day)
  barrier=threading.Barrier(2);errors=[]
  def worker():
   close_old_connections()
   try:barrier.wait(timeout=10);generate_due_expenses(business)
   except Exception as exc:errors.append(type(exc).__name__+': '+str(exc))
   finally:close_old_connections()
  threads=[threading.Thread(target=worker) for _ in range(2)]
  for t in threads:t.start()
  for t in threads:t.join(timeout=20)
  self.assertFalse(errors,errors);self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(s.generations.count(),1);self.assertEqual(Invoice.objects.count(),1)
