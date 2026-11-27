"""Owned provisional lifecycle actions and conservative role evidence."""
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, SimpleTestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from accounts.models import BusinessProfile, BusinessParty
from audit.models import AuditEvent
from audit.services import record_event
from expenses.models import ExpenseITCReview, ExpenseCategory, ExpenseDetail
from taxation.models import ReconciliationRun, ReconciliationRecord
from . import test_extraction as fixtures
from .models import Invoice, InvoicePayment, InvoiceExtractionAttempt as Attempt, InvoiceExtractionReview as Review
from .classification import classification_assessment
from .management_services import manage_provisional, state_token, eligibility_errors, activity
from analytics.services import build_analytics_context


class ClassificationTests(SimpleTestCase):
    def assessment(self, kind='purchase', **updates):
        data=fixtures.parsed();data.update(updates)
        business=SimpleNamespace(gstin='27AAACS9000A1ZY',business_name='Synthetic Buyer',state='Maharashtra')
        return classification_assessment(business,data,kind)

    def test_correct_sales(self):
        self.assertEqual(self.assessment('sales',supplier_gstin='27AAACS9000A1ZY',buyer_gstin='27AAACS9002A1ZW')['status'],'consistent')
    def test_correct_purchase(self):self.assertEqual(self.assessment()['status'],'consistent')
    def test_correct_expense_never_infers_purchase_versus_expense(self):
        result=self.assessment('expense');self.assertEqual(result['status'],'consistent');self.assertTrue(any('cannot distinguish' in m for m in result['messages']))
    def test_wrong_sales(self):self.assertEqual(self.assessment('sales')['status'],'wrong_type')
    def test_wrong_purchase(self):self.assertEqual(self.assessment('purchase',supplier_gstin='27AAACS9000A1ZY',buyer_gstin='27AAACS9002A1ZW')['status'],'wrong_type')
    def test_wrong_expense(self):self.assertEqual(self.assessment('expense',supplier_gstin='27AAACS9000A1ZY',buyer_gstin='27AAACS9002A1ZW')['status'],'wrong_type')
    def test_missing_supplier(self):self.assertEqual(self.assessment(supplier_gstin=None)['status'],'insufficient')
    def test_missing_buyer(self):self.assertEqual(self.assessment(buyer_gstin=None)['status'],'insufficient')
    def test_invalid_gstin(self):self.assertEqual(self.assessment(supplier_gstin='27INVALID')['status'],'insufficient')
    def test_conflicting_or_low_confidence_gstin(self):
        for level in ('conflicting','low','missing'):
            self.assertEqual(self.assessment(confidence={'buyer_gstin':{'level':level}})['status'],'insufficient')
    def test_both_roles_match(self):self.assertEqual(self.assessment(supplier_gstin='27AAACS9000A1ZY')['status'],'ambiguous')
    def test_neither_role_matches(self):self.assertEqual(self.assessment(buyer_gstin='27AAACS9002A1ZW')['status'],'identity_mismatch')
    def test_name_cannot_override_gstin_and_conflict_is_explicit(self):
        result=self.assessment(supplier_name='Synthetic Buyer',buyer_name='Different legal name')
        self.assertEqual(result['status'],'consistent');self.assertTrue(any('Name and GSTIN' in m for m in result['messages']))
    def test_dual_role_names_do_not_make_identity_ambiguous(self):
        self.assertEqual(self.assessment(supplier_name='Synthetic Buyer',buyer_name='Synthetic Buyer')['status'],'consistent')
    def test_state_and_special_label_warnings(self):
        result=self.assessment(supplier_state_code='29',detected_document_kind='credit note')
        self.assertTrue(any('state evidence' in m for m in result['messages']));self.assertTrue(any('dedicated document workflow' in m for m in result['messages']))
    def test_no_evidence_is_not_changed_by_assessment(self):
        data=fixtures.parsed();before=deepcopy(data);self.assessment(**data);self.assertEqual(data,before)


