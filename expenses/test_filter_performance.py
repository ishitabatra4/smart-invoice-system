"""Batched exact filtering still verifies receipt bytes on every request."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import connection
from django.db.models.fields.files import FieldFile
from django.test import TestCase,RequestFactory,override_settings
from django.test.utils import CaptureQueriesContext
from accounts.models import BusinessProfile
from invoices.models import Invoice,InvoiceLineItem
from invoices.test_files import valid_pdf
from .models import ExpenseDetail,ExpenseCategory,ExpenseITCReview
from .services import source_evidence,fingerprint,itc_state
from .views import _expense_list_context,_expense_queryset


class ExpenseFilterPerformanceTests(TestCase):
    def setUp(self):
        self.media=tempfile.TemporaryDirectory();self.addCleanup(self.media.cleanup);self.settings_override=override_settings(MEDIA_ROOT=self.media.name);self.settings_override.enable();self.addCleanup(self.settings_override.disable)
        self.user=get_user_model().objects.create_user('filter-performance-owner');self.business=BusinessProfile.objects.create(user=self.user,business_name='Synthetic filter checks',gstin='27ABCDE1234F1Z0');self.category=ExpenseCategory.objects.create(business=self.business,name='Synthetic costs')
    def request(self,**params):
        req=RequestFactory().get('/expenses/list/',params);req.user=self.user;req.session={};req._messages=FallbackStorage(req);return req
    def invoice(self,index,review=True):
        inv=Invoice.objects.create(user=self.user,invoice_type='expense',invoice_number=f'FILTER-{index}',invoice_date=date(2026,11,1),status='verified',taxable_amount='100',cgst_amount='9',sgst_amount='9',total_amount='118',supplier_gstin='27PQRST5678L1Z0',buyer_gstin=self.business.gstin,calculation_version=1,pricing_mode='exclusive',supplier_state_code='27',place_of_supply='27',supply_category='standard')
        inv.source_file.save('synthetic.pdf',ContentFile(valid_pdf()),save=True)
        ExpenseDetail.objects.create(invoice=inv,category=self.category,title='Synthetic expense')
        InvoiceLineItem.objects.create(invoice=inv,description='Synthetic',quantity='1',rate='100',tax_rate='18',taxable_amount='100',cgst_amount='9',sgst_amount='9',tax_amount='18',total_amount='118',tax_treatment='taxable')
        inv.refresh_from_db()
        if review:
            evidence=source_evidence(inv);ExpenseITCReview.objects.create(invoice=inv,business=self.business,reviewer=self.user,status='potential',reason='Synthetic fixture only',fingerprint=fingerprint(evidence),evidence=evidence)
        return inv
    def test_hundred_reviewed_expenses_use_constant_query_count_and_exact_results(self):
        for index in range(100):self.invoice(index)
        with CaptureQueriesContext(connection) as queries:rows,filters,page=_expense_list_context(self.request(itc='potential'),self.business)
        self.assertLessEqual(len(queries),10);self.assertEqual(len(rows),25);self.assertEqual(page.paginator.count,100)
        self.assertTrue(all(row['outstanding']==Decimal('118') and row['itc']['status']=='potential' for row in rows))
    def test_receipt_changed_between_requests_is_stale(self):
        inv=self.invoice(1);self.assertEqual(_expense_list_context(self.request(itc='potential'),self.business)[2].paginator.count,1)
        with inv.source_file.storage.open(inv.source_file.name,'wb') as file:file.write(b'changed synthetic document')
        rows,filters,page=_expense_list_context(self.request(itc='pending'),self.business);self.assertEqual(page.paginator.count,1);self.assertIn('Source changed',rows[0]['itc']['reason'])
    def test_batch_fingerprint_equals_existing_uncached_fingerprint(self):
        inv=self.invoice(1);expected=source_evidence(inv);loaded=_expense_queryset(self.user).get(pk=inv.pk);context={'business':self.business,'available':True,'documents':{},'bytes_read':0}
        self.assertEqual(source_evidence(loaded,context=context),expected)
    def test_oversized_scope_rejected_without_evaluating_rows(self):
        Invoice.objects.bulk_create([Invoice(user=self.user,invoice_type='expense',invoice_number=f'SCOPE-{i}',invoice_date=date(2026,11,1),status='verified',total_amount='1') for i in range(1001)])
        req=self.request(status='unpaid')
        with patch('expenses.views._expense_row') as row:rows,filters,page=_expense_list_context(req,self.business);row.assert_not_called()
        self.assertEqual(rows,[]);self.assertTrue(any('1,000' in str(m) for m in req._messages))
    def test_read_budget_failure_does_not_label_valid_itc_as_ineligible(self):
        inv=self.invoice(1);context={'business':self.business,'available':True,'documents':{},'bytes_read':64*1024*1024}
        with self.assertRaises(ValidationError):source_evidence(inv,context=context)
        self.assertEqual(ExpenseITCReview.objects.get(invoice=inv).status,'potential')
    def test_latest_review_wins_and_missing_receipt_is_stale(self):
        inv=self.invoice(1);evidence=source_evidence(inv);ExpenseITCReview.objects.create(invoice=inv,business=self.business,reviewer=self.user,status='ineligible',reason='Synthetic reviewed restriction',fingerprint=fingerprint(evidence),evidence=evidence)
        self.assertEqual(_expense_list_context(self.request(itc='ineligible'),self.business)[2].paginator.count,1)
        inv.source_file.storage.delete(inv.source_file.name)
        self.assertEqual(_expense_list_context(self.request(itc='pending'),self.business)[2].paginator.count,1)
    def test_foreign_business_context_rejected(self):
        inv=self.invoice(1);other=get_user_model().objects.create_user('foreign-filter-owner');business=BusinessProfile.objects.create(user=other,business_name='Other')
        with self.assertRaises(ValidationError):source_evidence(inv,context={'business':business,'available':True,'documents':{},'bytes_read':0})
