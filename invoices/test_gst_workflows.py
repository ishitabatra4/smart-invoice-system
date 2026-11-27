from datetime import date
from decimal import Decimal
from django.test import TestCase,Client
from django.urls import reverse
from accounts.models import BusinessParty
from audit.models import AuditEvent
from analytics.services import build_analytics_context
from expenses.models import RecurringExpense,ExpenseCategory
from expenses.recurrence import generate_due_expenses
from .models import Invoice,InvoiceLineItem
from . import test_manual as fixtures

D=Decimal


class GSTWorkflowTests(TestCase):
    setUp=fixtures.ManualInvoiceTests.setUp
    payload=fixtures.ManualInvoiceTests.payload
    create=fixtures.ManualInvoiceTests.create
    finalize=fixtures.ManualInvoiceTests.finalize

    def test_server_preview_matches_saved_inclusive_discounted_invoice(self):
        data=self.payload(pricing_mode='inclusive',invoice_discount_type='percent',invoice_discount_value='10',
            **{'items-0-rate':'1180','items-0-quantity':'1','items-0-discount_type':'percent','items-0-discount':'10'})
        preview=self.client.post(reverse('invoice_calculate',args=['sales']),data)
        self.assertEqual(preview.status_code,200,preview.content)
        self.assertFalse(Invoice.objects.exists())
        self.client.post(reverse('invoice_create',args=['sales']),data)
        invoice=Invoice.objects.get(user=self.user)
        values=preview.json()
        self.assertEqual(invoice.total_amount,D(values['total_amount']))
        self.assertEqual(invoice.taxable_amount,D('810'))
        self.assertEqual(invoice.total_amount,D('955.80'))
        self.assertEqual(invoice.calculation_version,1)
        self.assertEqual(invoice.line_items.get().discount_value,D('10'))
        self.assertEqual(invoice.line_items.get().discount,D('118'))
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state,'finalized')

    def test_client_totals_cannot_override_authoritative_values(self):
        invoice=self.create(total_amount='0.01',cgst_amount='0',sgst_amount='0',**{'items-0-taxable_amount':'0.01','items-0-total_amount':'0.01'})
        self.assertEqual(invoice.total_amount,D('224.20'))
        self.assertEqual(invoice.line_items.get().total_amount,D('224.20'))

    def test_edit_recalculates_and_audits_discount_pricing_and_treatment(self):
        invoice=self.create()
        data=self.payload(pricing_mode='inclusive',place_of_supply='29',invoice_discount_type='percent',invoice_discount_value='10')
        response=self.client.post(reverse('invoice_draft_edit',args=[invoice.pk]),data)
        self.assertEqual(response.status_code,302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.pricing_mode,'inclusive')
        self.assertEqual(invoice.igst_amount,invoice.total_tax)
        event=AuditEvent.objects.filter(action='update',object_id=str(invoice.pk),module='invoices').get()
        self.assertEqual(event.before['pricing_mode'],'exclusive')
        self.assertEqual(event.after['pricing_mode'],'inclusive')
        self.assertEqual(event.after['invoice_discount_value'],'10')

    def test_finalized_components_discounts_and_pricing_cannot_be_changed(self):
        invoice=self.create(pricing_mode='inclusive')
        self.finalize(invoice)
        invoice.refresh_from_db()
        original=(invoice.total_amount,invoice.pricing_mode,invoice.total_discount_amount)
        self.client.post(reverse('invoice_draft_edit',args=[invoice.pk]),self.payload(invoice_discount_value='100'))
        invoice.refresh_from_db()
        self.assertEqual((invoice.total_amount,invoice.pricing_mode,invoice.total_discount_amount),original)
        self.assertEqual(self.client.post(reverse('invoice_calculate',args=['sales']),{**self.payload(),'invoice_id':invoice.pk}).status_code,404)

    def test_finalization_rejects_modified_saved_line_components(self):
        invoice=self.create()
        InvoiceLineItem.objects.filter(invoice=invoice).update(cgst_amount=D('0'))
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state,'draft')
        self.assertEqual(invoice.line_items.get().cgst_amount,D('0'))

    def test_special_supply_and_missing_place_rejected_without_saving(self):
        for changes in ({'supply_category':'sez'},{'supply_category':'reverse_charge'},{'place_of_supply':''}):
            response=self.client.post(reverse('invoice_create',args=['sales']),self.payload(**changes))
            self.assertEqual(response.status_code,200)
            self.assertFalse(Invoice.objects.exists())

    def test_preview_has_owner_csrf_and_post_controls(self):
        invoice=self.create()
        data=self.payload(invoice_id=invoice.pk)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse('invoice_calculate',args=['sales']),data).status_code,404)
        self.assertEqual(self.client.get(reverse('invoice_calculate',args=['sales'])).status_code,405)
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        self.assertEqual(client.post(reverse('invoice_calculate',args=['sales']),data).status_code,403)
        self.client.logout()
        self.assertEqual(self.client.post(reverse('invoice_calculate',args=['sales']),data).status_code,302)

    def test_historical_uploaded_values_and_classification_stay_unknown(self):
        legacy=Invoice.objects.create(user=self.user,invoice_type='sales',invoice_number='HISTORICAL',invoice_date=date(2026,10,1),supplier_name='Original',taxable_amount=D('100'),cgst_amount=D('9'),sgst_amount=D('9'),total_amount=D('118'),status='verified')
        self.create()
        self.client.get(reverse('invoice_detail',args=[legacy.pk]))
        legacy.refresh_from_db()
        self.assertEqual(legacy.total_amount,D('118'))
        self.assertIsNone(legacy.calculation_version)
        self.assertIsNone(legacy.pricing_mode)
        self.assertIsNone(legacy.treatment_breakdown)
        context=build_analytics_context(self.user,date(2026,10,1),date(2026,10,31),'custom')
        self.assertEqual(context['treatment_summary']['sales']['unclassified'],D('100'))

    def test_analytics_preserves_distinct_tax_treatments_without_double_counting(self):
        for treatment in ('taxable','exempt','nil_rated','non_gst'):
            invoice=self.create(**{'items-0-tax_rate':'0','items-0-tax_treatment':treatment})
            self.finalize(invoice)
        context=build_analytics_context(self.user,date(2026,10,1),date(2026,10,31),'custom')
        self.assertEqual(context['totals']['sales'],D('760'))
        self.assertEqual(context['totals']['output_gst'],D('0'))
        self.assertEqual(context['treatment_summary']['sales'],{'taxable':D('190'),'exempt':D('190'),'nil_rated':D('190'),'non_gst':D('190'),'unclassified':D('0')})

    def test_rates_are_selectable_and_arbitrary_rates_fail_validation(self):
        response=self.client.post(reverse('invoice_create',args=['sales']),self.payload(**{'items-0-tax_rate':'7'}))
        self.assertEqual(response.status_code,200)
        self.assertFalse(Invoice.objects.exists())
        response=self.client.get(reverse('invoice_create',args=['sales']))
        self.assertContains(response,'GST inclusive')
        self.assertContains(response,'Tax treatment')
        self.assertContains(response,'Invoice discount')
        self.assertContains(response,'data-gst-preview-url')

    def test_recurring_missing_supply_is_reviewed_not_silently_guessed(self):
        category=ExpenseCategory.objects.create(business=self.business,name='Recurring')
        schedule=RecurringExpense.objects.create(business=self.business,category=category,supplier=self.vendor,title='Old schedule',taxable_amount=D('100'),gst_rate=D('18'),frequency='monthly',start_date=date(2026,10,1),next_due_date=date(2026,10,1))
        self.assertTrue(schedule.gst_review_required)
        with self.assertLogs('expenses.recurrence',level='WARNING'):
            self.assertEqual(generate_due_expenses(self.business,date(2026,10,1),allow_backfill=True),[])
        schedule.refresh_from_db()
        self.assertEqual(schedule.next_due_date,date(2026,10,1))
        self.assertFalse(schedule.generations.exists())

    def test_expense_preview_uses_same_inclusive_engine_without_writes(self):
        response=self.client.post(reverse('expense_calculate'),{'amount':'1180','discount':'0','gst_rate':'18','gst_inclusive':'inclusive','supplier_state_code':'27','place_of_supply':'29','supply_category':'standard','tax_treatment':'taxable'})
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['taxable_amount'],'1000.00')
        self.assertEqual(response.json()['igst_amount'],'180.00')
        self.assertEqual(response.json()['total_amount'],'1180.00')
        self.assertFalse(Invoice.objects.exists())

    def test_earlier_draft_requires_explicit_review_without_rewriting_amounts(self):
        invoice=self.create()
        Invoice.objects.filter(pk=invoice.pk).update(calculation_version=None,pricing_mode=None)
        original=invoice.total_amount
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state,'draft')
        self.assertEqual(invoice.total_amount,original)
        self.assertIsNone(invoice.pricing_mode)
        response=self.client.post(reverse('invoice_draft_edit',args=[invoice.pk]),self.payload())
        self.assertEqual(response.status_code,302)
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state,'finalized')

    def test_uploaded_component_warnings_clear_after_authorized_correction(self):
        from .views import _invoice_validation_issues
        invoice=Invoice.objects.create(user=self.user,invoice_type='purchase',invoice_number='REVIEW',invoice_date=date(2026,10,1),supplier_name='Source',taxable_amount=D('100'),cgst_amount=D('9'),sgst_amount=D('9'),igst_amount=D('18'),total_amount=D('136'),status='review')
        InvoiceLineItem.objects.create(invoice=invoice,description='Source line',quantity=1,rate=D('100'),tax_rate=D('18'),taxable_amount=D('100'),tax_amount=D('18'),total_amount=D('118'))
        warnings=_invoice_validation_issues(invoice)
        self.assertTrue(any('CGST/SGST and IGST' in warning for warning in warnings))
        invoice.igst_amount=D('0');invoice.total_amount=D('118')
        self.assertEqual(_invoice_validation_issues(invoice,previous_issues=warnings),[])

    def test_discounted_invoice_payments_and_cash_flow_use_saved_totals(self):
        from .ledger import invoice_outstanding
        invoice=self.create(invoice_discount_type='percent',invoice_discount_value='10')
        self.finalize(invoice)
        response=self.client.post(reverse('party_detail',args=[self.customer.pk]),{'invoice':invoice.pk,'payment_date':'2026-10-02','amount':'100','payment_mode':'cash','reference_id':'GST-PARTIAL'})
        self.assertEqual(response.status_code,302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount,D('201.78'))
        self.assertEqual(invoice_outstanding(invoice),D('101.78'))
        context=build_analytics_context(self.user,date(2026,10,1),date(2026,10,31),'custom')
        self.assertEqual(context['totals']['sales'],D('201.78'))
        self.assertEqual(context['totals']['payments_received'],D('100'))
        self.assertEqual(context['net_cash_flow'],D('100'))

    def test_new_recurring_inclusive_expense_uses_central_components(self):
        category=ExpenseCategory.objects.create(business=self.business,name='Inclusive recurring')
        schedule=RecurringExpense.objects.create(business=self.business,category=category,supplier=self.vendor,title='Inclusive',taxable_amount=D('1180'),gst_rate=D('18'),gst_inclusive=True,frequency='monthly',start_date=date(2026,10,1),next_due_date=date(2026,10,1),supplier_state_code='27',place_of_supply='29',supply_category='standard',tax_treatment='taxable')
        invoices=generate_due_expenses(self.business,date(2026,10,1),allow_backfill=True)
        self.assertEqual(len(invoices),1)
        invoice=invoices[0]
        self.assertEqual(invoice.total_amount,D('1180'))
        self.assertEqual(invoice.taxable_amount,D('1000'))
        self.assertEqual(invoice.igst_amount,D('180'))
        self.assertFalse(invoice.line_items.get().taxable_amount_mismatch)
        self.assertEqual(generate_due_expenses(self.business,date(2026,10,1),allow_backfill=True),[])