class ManagementTests(TestCase):
    setUp=fixtures.ExtractionWorkflowTests.setUp
    upload=fixtures.ExtractionWorkflowTests.upload
    payload=fixtures.ExtractionWorkflowTests.payload
    review=fixtures.ExtractionWorkflowTests.review

    def new(self,kind='sales'):
        response,a=self.upload(kind=kind);self.assertEqual(response.status_code,302);return a.invoice,a
    def request(self):
        request=RequestFactory().post('/isolated-management/');request.user=self.user;return request
    def token(self,invoice,operation='change_type'):
        invoice.refresh_from_db();return state_token(invoice,self.business,operation,invoice.extraction_attempts.first())
    def change(self,invoice,target='purchase',**updates):
        values={'request':self.request(),'invoice_id':invoice.pk,'operation':'change_type','token':self.token(invoice),'reason':'Fictional upload selected the wrong category','confirm':True,'target_type':target};values.update(updates)
        return manage_provisional(**values)
    def cancel(self,invoice):
        self.assertEqual(self.client.post(reverse('invoice_delete',args=[invoice.pk]),{'reason':'Synthetic provisional cancellation'}).status_code,302);invoice.refresh_from_db()
    def restore(self,invoice,**updates):
        values={'request':self.request(),'invoice_id':invoice.pk,'operation':'restore','token':self.token(invoice,'restore'),'reason':'Fictional accidental provisional cancellation','confirm':True};values.update(updates)
        return manage_provisional(**values)
    def totals(self):return build_analytics_context(self.user,date(2026,1,1),date(2026,12,31),'custom')['totals']
    def assert_change(self,current,target):
        invoice,a=self.new(current);self.change(invoice,target);invoice.refresh_from_db();self.assertEqual(invoice.invoice_type,target);self.assertEqual(invoice.status,'review')
    def test_sales_to_purchase(self):self.assert_change('sales','purchase')
    def test_sales_to_expense(self):self.assert_change('sales','expense')
    def test_purchase_to_sales(self):self.assert_change('purchase','sales')
    def test_purchase_to_expense(self):self.assert_change('purchase','expense')
    def test_expense_to_sales(self):self.assert_change('expense','sales')
    def test_expense_to_purchase(self):self.assert_change('expense','purchase')
    def test_unsupported_type(self):
        invoice,a=self.new()
        with self.assertRaises(ValidationError):self.change(invoice,'credit')
    def test_missing_reason(self):
        invoice,a=self.new()
        with self.assertRaises(ValidationError):self.change(invoice,reason='  ')
    def test_missing_confirmation(self):
        invoice,a=self.new()
        with self.assertRaises(ValidationError):self.change(invoice,confirm=False)
    def test_same_type_no_change_or_event(self):
        invoice,a=self.new();count=AuditEvent.objects.count()
        with self.assertRaises(ValidationError):self.change(invoice,'sales')
        self.assertEqual(AuditEvent.objects.count(),count)
    def blocked(self,field,value):
        invoice,a=self.new();setattr(invoice,field,value);invoice.save(update_fields=[field])
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_verified_rejected(self):self.blocked('status','verified')
    def test_paid_rejected(self):self.blocked('status','paid')
    def test_finalized_rejected(self):self.blocked('document_state','finalized')
    def test_processing_rejected(self):self.blocked('status','processing')
    def test_cancelled_requires_restoration(self):self.blocked('status','cancelled')
    def test_manual_or_legacy_rejected(self):
        for mode in ('manual','legacy'):
            with self.subTest(mode=mode):
                data=fixtures.parsed(fixtures.TEXT.replace('INV-0009/26-A','SYNTHETIC-'+mode));_,a=self.upload(data=data,body=fixtures.text_pdf(fixtures.TEXT+'\n'+mode));invoice=a.invoice;invoice.entry_mode=mode;invoice.document_state='draft' if mode=='manual' else 'legacy';invoice.status='draft' if mode=='manual' else 'review';invoice.save()
                with self.assertRaises(ValidationError):self.change(invoice)
    def test_confirmed_extraction_rejected(self):
        invoice,a=self.new();Review.objects.create(user=self.user,business=self.business,invoice=invoice,attempt=a,snapshot={})
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_payment_dependency_rejected(self):
        invoice,a=self.new();InvoicePayment.objects.create(user=self.user,invoice=invoice,party=self.vendor,payment_date=date(2026,11,20),amount=D('1'),payment_mode='cash',reference_id='SYNTHETIC')
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_itc_review_dependency_rejected(self):
        invoice,a=self.new();ExpenseITCReview.objects.create(invoice=invoice,business=self.business,reviewer=self.user,status='pending',reason='Synthetic',fingerprint='a'*64,evidence={})
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_expense_metadata_dependency_rejected(self):
        invoice,a=self.new();category=ExpenseCategory.objects.create(business=self.business,name='Synthetic')
        ExpenseDetail.objects.create(invoice=invoice,category=category,title='Pending metadata')
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_reconciliation_dependency_rejected(self):
        invoice,a=self.new();run=ReconciliationRun.objects.create(user=self.user,return_period='2026-11')
        ReconciliationRecord.objects.create(run=run,invoice_number=invoice.invoice_number,invoice_date=invoice.invoice_date,supplier_gstin=invoice.supplier_gstin,matched_invoice=invoice,taxable_value=1000)
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_adjustment_dependency_rejected(self):
        from .models import InvoiceAdjustmentNote
        invoice,a=self.new();InvoiceAdjustmentNote.objects.create(user=self.user,business=self.business,original_invoice=invoice,party=self.vendor,kind='credit',direction='issued',number='CN-SYNTHETIC',year_start=date(2026,4,1),document_date=date(2026,11,20),reason='Synthetic note',taxable_amount=1,total_amount=1)
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_inflight_attempt_rejected_even_when_invoice_status_review(self):
        invoice,a=self.new();Attempt.objects.create(user=self.user,business=self.business,invoice=invoice,source_file=a.source_file.name,file_sha256=a.file_sha256,status='processing')
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_foreign_invoice_not_found(self):
        invoice,a=self.new();self.client.force_login(self.other)
        for route in ('invoice_change_type','invoice_restore'):
            self.assertEqual(self.client.get(reverse(route,args=[invoice.pk])).status_code,404)
            self.assertEqual(self.client.post(reverse(route,args=[invoice.pk]),{'confirm':'on'}).status_code,404)
    def test_wrong_business_attempt_rejected(self):
        invoice,a=self.new();Attempt.objects.filter(pk=a.pk).update(business=BusinessProfile.objects.get(user=self.other))
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_exact_source_conflict_rejected_even_across_types(self):
        invoice,a=self.new();other=Invoice.objects.create(user=self.user,invoice_number='DISTINCT',invoice_date=date(2026,11,20),invoice_type='expense',status='review',entry_mode='uploaded')
        Attempt.objects.create(user=self.user,business=self.business,invoice=other,status='review',file_sha256=a.file_sha256,source_file=a.source_file.name)
        with self.assertRaises(ValidationError):self.change(invoice,acknowledge_duplicates=True)
    def duplicate(self,kind='purchase',gst='27AAACS9002A1ZW',number='inv-0009/26-a'):
        return Invoice.objects.create(user=self.user,invoice_number=number,invoice_date=date(2026,11,20),invoice_type=kind,supplier_gstin=gst,total_amount=1180,status='review',supplier_name='Synthetic Supplier')
    def test_strong_cross_type_conflict_beyond_bounded_signal_examples(self):
        invoice,a=self.new()
        rows=[Invoice(user=self.user,invoice_type='expense',invoice_number=invoice.invoice_number,invoice_date=invoice.invoice_date,supplier_gstin='SYNTHETIC-'+str(i),status='review') for i in range(101)]
        rows[0].supplier_gstin=invoice.supplier_gstin
        Invoice.objects.bulk_create(rows)
        with self.assertRaises(ValidationError):self.change(invoice,acknowledge_duplicates=True)
    def test_target_strong_identity_duplicate(self):
        invoice,a=self.new();self.duplicate()
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_purchase_expense_cross_type_duplicate(self):
        invoice,a=self.new();self.duplicate('expense')
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_duplicate_normalization_tabs_case(self):
        invoice,a=self.new();self.duplicate('expense',number='\tINV-0009/26-A\n')
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_same_party_sale_and_purchase_can_be_distinct(self):
        invoice,a=self.new('purchase');Invoice.objects.create(user=self.user,invoice_number=invoice.invoice_number,invoice_date=invoice.invoice_date,invoice_type='sales',buyer_gstin=invoice.supplier_gstin,total_amount=1180,status='review')
        self.change(invoice,'expense');invoice.refresh_from_db();self.assertEqual(invoice.invoice_type,'expense')
    def test_potential_duplicate_requires_acknowledgement(self):
        invoice,a=self.new();self.duplicate(gst='')
        with self.assertRaises(ValidationError):self.change(invoice)
        self.change(invoice,acknowledge_duplicates=True)
    def test_preserves_source_rows_history_values_and_audits(self):
        invoice,a=self.new();before=Invoice.objects.values().get(pk=invoice.pk);lines=list(invoice.line_items.values());attempts=list(invoice.extraction_attempts.values());events=list(AuditEvent.objects.values());totals=self.totals()
        self.change(invoice);after=Invoice.objects.values().get(pk=invoice.pk)
        for key,value in before.items():
            if key not in {'invoice_type','status','updated_at'}:self.assertEqual(after[key],value,key)
        self.assertEqual(list(invoice.line_items.values()),lines);self.assertEqual(list(invoice.extraction_attempts.values()),attempts)
        self.assertEqual(list(AuditEvent.objects.filter(pk__in=[e['id'] for e in events]).values()),events)
        self.assertEqual(self.totals(),totals)
        with a.source_file.open('rb') as source:self.assertEqual(hashlib.sha256(source.read()).hexdigest(),a.file_sha256)
        event=activity(invoice).first();self.assertEqual(event.metadata['operation'],'change_type');self.assertEqual(event.metadata['attempt_id'],a.pk);self.assertEqual(event.metadata['source_sha256'],a.file_sha256);self.assertTrue(event.metadata['correction_reason']);self.assertEqual(event.metadata['previous_status'],'review');self.assertEqual(event.metadata['resulting_status'],'review');self.assertEqual(event.metadata['previous_invoice_type'],'sales');self.assertEqual(event.metadata['resulting_invoice_type'],'purchase')
    def test_lists_and_counterparty_render_after_change(self):
        invoice,a=self.new();self.change(invoice)
        self.assertContains(self.client.get(reverse('invoice_list'),{'type':'purchase'}),'Synthetic Supplier')
        response=self.client.get(reverse('invoice_detail',args=[invoice.pk]));self.assertContains(response,'Purchase Invoice');self.assertContains(response,'Change Invoice Type')
        self.assertContains(self.client.get(reverse('extraction_review',args=[a.pk])),'Change Invoice Type')
    def test_stale_repeated_submission_rejected(self):
        invoice,a=self.new();token=self.token(invoice);self.change(invoice,token=token)
        with self.assertRaises(ValidationError):self.change(invoice,'expense',token=token)
    def test_rollback_if_audit_fails(self):
        invoice,a=self.new();before=Invoice.objects.values().get(pk=invoice.pk)
        with patch('invoices.management_services.record_event',side_effect=RuntimeError('Synthetic failure')):
            with self.assertRaises(RuntimeError):self.change(invoice)
        self.assertEqual(Invoice.objects.values().get(pk=invoice.pk),before)
    def test_changed_or_missing_source_rejected(self):
        invoice,a=self.new();Path(a.source_file.path).write_bytes(b'changed')
        with self.assertRaises(ValidationError):self.change(invoice)
        Path(a.source_file.path).unlink()
        with self.assertRaises(ValidationError):self.change(invoice)
    def test_post_csrf_authentication_and_preview(self):
        invoice,a=self.new();url=reverse('invoice_change_type',args=[invoice.pk]);strict=Client(enforce_csrf_checks=True);strict.force_login(self.user)
        self.assertEqual(strict.post(url,{'target_type':'purchase','confirm':'on'}).status_code,403)
        self.client.logout();self.assertEqual(self.client.get(url).status_code,302);self.client.force_login(self.user)
        before=Invoice.objects.values().get(pk=invoice.pk);count=AuditEvent.objects.count();response=self.client.get(url);token=response.context['form'].initial['token']
        preview=self.client.post(url,{'token':token,'target_type':'purchase','preview':'1'})
        self.assertEqual(preview.status_code,200);self.assertEqual(Invoice.objects.values().get(pk=invoice.pk),before);self.assertEqual(AuditEvent.objects.count(),count)
        self.assertEqual(self.client.put(url).status_code,405)
    def test_stale_review_token_after_type_correction(self):
        invoice,a=self.new();old=self.payload(a);self.change(invoice)
        response=self.client.post(reverse('extraction_review',args=[a.pk]),old)
        self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists());self.assertContains(response,'expired or belongs')
    def test_warning_updated_after_correction_and_retry_uses_current_type(self):
        invoice,a=self.new();self.assertEqual(self.client.get(reverse('invoice_detail',args=[invoice.pk])).context['classification']['status'],'wrong_type')
        self.change(invoice);self.assertEqual(self.client.get(reverse('invoice_detail',args=[invoice.pk])).context['classification']['status'],'consistent')
        original=deepcopy(a.result)
        with patch('invoices.extraction.extract_invoice_data',return_value=fixtures.parsed()):
            response=self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'})
        self.assertEqual(response.status_code,302);invoice.refresh_from_db();a.refresh_from_db()
        self.assertEqual(invoice.invoice_type,'purchase');self.assertEqual(a.result,original)
        self.assertEqual(invoice.extraction_attempts.first().result['upload_invoice_type'],'purchase')
    def test_type_correction_does_not_bypass_hard_identity_confirmation(self):
        invoice,a=self.new('purchase');self.change(invoice,'sales')
        # The same valid purchase evidence remains wrong for sales; explicit checkboxes cannot override it.
        customer=BusinessParty.objects.create(business=self.business,party_type='customer',name='Synthetic Buyer',gstin=self.business.gstin)
        response=self.review(a,party=customer.pk)
        self.assertEqual(response.status_code,200);self.assertFalse(Review.objects.exists())
    def test_restore_preserves_cancellation_history_and_type(self):
        invoice,a=self.new();totals=self.totals();self.cancel(invoice);events=list(activity(invoice).values());attempts=list(invoice.extraction_attempts.values());kind=invoice.invoice_type
        self.restore(invoice);invoice.refresh_from_db();self.assertEqual(invoice.status,'review');self.assertEqual(invoice.invoice_type,kind);self.assertEqual(list(invoice.extraction_attempts.values()),attempts);self.assertEqual(self.totals(),totals)
        event=activity(invoice).first();self.assertEqual(event.metadata['operation'],'restore');self.assertEqual(event.metadata['cancellation_event_id'],events[0]['id']);self.assertTrue(activity(invoice).filter(pk=events[0]['id']).exists())
        self.change(invoice,'purchase')
    def test_cancelled_uploaded_expense_can_be_restored_without_metadata(self):
        invoice,a=self.new('expense');self.cancel(invoice);self.assertEqual(invoice.status,'cancelled')
        self.restore(invoice);invoice.refresh_from_db();self.assertEqual(invoice.status,'review');self.assertEqual(invoice.invoice_type,'expense')
    def test_expense_cancel_routes_reject_processing(self):
        invoice,a=self.new('expense');invoice.status='processing';invoice.save()
        for name in ('invoice_delete','expense_cancel'):
            self.client.post(reverse(name,args=[invoice.pk]),{'reason':'Cannot cancel in-flight source'})
            invoice.refresh_from_db();self.assertEqual(invoice.status,'processing')
    def test_restore_active_rejected(self):
        invoice,a=self.new()
        with self.assertRaises(ValidationError):self.restore(invoice)
    def test_restore_reason_and_checkbox(self):
        invoice,a=self.new();self.cancel(invoice)
        for changes in ({'reason':''},{'confirm':False}):
            with self.assertRaises(ValidationError):self.restore(invoice,**changes)
    def test_restore_already_recognized_audit_rejected(self):
        invoice,a=self.new();record_event(request=self.request(),business=self.business,action='cancel',module='invoices',obj=invoice,description='Synthetic recognized history',before={'status':'verified'},after={'status':'cancelled'});invoice.status='cancelled';invoice.save()
        with self.assertRaises(ValidationError):self.restore(invoice)
    def test_restore_finalized_and_confirmed_rejected(self):
        invoice,a=self.new();self.cancel(invoice);invoice.finalized_at=timezone.now();invoice.save()
        with self.assertRaises(ValidationError):self.restore(invoice)
        invoice.finalized_at=None;invoice.save();Review.objects.create(user=self.user,business=self.business,invoice=invoice,attempt=a,snapshot={})
        with self.assertRaises(ValidationError):self.restore(invoice)
    def test_restore_payment_and_itc_dependencies_rejected(self):
        invoice,a=self.new();self.cancel(invoice);ExpenseITCReview.objects.create(invoice=invoice,business=self.business,reviewer=self.user,status='pending',reason='Synthetic',fingerprint='a'*64,evidence={})
        with self.assertRaises(ValidationError):self.restore(invoice)
        ExpenseITCReview.objects.filter(invoice=invoice).delete();InvoicePayment.objects.create(user=self.user,invoice=invoice,party=self.vendor,payment_date=date(2026,11,20),amount=D('1'),payment_mode='cash',reference_id='SYNTHETIC')
        with self.assertRaises(ValidationError):self.restore(invoice)
    def test_restore_exact_duplicate_rejected(self):
        invoice,a=self.new();self.cancel(invoice);other=Invoice.objects.create(user=self.user,invoice_type='expense',invoice_number='SYNTHETIC-OTHER',invoice_date=invoice.invoice_date,status='review')
        Attempt.objects.create(user=self.user,business=self.business,invoice=other,status='review',file_sha256=a.file_sha256)
        with self.assertRaises(ValidationError):self.restore(invoice,acknowledge_duplicates=True)
    def test_restore_missing_and_modified_original(self):
        invoice,a=self.new();self.cancel(invoice);Path(a.source_file.path).write_bytes(b'changed')
        with self.assertRaises(ValidationError):self.restore(invoice)
        Path(a.source_file.path).unlink()
        with self.assertRaises(ValidationError):self.restore(invoice)
    def test_restore_repeated_and_no_second_record(self):
        invoice,a=self.new();self.cancel(invoice);count=Invoice.objects.count();token=self.token(invoice,'restore');self.restore(invoice,token=token)
        with self.assertRaises(ValidationError):self.restore(invoice,token=token)
        self.assertEqual(Invoice.objects.count(),count)
    def test_restoration_page_history_and_post(self):
        invoice,a=self.new();self.cancel(invoice);url=reverse('invoice_restore',args=[invoice.pk]);page=self.client.get(url);self.assertContains(page,'Retained cancellation history')
        token=page.context['form'].initial['token'];response=self.client.post(url,{'token':token,'reason':'Synthetic mistaken cancellation','confirm':'on'})
        self.assertEqual(response.status_code,302);self.assertContains(self.client.get(reverse('invoice_detail',args=[invoice.pk])),'Change Invoice Type')
    def test_source_and_operation_token_binding(self):
        invoice,a=self.new();token=self.token(invoice,'restore')
        with self.assertRaises(ValidationError):self.change(invoice,token=token)
    def test_end_to_end_correct_retry_confirm_and_verify_purchase(self):
        invoice,a=self.new('sales');self.change(invoice,'purchase')
        with patch('invoices.extraction.extract_invoice_data',return_value=fixtures.parsed()):
            self.assertEqual(self.client.post(reverse('extraction_retry',args=[a.pk]),{'confirm':'on'}).status_code,302)
        a=invoice.extraction_attempts.first();self.assertEqual(self.review(a).status_code,302)
        self.assertEqual(self.client.post(reverse('invoice_verify',args=[invoice.pk]),{'confirm':'on'}).status_code,302)
        invoice.refresh_from_db();self.assertEqual(invoice.status,'verified');self.assertEqual(invoice.invoice_type,'purchase');self.assertEqual(invoice.total_amount,D('1180'))
    def test_end_to_end_expense_reclassification_stays_unrecognized(self):
        invoice,a=self.new();before=self.totals();self.change(invoice,'expense');self.assertEqual(self.totals(),before);self.assertFalse(Review.objects.exists())
        self.assertContains(self.client.get(reverse('expense_list')),'INV-0009/26-A')
    def test_end_to_end_cancel_restore_correct_confirm(self):
        invoice,a=self.new();self.cancel(invoice);self.restore(invoice);self.change(invoice);self.assertEqual(self.review(a).status_code,302);invoice.refresh_from_db();self.assertEqual(invoice.status,'review');self.assertTrue(activity(invoice).filter(action='cancel').exists())

    def test_duplicate_action_post_preserves_entire_invoice_on_failure(self):
        invoice,a=self.new();self.duplicate('expense');before=Invoice.objects.values().get(pk=invoice.pk);count=AuditEvent.objects.count()
        url=reverse('invoice_change_type',args=[invoice.pk]);page=self.client.get(url)
        response=self.client.post(url,{'token':page.context['form'].initial['token'],'target_type':'purchase','reason':'Synthetic duplicate test','confirm':'on','acknowledge_duplicates':'on'})
        self.assertEqual(response.status_code,200);self.assertContains(response,'exact/strong duplicate')
        self.assertEqual(Invoice.objects.values().get(pk=invoice.pk),before);self.assertEqual(AuditEvent.objects.count(),count)
    def test_financial_action_routes_are_forbidden(self):
        invoice,a=self.new();invoice.status='verified';invoice.save()
        for name in ('invoice_change_type','invoice_restore'):
            self.assertEqual(self.client.post(reverse(name,args=[invoice.pk]),{'confirm':'on','reason':'Cannot override'}).status_code,403)
    def test_restore_keeps_type_even_if_target_field_is_injected(self):
        invoice,a=self.new();self.cancel(invoice);url=reverse('invoice_restore',args=[invoice.pk]);page=self.client.get(url)
        response=self.client.post(url,{'token':page.context['form'].initial['token'],'target_type':'expense','reason':'Synthetic restore only','confirm':'on'})
        self.assertEqual(response.status_code,302);invoice.refresh_from_db();self.assertEqual(invoice.invoice_type,'sales')
    def test_cancellation_during_processing_is_rejected(self):
        invoice,a=self.new();invoice.status='processing';invoice.save()
        self.client.post(reverse('invoice_delete',args=[invoice.pk]));invoice.refresh_from_db();self.assertEqual(invoice.status,'processing')


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import close_old_connections, OperationalError
from django.test import TransactionTestCase


