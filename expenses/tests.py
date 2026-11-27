from invoices.test_files import valid_pdf
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from accounts.models import BusinessParty, BusinessProfile
from audit.models import AuditEvent
from invoices.models import Invoice, InvoicePayment

from .models import ExpenseCategory, ExpenseDetail, RecurringExpense, RecurringExpenseGeneration, private_expense_storage
from .recurrence import generate_due_expenses
from .reporting import export_report, report_rows
from analytics.services import build_analytics_context


class ExpenseWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="expense-user", password="secret")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Expense Test Co")
        self.supplier = BusinessParty.objects.create(business=self.business, party_type="vendor", name="Test Supplier")
        self.category = ExpenseCategory.objects.create(business=self.business, name="Rent", is_default=True)
        self.test_attachment_names = []
        self.client.login(username="expense-user", password="secret")

    def tearDown(self):
        for name in self.test_attachment_names:
            private_expense_storage.delete(name)

    def expense_payload(self, **updates):
        payload = {
            "supplier_state_code":"27", "place_of_supply":"27", "supply_category":"standard", "tax_treatment":"taxable",
            "title": "Office rent", "category": str(self.category.pk), "description": "Monthly rent",
            "expense_date": date.today().isoformat(), "due_date": "", "reference_number": "RENT-001",
            "supplier_party": str(self.supplier.pk), "supplier_name": "", "supplier_gstin": "",
            "supplier_email": "", "supplier_phone": "", "amount": "1000.00", "gst_rate": "18",
            "gst_inclusive": "exclusive", "discount": "0.00", "payment_status": "unpaid",
            "amount_paid": "0.00", "payment_date": "", "payment_mode": "", "payment_reference": "", "notes": "Lease",
        }
        payload.update(updates)
        return payload

    def create_expense(self, **updates):
        reference = updates.get("reference_number", "RENT-001")
        response = self.client.post(reverse("expense_add"), self.expense_payload(**updates))
        self.assertEqual(response.status_code, 302, msg=str(response.context["form"].errors) if response.context else "")
        return Invoice.objects.get(user=self.user, invoice_type="expense", invoice_number=reference)

    def record_payment(self, invoice, amount, ref):
        return self.client.post(reverse("expense_payment", args=[invoice.pk]), {
            "invoice": str(invoice.pk), "payment_date": date.today().isoformat(),
            "amount": str(amount), "payment_mode": "bank_transfer", "reference_id": ref, "notes": "Test payment",
        })

    def test_recognized_financial_edits_are_rejected_after_payment(self):
        invoice = self.create_expense()
        self.assertEqual(invoice.total_amount, Decimal("1180.00"))
        self.assertEqual(invoice.line_items.count(), 1)
        self.record_payment(invoice, Decimal("500.00"), "TX-500")
        response = self.client.post(reverse("expense_edit", args=[invoice.pk]), self.expense_payload(amount="400.00"))
        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount, Decimal("1180.00"))
        self.assertTrue(AuditEvent.objects.filter(object_type="invoices.invoice", object_id=str(invoice.pk), action="create").exists())
        response = self.client.post(reverse("expense_edit", args=[invoice.pk]), self.expense_payload(amount="600.00"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response,"Recognized financial values are immutable")
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount, Decimal("1180.00"))
        self.assertEqual(invoice.payments.count(), 1)
        self.assertEqual(invoice.total_amount - sum(invoice.payments.values_list("amount", flat=True)), Decimal("680.00"))
        self.assertFalse(AuditEvent.objects.filter(object_type="invoices.invoice", object_id=str(invoice.pk), action="update").exists())

    def test_partial_full_and_overpayment(self):
        invoice = self.create_expense()
        self.record_payment(invoice, Decimal("500.00"), "TX-500")
        invoice.refresh_from_db()
        self.assertEqual(sum(invoice.payments.values_list("amount", flat=True)), Decimal("500.00"))
        response = self.record_payment(invoice, Decimal("680.00"), "TX-680")
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "paid")
        before = invoice.payments.count()
        self.record_payment(invoice, Decimal("1.00"), "TX-OVER")
        self.assertEqual(invoice.payments.count(), before)

    def test_delete_soft_cancels_without_payments_and_refuses_with_payments(self):
        invoice = self.create_expense()
        self.client.post(reverse("expense_delete", args=[invoice.pk]),{"reason":"Duplicate booking; replace explicitly"})
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "cancelled")
        self.assertTrue(AuditEvent.objects.filter(object_type="invoices.invoice", object_id=str(invoice.pk), action="cancel").exists())

        other = self.create_expense(reference_number="RENT-002")
        InvoicePayment.objects.create(
            user=self.user, party=self.supplier, invoice=other, payment_date=date.today(),
            amount=Decimal("10.00"), payment_mode="cash", reference_id="KEEP-001",
        )
        self.client.post(reverse("expense_delete", args=[other.pk]))
        other.refresh_from_db()
        self.assertNotEqual(other.status, "cancelled")
        self.assertEqual(other.payments.count(), 1)

    def test_private_attachment_replace_remove_and_owner_check(self):
        pdf = SimpleUploadedFile("receipt.pdf", valid_pdf(), content_type="application/pdf")
        payload = self.expense_payload()
        payload["attachment"] = pdf
        invoice = self.client.post(reverse("expense_add"), payload)
        self.assertEqual(invoice.status_code, 302)
        expense = Invoice.objects.get(user=self.user, invoice_type="expense", invoice_number="RENT-001")
        detail = expense.expense_detail
        old_name = detail.attachment.name
        self.test_attachment_names.append(old_name)
        self.assertIn("private_expense_files", str(detail.attachment.path))
        self.assertEqual(self.client.get(reverse("expense_attachment", args=[expense.pk])).status_code, 200)
        self.assertFalse(str(detail.attachment.path).startswith(str(settings.MEDIA_ROOT)))

        replacement = SimpleUploadedFile("replacement.pdf", valid_pdf(), content_type="application/pdf")
        payload = self.expense_payload()
        payload["attachment"] = replacement
        self.client.post(reverse("expense_edit", args=[expense.pk]), payload)
        detail.refresh_from_db()
        self.test_attachment_names.append(detail.attachment.name)
        self.assertNotEqual(detail.attachment.name, old_name)
        self.client.post(reverse("expense_edit", args=[expense.pk]), self.expense_payload(remove_attachment="on"))
        detail.refresh_from_db()
        self.assertFalse(detail.attachment)

        other_user = get_user_model().objects.create_user(username="other", password="secret")
        BusinessProfile.objects.create(user=other_user, business_name="Other Co")
        self.client.logout()
        self.client.login(username="other", password="secret")
        self.assertEqual(self.client.get(reverse("expense_attachment", args=[expense.pk])).status_code, 404)

    def test_invalid_attachment_content_is_rejected(self):
        invalid = SimpleUploadedFile("receipt.pdf", b"this is not a PDF", content_type="application/pdf")
        payload = self.expense_payload()
        payload["attachment"] = invalid
        response = self.client.post(reverse("expense_add"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Invoice.objects.filter(user=self.user, invoice_number="RENT-001").exists())

    def test_detail_filters_and_analytics_count_expense_once(self):
        invoice = self.create_expense()
        detail = self.client.get(reverse("expense_detail", args=[invoice.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Office rent")
        listing = self.client.get(reverse("expense_list"), {
            "category": self.category.pk, "supplier": self.supplier.pk,
            "status": "unpaid", "start_date": date.today().isoformat(), "end_date": date.today().isoformat(),
        })
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "RENT-001")
        context = build_analytics_context(self.user, date.today(), date.today(), "custom")
        self.assertEqual(context["totals"]["expenses"], Decimal("1180.00"))

    def test_overdue_status_filter_and_report(self):
        yesterday = date.today() - timedelta(days=1)
        invoice = self.create_expense(expense_date=yesterday.isoformat(), due_date=yesterday.isoformat())
        listing = self.client.get(reverse("expense_list"), {"status": "overdue"})
        self.assertContains(listing, invoice.invoice_number)
        self.assertContains(listing, "Overdue")
        form, rows, _ = report_rows(self.user, self.business, {"report_type": "payment_status", "status": "overdue"})
        self.assertTrue(form.is_valid())
        self.assertEqual([row["invoice"].pk for row in rows], [invoice.pk])

    def test_recurring_generation_is_idempotent_and_unpaid(self):
        today = date.today()
        schedule = RecurringExpense.objects.create(supplier_state_code="27", place_of_supply="27", supply_category="standard", tax_treatment="taxable", 
            business=self.business, title="Internet", category=self.category, supplier=self.supplier,
            taxable_amount=Decimal("1000.00"), gst_rate=Decimal("18.00"), frequency="yearly",
            start_date=today, next_due_date=today, schedule_day=today.day,
        )
        created = generate_due_expenses(self.business, today)
        again = generate_due_expenses(self.business, today)
        self.assertEqual(len(created), 1)
        self.assertEqual(again, [])
        self.assertEqual(RecurringExpenseGeneration.objects.filter(recurring_expense=schedule).count(), 1)
        self.assertEqual(created[0].total_amount, Decimal("1180.00"))
        self.assertFalse(created[0].payments.exists())

    def test_non_duplicate_generation_integrity_error_does_not_skip_period(self):
        today = date.today()
        schedule = RecurringExpense.objects.create(supplier_state_code="27", place_of_supply="27", supply_category="standard", tax_treatment="taxable", 
            business=self.business, title="Internet", category=self.category, supplier=self.supplier,
            taxable_amount=Decimal("1000.00"), gst_rate=Decimal("18.00"), frequency="monthly",
            start_date=today, next_due_date=today, schedule_day=today.day,
        )
        with patch("expenses.recurrence._save_generated_expense", side_effect=IntegrityError("unexpected constraint")):
            with self.assertRaises(IntegrityError):
                generate_due_expenses(self.business, today)
        schedule.refresh_from_db()
        self.assertEqual(schedule.next_due_date, today)
        self.assertFalse(schedule.generations.exists())

    def test_recurring_form_and_pause_resume_cancel(self):
        today = date.today().isoformat()
        response = self.client.post(reverse("recurring_expense_add"), {
            "supplier_state_code":"27", "place_of_supply":"27", "supply_category":"standard", "tax_treatment":"taxable",
            "title": "Phone plan", "category": str(self.category.pk), "supplier": str(self.supplier.pk),
            "taxable_amount": "200", "gst_rate": "18", "gst_inclusive": "",
            "frequency": "monthly", "start_date": today, "end_date": "",
        })
        self.assertEqual(response.status_code, 302)
        schedule = RecurringExpense.objects.get(title="Phone plan", business=self.business)
        self.client.post(reverse("recurring_expense_action", args=[schedule.pk, "pause"]))
        schedule.refresh_from_db()
        self.assertFalse(schedule.is_active)
        self.client.post(reverse("recurring_expense_action", args=[schedule.pk, "resume"]))
        schedule.refresh_from_db()
        self.assertTrue(schedule.is_active)
        self.client.post(reverse("recurring_expense_generate"))
        self.assertTrue(RecurringExpenseGeneration.objects.filter(recurring_expense=schedule).exists())
        self.client.post(reverse("recurring_expense_action", args=[schedule.pk, "cancel"]))
        schedule.refresh_from_db()
        self.assertTrue(schedule.is_cancelled)
        self.assertFalse(schedule.is_active)

    def test_exports_and_report_filter_are_scoped(self):
        self.create_expense()
        form, rows, totals = report_rows(self.user, self.business, {"report_type": "complete"})
        self.assertTrue(form.is_valid())
        self.assertEqual(len(rows), 1)
        self.assertEqual(totals["total"], Decimal("1180.00"))
        for fmt, expected in (("csv", "text/csv"), ("xlsx", "spreadsheetml"), ("pdf", "application/pdf")):
            response = export_report(rows, "complete", totals, fmt)
            self.assertIn(expected, response["Content-Type"])
            if fmt == "pdf":
                self.assertTrue(response.content.startswith(b"%PDF-1.4"))

    def test_expense_and_recurring_pages_do_not_show_other_users_data(self):
        invoice = self.create_expense()
        other = get_user_model().objects.create_user(username="other-user", password="secret")
        BusinessProfile.objects.create(user=other, business_name="Other Test Co")
        self.client.logout()
        self.client.login(username="other-user", password="secret")
        self.assertEqual(self.client.get(reverse("expense_detail", args=[invoice.pk])).status_code, 404)
