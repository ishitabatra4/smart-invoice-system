"""Separate connections exercise SQLite's writer contention without touching live data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import close_old_connections, OperationalError
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import BusinessProfile, BusinessParty
from expenses.models import ExpenseCategory, RecurringExpense, RecurringExpenseGeneration
from expenses.recurrence import generate_due_expenses
from .models import Invoice, InvoicePayment


class FinancialConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("concurrent-owner", password="secret")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Concurrent business", gstin="27AAACS9000A1ZY")
        self.party = BusinessParty.objects.create(business=self.business, name="Concurrent supplier", party_type="vendor")
        self.invoice = Invoice.objects.create(user=self.user, supplier_party=self.party, invoice_type="expense",
            invoice_number="CONCURRENT", invoice_date=date(2026, 10, 1), supplier_name=self.party.name,
            taxable_amount=Decimal("100"), total_amount=Decimal("100"), status="verified")

    def test_concurrent_payments_never_exceed_balance(self):
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.user)
        barrier = Barrier(2)
        def pay(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return clients[index].post(reverse("expense_payment", args=[self.invoice.pk]), {
                    "invoice": self.invoice.pk, "payment_date": "2026-10-02", "amount": "60",
                    "payment_mode": "cash", "reference_id": f"CONCURRENT-{index}",
                }).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(pay, [0, 1]))
        self.assertTrue(all(status in {302, 409} for status in statuses), statuses)
        self.assertEqual(InvoicePayment.objects.filter(invoice=self.invoice).count(), 1)
        self.assertEqual(self.invoice.payments.get().amount, Decimal("60"))
        self.assertEqual(self.invoice.total_amount, Decimal("100"))

    def test_concurrent_recurring_generation_is_retryable_and_unique(self):
        category = ExpenseCategory.objects.create(business=self.business, name="Recurring")
        schedule = RecurringExpense.objects.create(supplier_state_code="27", place_of_supply="27", supply_category="standard", tax_treatment="taxable", business=self.business, category=category, supplier=self.party,
            title="Monthly", taxable_amount=Decimal("50"), frequency="monthly", start_date=date(2026, 10, 1),
            next_due_date=date(2026, 10, 1))
        barrier = Barrier(2)
        def generate(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return len(generate_due_expenses(self.business, date(2026, 10, 1),allow_backfill=True))
            except OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                return "retry"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(generate, [0, 1]))
        generate_due_expenses(self.business, date(2026, 10, 1),allow_backfill=True)
        self.assertEqual(RecurringExpenseGeneration.objects.filter(recurring_expense=schedule).count(), 1)
        schedule.refresh_from_db()
        self.assertEqual(schedule.next_due_date, date(2026, 11, 1))

    def test_concurrent_sales_numbering_and_create_retries_are_unique(self):
        customer = BusinessParty.objects.create(business=self.business, name='Concurrent customer', party_type='customer')
        clients = [Client(), Client()]
        payloads = []
        for client in clients:
            client.force_login(self.user)
            response = client.get(reverse('invoice_create', args=['sales']))
            from django.core import signing
            import uuid
            token = signing.dumps({'user':self.user.pk, 'type':'sales', 'key':str(uuid.uuid4())}, salt='invoices.create')
            payloads.append({'creation_token':token, 'party':customer.pk, 'invoice_date':'2026-10-01',
                'supplier_state_code':'27', 'place_of_supply':'27', 'adjustment':'0',
                'items-TOTAL_FORMS':'1','items-INITIAL_FORMS':'0','items-MIN_NUM_FORMS':'1','items-MAX_NUM_FORMS':'100',
                'items-0-description':'Service','items-0-quantity':'1','items-0-rate':'100','items-0-unit':'Hour',
                'items-0-discount':'0','items-0-tax_rate':'18'})
        barrier = Barrier(2)
        def create(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return clients[index].post(reverse('invoice_create', args=['sales']), payloads[index]).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(create, [0, 1]))
        self.assertTrue(all(status in {302, 409} for status in statuses), statuses)
        for client, payload in zip(clients, payloads):
            self.assertEqual(client.post(reverse('invoice_create', args=['sales']), payload).status_code, 302)
        invoices = Invoice.objects.filter(user=self.user, invoice_type='sales')
        self.assertEqual(invoices.count(), 2)
        self.assertEqual(set(invoices.values_list('invoice_number', flat=True)), {'SI/2627/000001','SI/2627/000002'})