class ManagementConcurrencyTests(TransactionTestCase):
    setUp=fixtures.ExtractionWorkflowTests.setUp
    upload=fixtures.ExtractionWorkflowTests.upload

    def race(self,operation):
        response,a=self.upload(kind='sales');invoice=a.invoice
        if operation=='restore':
            self.client.post(reverse('invoice_delete',args=[invoice.pk]));invoice.refresh_from_db()
        token=state_token(invoice,self.business,operation,a);barrier=Barrier(2)
        def run(index):
            close_old_connections()
            try:
                request=RequestFactory().post('/isolated-contention/');request.user=self.user
                barrier.wait(timeout=10)
                try:
                    manage_provisional(request=request,invoice_id=invoice.pk,operation=operation,token=token,
                                       target_type='purchase',reason='Synthetic contention',confirm=True)
                    return 'changed'
                except (ValidationError,OperationalError):return 'rejected/reload'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(run,[0,1]))
        self.assertLessEqual(results.count('changed'),1)
        if 'changed' not in results:
            request=RequestFactory().post('/explicit-test-reload/');request.user=self.user
            manage_provisional(request=request,invoice_id=invoice.pk,operation=operation,token=token,target_type='purchase',reason='Synthetic explicit reload after busy database',confirm=True)
        self.assertEqual(activity(invoice).filter(metadata__operation=operation).count(),1)
        invoice.refresh_from_db();self.assertEqual(invoice.status,'review')
        self.assertEqual(invoice.invoice_type,'purchase' if operation=='change_type' else 'sales')
    def test_concurrent_correction_commits_at_most_once(self):self.race('change_type')
    def test_concurrent_restoration_commits_at_most_once(self):self.race('restore')
