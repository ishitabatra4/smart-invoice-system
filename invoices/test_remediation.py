"""Independent financial examples and report resource boundaries for the seven fixes."""
from datetime import date, datetime, timezone as utc
from decimal import Decimal as D
from io import BytesIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import load_workbook
from accounts.models import BusinessProfile, BusinessParty
from .models import Invoice, InvoicePayment
from .ledger import invoice_outstanding, party_ledger_entries
from .ledger_views import _invoice_rows
from . import test_adjustments as adjustment_fixtures
from analytics.services import build_analytics_context, _month_labels
from django.core.exceptions import ValidationError


class HistoricalBalanceTests(adjustment_fixtures.AdjustmentHelpers, TestCase):
    def setUp(self):self.setup_data()
    def pay(self, amount, day, invoice=None, reference=None):
        return InvoicePayment.objects.create(user=self.user,party=self.customer,invoice=invoice or self.invoice,amount=D(amount),payment_date=day,payment_mode='bank_transfer',reference_id=reference or str(day))
    def test_exact_reported_1000_case(self):
        inv=Invoice.objects.create(user=self.user,buyer_party=self.customer,invoice_type='sales',invoice_number='HISTORY-1000',invoice_date=date(2026,10,1),status='paid',taxable_amount='1000',total_amount='1000')
        inv.refresh_from_db()
        self.pay('1000',date(2026,11,10),inv)
        self.assertEqual(invoice_outstanding(inv,as_of=date(2026,10,31)),D('1000'))
        self.assertEqual(invoice_outstanding(inv,as_of=date(2026,11,30)),D('0'))
        self.assertEqual(invoice_outstanding(inv),D('0'))
    def test_multiple_partial_and_future_dated_payments(self):
        for amount,day in [('2000',date(2026,10,10)),('5000',date(2026,11,10)),('4800',date(2026,12,10))]:self.pay(amount,day)
        self.invoice.status='paid';self.invoice.save(update_fields=['status'])
        for day,expected in [(date(2026,10,9),'11800'),(date(2026,10,10),'9800'),(date(2026,10,31),'9800'),(date(2026,11,30),'4800'),(date(2026,12,10),'0')]:self.assertEqual(invoice_outstanding(self.invoice,as_of=day),D(expected))
    def test_credit_debit_and_cancellation_effective_dates(self):
        credit=self.create_note(document_date=date(2026,10,5));self.finalize_note(credit)
        debit=self.create_note('debit',rows=[{'original_line':self.source,'mode':'value','quantity':0,'amount':D('1000')}],document_date=date(2026,10,20));self.finalize_note(debit)
        self.cancel_note(credit,date(2026,11,15))
        for day,amount in [(date(2026,10,4),'11800'),(date(2026,10,5),'9440'),(date(2026,10,20),'10620'),(date(2026,11,15),'12980')]:self.assertEqual(invoice_outstanding(self.invoice,as_of=day),D(amount))
    def test_overpayment_never_becomes_negative_historical_receivable(self):
        self.pay('11800',date(2026,10,2));credit=self.create_note(document_date=date(2026,10,3));self.finalize_note(credit)
        self.assertEqual(invoice_outstanding(self.invoice,as_of=date(2026,10,31)),D('0'))
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.invoice).count(),1)
    def test_current_legacy_paid_marker_is_preserved_without_invented_payment_date(self):
        self.invoice.status='paid';self.invoice.save(update_fields=['status'])
        self.assertEqual(invoice_outstanding(self.invoice),D('0'));self.assertEqual(invoice_outstanding(self.invoice,as_of=date(2026,10,31)),D('11800'))
    @override_settings(TIME_ZONE='Asia/Kolkata')
    def test_known_cancellation_timestamp_uses_business_local_date(self):
        self.invoice.status='cancelled';self.invoice.cancelled_at=datetime(2026,11,1,20,0,tzinfo=utc.utc)
        self.assertEqual(invoice_outstanding(self.invoice,as_of=date(2026,11,1)),D('11800'));self.assertEqual(invoice_outstanding(self.invoice,as_of=date(2026,11,2)),D('0'))
    def test_dashboard_and_reporting_agree_with_independent_period_values(self):
        self.pay('11800',date(2026,11,10));self.invoice.status='paid';self.invoice.save(update_fields=['status'])
        expected=build_analytics_context(self.user,date(2026,10,1),date(2026,10,31),'custom')
        self.assertEqual(expected['totals']['receivable'],D('11800'))
        response=self.client.get(reverse('analytics_dashboard'),{'period':'custom','start_date':'2026-10-01','end_date':'2026-10-31'})
        self.assertEqual(response.context['totals']['receivable'],D('11800'))
        from .adjustment_reporting import reporting_summary
        self.assertEqual(reporting_summary(self.user,date(2026,10,1),date(2026,10,31))['totals']['receivable'],D('11800'))
        self.assertEqual(party_ledger_entries(self.user,self.customer)[0]['receivable_balance'],D('11800'))
    def test_historical_calculation_does_not_rewrite_invoice_payment_or_audit(self):
        from audit.models import AuditEvent
        self.pay('11800',date(2026,11,10));self.invoice.status='paid';self.invoice.save(update_fields=['status'])
        before=(list(Invoice.objects.values()),list(InvoicePayment.objects.values()),AuditEvent.objects.count())
        invoice_outstanding(self.invoice,as_of=date(2026,10,31))
        self.assertEqual(before,(list(Invoice.objects.values()),list(InvoicePayment.objects.values()),AuditEvent.objects.count()))

    def test_mixed_role_adjustments_and_partial_payment_keep_each_direction(self):
        self.customer.party_type='both';self.customer.save(update_fields=['party_type'])
        Invoice.objects.create(user=self.user,supplier_party=self.customer,invoice_type='purchase',invoice_number='MIX-ADJ-P',invoice_date=date(2026,10,1),status='verified',total_amount='80',taxable_amount='80')
        credit=self.create_note(document_date=date(2026,10,5));self.finalize_note(credit);self.pay('1000',date(2026,10,10))
        final=party_ledger_entries(self.user,self.customer)[-1]
        self.assertEqual(final['receivable_balance'],D('8440'));self.assertEqual(final['payable_balance'],D('80'));self.assertEqual(final['balance'],D('8360'))
        self.cancel_note(credit,date(2026,11,15));final=party_ledger_entries(self.user,self.customer)[-1]
        self.assertEqual(final['receivable_balance'],D('10800'));self.assertEqual(final['payable_balance'],D('80'))
    def test_note_scope_and_related_row_caps_precede_analytics(self):
        credit=self.create_note(document_date=date(2026,10,5));self.finalize_note(credit)
        with patch('invoices.adjustment_views.MAX_REPORT_NOTES',0),patch('invoices.adjustment_reporting.reporting_summary') as summary:
            self.assertEqual(self.client.get(reverse('adjustment_report')).status_code,400);summary.assert_not_called()
        self.pay('100',date(2026,10,10))
        with patch('invoices.adjustment_views.MAX_REPORT_RELATED_ROWS',0),patch('invoices.adjustment_reporting.reporting_summary') as summary:
            self.assertEqual(self.client.get(reverse('adjustment_report')).status_code,400);summary.assert_not_called()


class MixedLedgerTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('mixed-ledger-owner',password='synthetic')
        self.business=BusinessProfile.objects.create(user=self.user,business_name='Synthetic mixed ledger')
        self.party=BusinessParty.objects.create(business=self.business,name='Synthetic both roles',party_type='both')
        self.sale=Invoice.objects.create(user=self.user,buyer_party=self.party,invoice_type='sales',invoice_number='MIX-S',invoice_date=date(2026,10,1),total_amount='100',taxable_amount='100',status='verified')
        self.purchase=Invoice.objects.create(user=self.user,supplier_party=self.party,invoice_type='purchase',invoice_number='MIX-P',invoice_date=date(2026,10,2),total_amount='80',taxable_amount='80',status='verified')
        self.sale.refresh_from_db();self.purchase.refresh_from_db()
        self.client.force_login(self.user)
    def test_two_balances_and_signed_comparison_without_settlement(self):
        entries=party_ledger_entries(self.user,self.party);self.assertEqual(entries[-1]['receivable_balance'],D('100'));self.assertEqual(entries[-1]['payable_balance'],D('80'));self.assertEqual(entries[-1]['balance'],D('20'))
        self.assertEqual(invoice_outstanding(self.sale),D('100'));self.assertEqual(invoice_outstanding(self.purchase),D('80'));self.assertEqual(InvoicePayment.objects.count(),0)
    def test_partial_payments_keep_directions_separate(self):
        for inv,amount,ref in [(self.sale,'30','MIX-IN'),(self.purchase,'20','MIX-OUT')]:InvoicePayment.objects.create(user=self.user,party=self.party,invoice=inv,payment_date=date(2026,10,3),amount=amount,payment_mode='cash',reference_id=ref)
        entry=party_ledger_entries(self.user,self.party)[-1];self.assertEqual(entry['receivable_balance'],D('70'));self.assertEqual(entry['payable_balance'],D('60'));self.assertEqual(entry['balance'],D('10'))
    def test_html_and_excel_have_same_two_balance_columns(self):
        response=self.client.get(reverse('party_detail',args=[self.party.pk]));self.assertContains(response,'Receivable balance');self.assertContains(response,'Payable balance');self.assertNotContains(response,'Running Balance')
        response=self.client.get(reverse('party_ledger_export',args=[self.party.pk]));book=load_workbook(BytesIO(response.content),read_only=True);rows=list(book.active.values)
        self.assertEqual(rows[0][6:8],('Receivable balance','Payable balance'));self.assertEqual(rows[-1][6:8],(100,80))
    def test_list_balances_are_separate(self):
        row=self.client.get(reverse('party_list')).context['party_rows'][0];self.assertEqual(row['receivable'],D('100'));self.assertEqual(row['payable'],D('80'))
    def test_ledger_queries_are_batched(self):
        for i in range(30):Invoice.objects.create(user=self.user,buyer_party=self.party,invoice_type='sales',invoice_number=f'COUNT-{i}',invoice_date=date(2026,10,1),total_amount='1',taxable_amount='1',status='verified')
        with CaptureQueriesContext(connection) as queries:rows=_invoice_rows(self.user,[self.party])
        self.assertEqual(len(rows),32);self.assertLessEqual(len(queries),4);self.assertEqual(sum(row['outstanding'] for row in rows),D('210'))


class AdjustmentReportBoundsTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('report-bounds-owner');BusinessProfile.objects.create(user=self.user,business_name='Synthetic bounds');self.client.force_login(self.user)
    def test_extreme_range_rejected_before_analytics(self):
        for params in [{'start':'0001-01-01','end':'9999-12-31'},{'start':'2020-01-01','end':'2035-01-01'},{'start':'bad'},{'start':'2026-11-02','end':'2026-11-01'}]:
            with patch('invoices.adjustment_reporting.reporting_summary') as summary:
                self.assertEqual(self.client.get(reverse('adjustment_report'),params).status_code,400);summary.assert_not_called()
    def test_month_bucket_guard_before_allocation(self):
        with self.assertRaises(ValidationError):_month_labels(date(1,1,1),date(9999,12,31))
        self.assertEqual(len(_month_labels(date(2026,1,1),date(2026,12,31))[0]),12)
    def test_ten_year_boundary_and_default_report(self):
        from datetime import timedelta
        start=date(2020,1,1);end=start+timedelta(days=3660)
        self.assertEqual(self.client.get(reverse('adjustment_report'),{'start':str(start),'end':str(end)}).status_code,200)
        self.assertEqual(self.client.get(reverse('adjustment_report'),{'start':str(start),'end':str(end+timedelta(days=1))}).status_code,400)
        self.assertEqual(self.client.get(reverse('adjustment_report')).status_code,200)
    def test_source_cap_checked_before_analytics(self):
        Invoice.objects.bulk_create([Invoice(user=self.user,invoice_type='sales',invoice_number=f'LIMIT-{i}',invoice_date=date(2026,11,1),status='verified',total_amount='1') for i in range(2001)])
        with patch('invoices.adjustment_reporting.reporting_summary') as summary:
            response=self.client.get(reverse('adjustment_report'),{'start':'2026-11-01','end':'2026-11-30'});self.assertEqual(response.status_code,400);summary.assert_not_called()

    def test_export_row_cap_precedes_workbook_generation(self):
        with patch('invoices.adjustment_views.MAX_EXPORT_ROWS',4),patch('openpyxl.Workbook') as workbook:
            self.assertEqual(self.client.get(reverse('adjustment_report'),{'export':'xlsx'}).status_code,400);workbook.assert_not_called()
        with patch('invoices.adjustment_views.MAX_EXPORT_ROWS',5):
            self.assertEqual(self.client.get(reverse('adjustment_report'),{'export':'xlsx'}).status_code,200)
