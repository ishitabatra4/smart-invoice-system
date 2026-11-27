from datetime import date
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from accounts.models import BusinessProfile, BusinessParty
from analytics.services import build_analytics_context
from audit.models import AuditEvent
from .models import Invoice, InvoiceNumberSequence, InvoicePayment


class ManualInvoiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('manual-owner', 'manual@example.com', 'secret')
        self.business = BusinessProfile.objects.create(user=self.user, business_name='Manual business', gstin='27ABCDE1234F1Z0')
        self.customer = BusinessParty.objects.create(business=self.business, name='Customer', party_type='customer')
        self.vendor = BusinessParty.objects.create(business=self.business, name='Supplier', party_type='vendor', gstin='27ABCDE1234F1Z0')
        self.other = get_user_model().objects.create_user('manual-other', password='secret')
        self.other_business = BusinessProfile.objects.create(user=self.other, business_name='Other business', gstin='27AAACS9000A1ZY')
        self.other_customer = BusinessParty.objects.create(business=self.other_business, name='Other customer', party_type='customer')
        self.client.force_login(self.user)

    def payload(self, kind='sales', token=None, **updates):
        if token is None:
            response = self.client.get(reverse('invoice_create', args=[kind]))
            self.assertEqual(response.status_code, 200)
            token = response.context['form'].initial['creation_token']
        data = {'creation_token': token, 'invoice_number': 'SUP-001' if kind == 'purchase' else '',
            'party': (self.customer if kind == 'sales' else self.vendor).pk, 'invoice_date': '2026-10-01', 'due_date': '2026-10-31',
            'supplier_state_code': '27', 'place_of_supply': '27', 'adjustment': '0', 'payment_terms': 'Net 30', 'notes': 'Thank you', 'terms_conditions': 'Pay by due date',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0', 'items-MIN_NUM_FORMS': '1', 'items-MAX_NUM_FORMS': '100',
            'items-0-description': 'Consulting', 'items-0-hsn_sac_code': '9983', 'items-0-quantity': '2', 'items-0-unit': 'Hours',
            'items-0-rate': '100', 'items-0-discount': '10', 'items-0-tax_rate': '18'}
        data.update(updates)
        return data

    def create(self, kind='sales', **updates):
        response = self.client.post(reverse('invoice_create', args=[kind]), self.payload(kind, **updates))
        self.assertEqual(response.status_code, 302, str(response.context['form'].errors) if response.context else '')
        return Invoice.objects.get(pk=int(response.url.rstrip('/').rsplit('/', 1)[1]))

    def finalize(self, invoice):
        return self.client.post(reverse('invoice_finalize', args=[invoice.pk]))

    def test_sales_draft_number_decimal_discount_and_preview(self):
        invoice = self.create()
        self.assertEqual(invoice.invoice_number, 'SI/2627/000001')
        self.assertEqual(invoice.entry_mode, 'manual')
        self.assertEqual(invoice.document_state, 'draft')
        self.assertEqual(invoice.status, 'draft')
        self.assertEqual(invoice.taxable_amount, Decimal('190'))
        self.assertEqual(invoice.cgst_amount, Decimal('17.10'))
        self.assertEqual(invoice.sgst_amount, Decimal('17.10'))
        self.assertEqual(invoice.total_amount, Decimal('224.20'))
        self.assertFalse(invoice.line_items.get().taxable_amount_mismatch)
        self.assertContains(self.client.get(reverse('invoice_preview', args=[invoice.pk])), 'DRAFT')
        self.assertContains(self.client.get(reverse('invoice_detail', args=[invoice.pk])), 'Finalize invoice')
        context = build_analytics_context(self.user, date(2026,10,1), date(2026,10,31), 'custom')
        self.assertEqual(context['totals']['sales'], Decimal('0'))

    def test_multiple_lines_interstate_and_rounding(self):
        data = self.payload(**{'items-TOTAL_FORMS':'2', 'place_of_supply':'29', 'adjustment':'-0.20',
            'items-1-description':'Hosting', 'items-1-hsn_sac_code':'9983', 'items-1-quantity':'1', 'items-1-unit':'Month',
            'items-1-rate':'50', 'items-1-discount':'0', 'items-1-tax_rate':'18'})
        self.client.post(reverse('invoice_create', args=['sales']), data)
        invoice = Invoice.objects.get(user=self.user)
        self.assertEqual(invoice.line_items.count(), 2)
        self.assertEqual(invoice.igst_amount, Decimal('43.20'))
        self.assertEqual(invoice.cgst_amount, Decimal('0'))
        self.assertEqual(invoice.total_amount, Decimal('283'))

    def test_finalization_locks_financial_edits_and_posts_once(self):
        invoice = self.create()
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'verified')
        self.assertEqual(invoice.document_state, 'finalized')
        self.assertIsNotNone(invoice.finalized_at)
        self.client.post(reverse('invoice_draft_edit', args=[invoice.pk]), self.payload(**{'items-0-rate':'1'}))
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount, Decimal('224.20'))
        self.finalize(invoice)
        self.assertEqual(AuditEvent.objects.filter(object_id=str(invoice.pk), action='verify', module='invoices').count(), 1)
        context = build_analytics_context(self.user, date(2026,10,1), date(2026,10,31), 'custom')
        self.assertEqual(context['totals']['sales'], Decimal('224.20'))

    def test_number_sequence_scopes_and_existing_number_collision(self):
        legacy = Invoice.objects.create(user=self.user, invoice_type='sales', invoice_number='si/2627/000001', invoice_date=date(2026,10,1), supplier_name='Legacy', total_amount=Decimal('500'), status='verified')
        first = self.create()
        second = self.create()
        self.assertEqual(first.invoice_number, 'SI/2627/000002')
        self.assertEqual(second.invoice_number, 'SI/2627/000003')
        legacy.refresh_from_db()
        self.assertEqual(legacy.total_amount, Decimal('500'))
        self.assertEqual(legacy.invoice_number, 'si/2627/000001')
        self.assertEqual(legacy.entry_mode, 'legacy')
        self.assertEqual(self.create(invoice_date='2027-04-01', due_date='2027-04-30').invoice_number, 'SI/2728/000001')

    def test_repeated_create_post_does_not_create_duplicate_or_consume_number(self):
        data = self.payload()
        first = self.client.post(reverse('invoice_create', args=['sales']), data)
        repeated = self.client.post(reverse('invoice_create', args=['sales']), data)
        self.assertEqual(first.url, repeated.url)
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 1)
        self.assertEqual(InvoiceNumberSequence.objects.get().next_value, 2)
        self.assertEqual(AuditEvent.objects.filter(module='invoices', action='create').count(), 1)

    def test_draft_edit_preserves_identifier_and_updates_lines(self):
        invoice = self.create()
        data = self.payload(**{'items-0-discount':'20', 'invoice_number':'ATTACKED'})
        self.assertEqual(self.client.post(reverse('invoice_draft_edit', args=[invoice.pk]), data).status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.invoice_number, 'SI/2627/000001')
        self.assertEqual(invoice.taxable_amount, Decimal('180'))
        self.assertEqual(invoice.line_items.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action='update', object_id=str(invoice.pk)).exists())

    def test_purchase_supplier_duplicate_and_manual_verification(self):
        invoice = self.create('purchase')
        response = self.client.post(reverse('invoice_create', args=['purchase']), self.payload('purchase'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 1)
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'verified')
        self.assertEqual(invoice.invoice_number, 'SUP-001')
        self.assertEqual(invoice.supplier_party_id, self.vendor.pk)
        self.assertEqual(invoice.buyer_name, self.business.business_name)

    def test_new_party_created_in_same_transaction(self):
        data = self.payload(party='', add_new_party='on', **{'newparty-name':'Brand new customer', 'newparty-party_type':'customer'})
        response = self.client.post(reverse('invoice_create', args=['sales']), data)
        self.assertEqual(response.status_code, 302, str(response.context['party_form'].errors) if response.context else '')
        invoice = Invoice.objects.get(user=self.user)
        self.assertEqual(invoice.buyer_party.name, 'Brand new customer')
        self.assertEqual(invoice.buyer_party.business_id, self.business.pk)
        self.assertEqual(invoice.buyer_party.party_type, 'customer')

    def test_invalid_amounts_rates_dates_and_party_do_not_save(self):
        for updates in [{'items-0-discount':'201'}, {'items-0-quantity':'0'}, {'items-0-tax_rate':'101'},
                        {'due_date':'2026-09-30'}, {'party':self.other_customer.pk}, {'supplier_state_code':'29'},
                        {'place_of_supply':'99'}, {'adjustment':'2'}, {'items-0-rate':'NaN'}, {'items-0-quantity':'999999999','items-0-rate':'999999999999'},
                        {'items-TOTAL_FORMS':'101'}]:
            with self.subTest(updates=updates):
                response = self.client.post(reverse('invoice_create', args=['sales']), self.payload(**updates))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Invoice.objects.filter(user=self.user).exists())

    def test_tampered_or_foreign_creation_tokens_rejected(self):
        data = self.payload()
        data['creation_token'] += 'tamper'
        self.client.post(reverse('invoice_create', args=['sales']), data)
        self.assertFalse(Invoice.objects.filter(user=self.user).exists())
        data = self.payload()
        self.client.force_login(self.other)
        data.update(party=self.other_customer.pk)
        self.client.post(reverse('invoice_create', args=['sales']), data)
        self.assertFalse(Invoice.objects.filter(user=self.other).exists())

    def test_cross_business_preview_edit_finalize_and_cancellation_are_denied(self):
        invoice = self.create()
        self.client.force_login(self.other)
        for name in ('invoice_preview', 'invoice_detail', 'invoice_draft_edit'):
            self.assertEqual(self.client.get(reverse(name, args=[invoice.pk])).status_code, 404)
        for name in ('invoice_finalize', 'invoice_delete'):
            self.assertEqual(self.client.post(reverse(name, args=[invoice.pk])).status_code, 404)

    def test_cancel_retains_amounts_and_number_and_does_not_reuse_number(self):
        invoice = self.create()
        self.finalize(invoice)
        self.client.post(reverse('invoice_delete', args=[invoice.pk]))
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state, 'cancelled')
        self.assertEqual(invoice.total_amount, Decimal('224.20'))
        self.assertEqual(self.create().invoice_number, 'SI/2627/000002')
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state, 'cancelled')

    def test_finalized_snapshot_stays_unchanged_after_party_profile_edits(self):
        invoice = self.create()
        self.finalize(invoice)
        self.customer.name = 'Changed customer'
        self.customer.save()
        self.business.business_name = 'Changed business'
        self.business.save()
        response = self.client.get(reverse('invoice_preview', args=[invoice.pk]))
        self.assertContains(response, 'Manual business')
        self.assertContains(response, 'Customer')
        self.assertNotContains(response, 'Changed customer')

    def test_finalization_rejects_changed_party_and_inconsistent_amounts(self):
        invoice = self.create()
        self.customer.name = 'Changed'
        self.customer.save()
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state, 'draft')
        self.customer.name = 'Customer'
        self.customer.save()
        Invoice.objects.filter(pk=invoice.pk).update(total_amount=Decimal('1'))
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state, 'draft')
        self.assertEqual(invoice.total_amount, Decimal('1'))

    def test_payment_preserves_issued_state_and_prevents_cancellation(self):
        invoice = self.create()
        self.finalize(invoice)
        response = self.client.post(reverse('party_detail', args=[self.customer.pk]), {'invoice':invoice.pk, 'payment_date':'2026-10-02', 'amount':'224.20', 'payment_mode':'cash', 'reference_id':'SALE-PAID'})
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.document_state, 'finalized')
        self.client.post(reverse('invoice_delete', args=[invoice.pk]))
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state, 'finalized')
        self.assertEqual(invoice.payments.count(), 1)

    def test_finalization_requires_post_and_csrf(self):
        invoice = self.create()
        self.assertEqual(self.client.get(reverse('invoice_finalize', args=[invoice.pk])).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse('invoice_finalize', args=[invoice.pk])).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('invoice_create', args=['sales'])).status_code, 302)

    def test_half_paise_tax_rounding_is_consistent_in_new_manual_lines(self):
        invoice = self.create(**{'items-0-quantity':'1', 'items-0-rate':'0.10', 'items-0-discount':'0', 'items-0-tax_rate':'5'})
        self.assertEqual(invoice.line_items.get().tax_amount, Decimal('0.01'))
        self.assertFalse(invoice.line_items.get().tax_amount_mismatch)
        self.finalize(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.document_state, 'finalized')

    def test_repeated_new_party_submission_reuses_original_invoice(self):
        data = self.payload(party='', add_new_party='on', **{'newparty-name':'New registered customer', 'newparty-gstin':'29ABCDE9876G1ZY'})
        first = self.client.post(reverse('invoice_create', args=['sales']), data)
        self.assertEqual(first.status_code, 302)
        second = self.client.post(reverse('invoice_create', args=['sales']), data)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(first.url, second.url)
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 1)
        self.assertEqual(BusinessParty.objects.filter(business=self.business, gstin='29ABCDE9876G1ZY').count(), 1)

    def test_financial_year_change_is_rejected_without_altering_existing_draft(self):
        invoice = self.create()
        response = self.client.post(reverse('invoice_draft_edit', args=[invoice.pk]), self.payload(invoice_date='2027-04-01', due_date='2027-04-30'))
        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.invoice_date, date(2026,10,1))
        self.assertEqual(invoice.invoice_number, 'SI/2627/000001')
        self.assertEqual(invoice.line_items.count(), 1)

    def test_manual_purchase_draft_cannot_be_used_in_reconciliation(self):
        from taxation.models import ReconciliationRun, ReconciliationRecord
        invoice = self.create('purchase')
        run = ReconciliationRun.objects.create(user=self.user, return_period='2026-10')
        record = ReconciliationRecord.objects.create(run=run, supplier_gstin=self.vendor.gstin, invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date, taxable_value=invoice.taxable_amount, total_tax=invoice.total_tax)
        response = self.client.get(reverse('reconciliation_run', args=[run.pk]))
        self.assertNotIn(invoice.pk, [candidate.pk for candidate in response.context['candidate_invoices']])
        self.assertEqual(self.client.post(reverse('reconciliation_match', args=[run.pk, record.pk]), {'invoice_id':invoice.pk}).status_code, 404)
        self.finalize(invoice)
        self.assertEqual(self.client.post(reverse('reconciliation_match', args=[run.pk, record.pk]), {'invoice_id':invoice.pk}).status_code, 302)
