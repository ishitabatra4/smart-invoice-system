"""Consolidated acceptance guards: synthetic test databases/storage only."""
from datetime import date
from decimal import Decimal as D
from copy import deepcopy
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase,SimpleTestCase,RequestFactory
from django.urls import reverse
from accounts.models import BusinessProfile,BusinessParty
from . import test_extraction as extraction, test_manual as manual
from .models import Invoice,InvoicePayment,InvoiceExtractionAttempt as Attempt,InvoiceExtractionReview as Review,InvoiceNumberSequence
from .extraction_services import validate_review_due_date
from .gstin import validate_document_gstins,validate_registration_state
from .manual_services import finalize_invoice


class SharedGSTValidationTests(SimpleTestCase):
    def test_registered_and_unregistered_buyer_supported(self):
        for buyer in ('','27AAACS9000A1ZY'):
            validate_document_gstins('27AAACS9002A1ZW',buyer,'27',D('180'))
    def test_unregistered_supplier_zero_tax_supported(self):
        validate_document_gstins('','','27',D('0'))
        with self.assertRaisesMessage(ValidationError,'GST-bearing invoice requires'):
            validate_document_gstins('','','27',D('1'))
    def test_invalid_checksums_rejected_consistently(self):
        for supplier,buyer in [('27PINEA1234B1Z2',''),('27AAACS9002A1ZW','27ABCDE1234F1Z5')]:
            with self.assertRaisesMessage(ValidationError,'checksum'):
                validate_document_gstins(supplier,buyer,'27',D('180'))
    def test_supplier_state_and_registration_state_checks(self):
        with self.assertRaisesMessage(ValidationError,'Supplier state'):
            validate_document_gstins('27AAACS9002A1ZW','','29',D('180'))
        for name in ('Maharashtra','27'):
            validate_registration_state('27AAACS9002A1ZW',name,'Selected party')
        with self.assertRaisesMessage(ValidationError,'state does not match'):
            validate_registration_state('27AAACS9002A1ZW','Rajasthan','Business profile')
        with self.assertRaisesMessage(ValidationError,'state could not be checked'):
            validate_registration_state('27AAACS9002A1ZW','Unknown State','Business profile')
    def test_place_of_supply_is_not_inferred_from_buyer_registration(self):
        # Bill-to registration and reviewed place of supply can legitimately differ.
        validate_document_gstins('27AAACS9002A1ZW','29ABCDE9876G1ZY','27',D('180'))
        validate_registration_state('29ABCDE9876G1ZY','Karnataka','Selected party')


class DueDateReviewTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('consolidated-ocr',email='ocr@example.test')
        self.business=BusinessProfile.objects.create(user=self.user,business_name='Synthetic Buyer',gstin='27AAACS9000A1ZY',state='Maharashtra')
        self.vendor=BusinessParty.objects.create(business=self.business,name='Synthetic Supplier',party_type='vendor',gstin='27AAACS9002A1ZW',state='Maharashtra')
        self.client.force_login(self.user)
    def upload(self,text=extraction.TEXT):
        response=self.client.post(reverse('invoice_upload'),{'invoice_type':'purchase','source_files':SimpleUploadedFile('synthetic-dates.pdf',extraction.text_pdf(text),content_type='application/pdf')})
        self.assertEqual(response.status_code,302);return Attempt.objects.first()
    def payload(self,a,**updates):return extraction.ExtractionWorkflowTests.payload(self,a,**updates)
    def test_due_candidate_on_provisional_detail_is_explicitly_unconfirmed(self):
        a=self.upload();before=deepcopy(a.result)
        for name,args in [('invoice_detail',[a.invoice_id]),('extraction_review',[a.pk])]:
            response=self.client.get(reverse(name,args=args))
            self.assertContains(response,'2026-12-10');self.assertContains(response,'unconfirmed source evidence')
            self.assertContains(response,'Not confirmed by a current review')
        a.invoice.refresh_from_db();self.assertIsNone(a.invoice.due_date);a.refresh_from_db();self.assertEqual(a.result,before)
    def test_known_due_clear_requires_acknowledgement_and_preserves_evidence(self):
        a=self.upload();before=deepcopy(a.result)
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a,due_date=''))
        self.assertEqual(response.status_code,200);self.assertContains(response,'Extracted due date is 2026-12-10')
        self.assertFalse(Review.objects.exists())
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a,due_date='',clear_due_date_confirmed='on'))
        self.assertEqual(response.status_code,302);a.invoice.refresh_from_db();self.assertIsNone(a.invoice.due_date)
        review=Review.objects.get();self.assertTrue(review.snapshot['due_date_clear_confirmed'])
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'verified');self.assertIsNone(a.invoice.due_date)
        a.refresh_from_db();self.assertEqual(a.result,before)
    def test_due_clear_cannot_bypass_service_with_empty_string(self):
        a=self.upload()
        for empty in (None,''):
            with self.assertRaisesMessage(ValidationError,'explicitly acknowledge'):
                validate_review_due_date(a,{'due_date':empty})
    def test_known_due_persists_through_review_and_verification(self):
        a=self.upload();self.assertEqual(self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a)).status_code,302)
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db()
        self.assertEqual(a.invoice.due_date,date(2026,12,10));self.assertEqual(a.invoice.status,'verified')
        response=self.client.get(reverse('invoice_detail',args=[a.invoice_id]));self.assertContains(response,'Confirmed review')
    def test_ambiguous_due_is_not_guessed_or_backfilled(self):
        a=self.upload(extraction.TEXT.replace('10 Dec 2026','05/07/2026'))
        self.assertIsNone(a.result['due_date']);self.assertEqual(a.result['confidence']['due_date']['level'],'conflicting')
        response=self.client.get(reverse('invoice_detail',args=[a.invoice_id]));self.assertContains(response,'Ambiguous due date')
        page=self.client.get(reverse('extraction_review',args=[a.pk]));self.assertNotIn('clear_due_date_confirmed',page.context['form'].fields)
        a.invoice.refresh_from_db();self.assertIsNone(a.invoice.due_date)
    def test_ocr_state_mismatch_blocks_confirmation(self):
        a=self.upload();self.vendor.state='Rajasthan';self.vendor.save(update_fields=['state'])
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a));self.assertEqual(response.status_code,200)
        self.assertContains(response,'Selected party state does not match');self.assertFalse(Review.objects.exists())
    def test_ocr_business_state_mismatch_blocks_confirmation(self):
        a=self.upload();self.business.state='Rajasthan';self.business.save(update_fields=['state'])
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a))
        self.assertEqual(response.status_code,200);self.assertContains(response,'Business profile state does not match');self.assertFalse(Review.objects.exists())
    def test_ocr_verification_rechecks_registration_state_after_confirmation(self):
        a=self.upload();self.assertEqual(self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a)).status_code,302)
        self.vendor.state='Rajasthan';self.vendor.save(update_fields=['state'])
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db()
        self.assertEqual(a.invoice.status,'review');self.assertFalse(InvoicePayment.objects.exists())
    def test_ocr_buyer_unregistered_and_zero_tax_unregistered_supplier(self):
        self.business.gstin='';self.business.state='';self.business.save()
        a=self.upload(extraction.TEXT.replace('Buyer GSTIN: 27AAACS9000A1ZY','Buyer GSTIN:'))
        response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a,buyer_gstin=''))
        self.assertEqual(response.status_code,302);self.assertEqual(Review.objects.count(),1)
        self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'verified')
    def test_ocr_zero_tax_unregistered_supplier_and_business(self):
        self.business.gstin='';self.business.state='';self.business.save();self.vendor.gstin='';self.vendor.state='';self.vendor.save()
        text=extraction.TEXT.replace('Supplier GSTIN: 27AAACS9002A1ZW','Supplier GSTIN:').replace('Buyer GSTIN: 27AAACS9000A1ZY','Buyer GSTIN:').replace('CGST 9%: INR 90.00','CGST: INR 0.00').replace('SGST 9%: INR 90.00','SGST: INR 0.00').replace('Grand Total: INR 1180.00','Grand Total: INR 1000.00').replace('18%','0%')
        a=self.upload(text);response=self.client.post(reverse('extraction_review',args=[a.pk]),self.payload(a,supplier_gstin='',buyer_gstin='',cgst_amount='0',sgst_amount='0',total_amount='1000',**{'items-0-tax_rate':'0','items-0-tax_treatment':'non_gst'}))
        self.assertEqual(response.status_code,302);self.client.post(reverse('invoice_verify',args=[a.invoice_id]),{'confirm':'on'});a.invoice.refresh_from_db();self.assertEqual(a.invoice.status,'verified')


class ManualGSTConsistencyTests(TestCase):
    setUp=manual.ManualInvoiceTests.setUp
    payload=manual.ManualInvoiceTests.payload
    def test_bad_business_checksum_prevents_creation_without_consuming_number(self):
        self.business.gstin='27ABCDE1234F1Z5';self.business.save()
        response=self.client.post(reverse('invoice_create',args=['sales']),self.payload())
        self.assertEqual(response.status_code,200);self.assertContains(response,'checksum');self.assertFalse(Invoice.objects.exists());self.assertFalse(InvoiceNumberSequence.objects.exists())
        self.business.refresh_from_db();self.assertEqual(self.business.gstin,'27ABCDE1234F1Z5')
    def test_bad_party_checksum_prevents_new_purchase(self):
        self.vendor.gstin='27PINEA1234B1Z2';self.vendor.save()
        response=self.client.post(reverse('invoice_create',args=['purchase']),self.payload('purchase'))
        self.assertEqual(response.status_code,200);self.assertContains(response,'checksum');self.assertFalse(Invoice.objects.exists())
    def test_known_business_and_party_state_discrepancies_prevent_creation(self):
        for target in (self.business,self.customer):
            target.state='Rajasthan';target.save()
            if target==self.customer:target.gstin='27FGHIJ2468K1ZB';target.save()
            response=self.client.post(reverse('invoice_create',args=['sales']),self.payload())
            self.assertEqual(response.status_code,200);self.assertContains(response,'state does not match');self.assertFalse(Invoice.objects.exists())
            target.state='';target.save()
    def test_missing_seller_registration_cannot_charge_gst(self):
        self.business.gstin=None;self.business.save()
        response=self.client.post(reverse('invoice_create',args=['sales']),self.payload())
        self.assertEqual(response.status_code,200);self.assertContains(response,'GST-bearing invoice requires');self.assertFalse(Invoice.objects.exists())
    def test_non_gst_and_unregistered_workflow_remains_supported(self):
        self.business.gstin=None;self.business.save();self.vendor.gstin='';self.vendor.save()
        for kind in ('sales','purchase'):
            response=self.client.post(reverse('invoice_create',args=[kind]),self.payload(kind,**{'items-0-tax_rate':'0','items-0-tax_treatment':'non_gst'}))
            self.assertEqual(response.status_code,302);invoice=Invoice.objects.get(pk=int(response.url.rstrip('/').split('/')[-1]))
            self.client.post(reverse('invoice_finalize',args=[invoice.pk]));invoice.refresh_from_db();self.assertEqual(invoice.status,'verified');self.assertEqual(invoice.total_tax,0)
    def test_finalization_checks_legacy_invalid_registration_without_rewriting_draft(self):
        response=self.client.post(reverse('invoice_create',args=['sales']),self.payload());invoice=Invoice.objects.get()
        self.business.gstin='27ABCDE1234F1Z5';self.business.save()
        self.client.post(reverse('invoice_finalize',args=[invoice.pk]));invoice.refresh_from_db();self.assertEqual(invoice.status,'draft')
        self.assertEqual(invoice.total_amount,D('224.20'))
    def test_finalizer_rejects_bad_saved_gstin_even_when_profile_snapshot_matches(self):
        self.client.post(reverse('invoice_create',args=['sales']),self.payload());invoice=Invoice.objects.get()
        Invoice.objects.filter(pk=invoice.pk).update(supplier_gstin='27ABCDE1234F1Z5')
        request=RequestFactory().post('/synthetic-finalize/');request.user=self.user
        with self.assertRaisesMessage(ValidationError,'checksum'):finalize_invoice(request=request,invoice_id=invoice.pk)
        invoice.refresh_from_db();self.assertEqual(invoice.status,'draft');self.assertEqual(invoice.total_amount,D('224.20'))
    def test_legacy_read_only_pages_do_not_revalidate_or_mutate_historical_identities(self):
        self.business.gstin='27ABCDE1234F1Z5';self.business.save()
        invoice=Invoice.objects.create(user=self.user,invoice_type='sales',invoice_number='SYNTHETIC-LEGACY',invoice_date=date(2026,10,1),supplier_name='Historical business',supplier_gstin=self.business.gstin,buyer_party=self.customer,buyer_name=self.customer.name,taxable_amount=1000,cgst_amount=90,sgst_amount=90,total_amount=1180,status='verified')
        before=list(Invoice.objects.values())
        for route,args in [('invoice_list',[]),('invoice_detail',[invoice.pk]),('analytics_dashboard',[]),('party_detail',[self.customer.pk])]:self.assertEqual(self.client.get(reverse(route,args=args)).status_code,200)
        self.assertEqual(list(Invoice.objects.values()),before)
    def test_payables_scope_labels_and_values_are_explicit(self):
        Invoice.objects.create(user=self.user,invoice_type='purchase',invoice_number='SYNTHETIC-PURCHASE',invoice_date=date(2026,11,1),supplier_party=self.vendor,supplier_name=self.vendor.name,total_amount=80,status='verified')
        Invoice.objects.create(user=self.user,invoice_type='expense',invoice_number='SYNTHETIC-EXPENSE',invoice_date=date(2026,11,1),supplier_party=self.vendor,supplier_name=self.vendor.name,total_amount=20,status='verified')
        response=self.client.get(reverse('analytics_dashboard'),{'period':'custom','start_date':'2026-11-01','end_date':'2026-11-30'})
        self.assertContains(response,'Purchase &amp; expense payables');self.assertEqual(response.context['totals']['payable'],D('100'))
        response=self.client.get(reverse('outstanding_dashboard'),{'q':'does-not-match'})
        self.assertContains(response,'Purchase invoice payables');self.assertContains(response,'expense bills are excluded');self.assertContains(response,'before the row filters')
        self.assertEqual(response.context['totals']['payable'],D('80'));self.assertEqual(response.context['rows'],[])
