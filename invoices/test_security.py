from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook
from PIL import Image

from accounts.models import BusinessProfile, BusinessParty
from analytics.services import build_analytics_context, dashboard_date_range
from audit.models import AuditEvent
from audit.services import safe_data
from expenses.forms import ExpenseForm
from expenses.models import ExpenseCategory, ExpenseDetail, RecurringExpense
from expenses.recurrence import generate_due_expenses
from expenses.reporting import report_rows
from taxation.models import ReconciliationRecord, ReconciliationRun, ReconciliationAction
from .file_security import validate_document
from .models import Invoice, InvoiceBatch, InvoicePayment, InvoiceLineItem
from .test_files import valid_pdf


class SecurityIsolationTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = override_settings(MEDIA_ROOT=self.directory.name)
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_user("isolation-owner", password="secret")
        self.other = get_user_model().objects.create_user("isolation-other", password="secret")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Owner")
        self.foreign_business = BusinessProfile.objects.create(user=self.other, business_name="Foreign")
        self.party = BusinessParty.objects.create(business=self.business, name="Owner supplier", party_type="vendor")
        self.foreign_party = BusinessParty.objects.create(business=self.foreign_business, name="Foreign supplier", party_type="vendor")
        self.invoice = self.make_invoice(self.user, self.party, "OWN")
        self.foreign_invoice = self.make_invoice(self.other, self.foreign_party, "FOREIGN")
        self.foreign_expense = self.make_invoice(self.other, self.foreign_party, "FOREIGN-EXP", "expense")
        self.category = ExpenseCategory.objects.create(business=self.business, name="Owner category")
        self.foreign_category = ExpenseCategory.objects.create(business=self.foreign_business, name="Foreign category")
        ExpenseDetail.objects.create(invoice=self.foreign_expense, category=self.foreign_category, title="Foreign expense")
        self.batch = InvoiceBatch.objects.create(user=self.other, invoice_type="purchase")
        self.run = ReconciliationRun.objects.create(user=self.user, return_period="2026-10")
        self.foreign_run = ReconciliationRun.objects.create(user=self.other, return_period="2026-10")
        self.record = self.make_record("OWN")
        self.event = AuditEvent.objects.create(business=self.foreign_business, user=self.other, action="create", module="invoices", description="FOREIGN PRIVATE EVENT")
        self.client.force_login(self.user)

    def make_invoice(self, user, party, number, kind="purchase"):
        return Invoice.objects.create(user=user, supplier_party=party, invoice_type=kind, invoice_number=number,
                                      invoice_date=date(2026, 10, 1), supplier_name=party.name,
                                      taxable_amount=Decimal("100"), total_amount=Decimal("100"), status="verified")

    def make_record(self, number):
        return ReconciliationRecord.objects.create(run=self.run, supplier_gstin="27ABCDE1234F1Z5", invoice_number=number,
                                                   invoice_date=date(2026, 10, 1), taxable_value=Decimal("100"))

    def payment_data(self, invoice=None, amount="60", reference="PAY-ONE"):
        return {"invoice": (invoice or self.invoice).pk, "payment_date": "2026-10-02", "amount": amount,
                "payment_mode": "cash", "reference_id": reference}

    def test_foreign_record_gets_and_posts_return_404(self):
        routes = [("invoice_detail", [self.foreign_invoice.pk]), ("invoice_edit", [self.foreign_invoice.pk]),
                  ("invoice_source", [self.foreign_invoice.pk]), ("expense_detail", [self.foreign_expense.pk]),
                  ("expense_edit", [self.foreign_expense.pk]), ("expense_attachment", [self.foreign_expense.pk]),
                  ("party_detail", [self.foreign_party.pk]), ("party_edit", [self.foreign_party.pk]),
                  ("party_ledger_export", [self.foreign_party.pk]), ("invoice_batch_detail", [self.batch.pk]),
                  ("invoice_batch_export", [self.batch.pk]), ("reconciliation_run", [self.foreign_run.pk]),
                  ("reconciliation_export", [self.foreign_run.pk]), ("reconciliation_source", [self.foreign_run.pk]),
                  ("audit_event_detail", [self.event.pk])]
        for name, args in routes:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404)
        for name, args in [("invoice_delete", [self.foreign_invoice.pk]), ("invoice_verify", [self.foreign_invoice.pk]),
                           ("expense_cancel", [self.foreign_expense.pk]), ("expense_payment", [self.foreign_expense.pk])]:
            self.assertEqual(self.client.post(reverse(name, args=args), self.payment_data(self.foreign_expense)).status_code, 404)
        self.foreign_invoice.refresh_from_db()
        self.assertEqual(self.foreign_invoice.status, "verified")

    def test_forged_payment_invoice_and_party_do_not_create_payments(self):
        self.client.post(reverse("party_detail", args=[self.party.pk]), self.payment_data(self.foreign_invoice))
        self.client.post(reverse("party_detail", args=[self.foreign_party.pk]), self.payment_data())
        self.assertFalse(InvoicePayment.objects.exists())

    def test_duplicate_overpayment_nonpositive_and_normalized_references(self):
        url = reverse("party_detail", args=[self.party.pk])
        self.client.post(url, self.payment_data())
        self.client.post(url, self.payment_data(amount="10", reference=" pay-one "))
        self.client.post(url, self.payment_data(amount="41", reference="OVER"))
        self.client.post(url, self.payment_data(amount="0", reference="ZERO"))
        self.client.post(url, self.payment_data(amount="-1", reference="NEGATIVE"))
        self.assertEqual(self.invoice.payments.count(), 1)
        self.assertEqual(self.invoice.payments.get().amount, Decimal("60"))
        self.assertEqual(AuditEvent.objects.filter(action="payment").count(), 1)

    def test_related_objects_are_validated_for_owner(self):
        self.invoice.supplier_party = self.foreign_party
        with self.assertRaises(ValidationError):
            self.invoice.full_clean()
        self.invoice.supplier_party = self.party
        self.invoice.batch = self.batch
        with self.assertRaises(ValidationError):
            self.invoice.full_clean()
        self.record.matched_invoice = self.foreign_invoice
        with self.assertRaises(ValidationError):
            self.record.full_clean()
        form = ExpenseForm({"category": self.foreign_category.pk, "supplier_party": self.foreign_party.pk}, business=self.business)
        self.assertFalse(form.is_valid())
        self.assertIn("category", form.errors)
        self.assertIn("supplier_party", form.errors)

    def test_private_invoice_download_existing_paths_and_missing_files(self):
        self.invoice.source_file.save("legacy.pdf", SimpleUploadedFile("legacy.pdf", valid_pdf()))
        response = self.client.get(reverse("invoice_source", args=[self.invoice.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
        self.assertEqual(self.client.get(self.invoice.source_file.url).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("invoice_source", args=[self.invoice.pk])).status_code, 404)
        self.client.force_login(self.user)
        self.invoice.source_file = "missing.pdf"
        self.invoice.save()
        self.assertEqual(self.client.get(reverse("invoice_source", args=[self.invoice.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("invoice_source", args=[self.invoice.pk])).status_code, 302)

    def test_reconciliation_cross_user_invalid_id_and_duplicate_matches(self):
        url = reverse("reconciliation_match", args=[self.run.pk, self.record.pk])
        for value in [self.foreign_invoice.pk, "not-a-number", "9" * 100]:
            self.assertEqual(self.client.post(url, {"invoice_id": value}).status_code, 404)
        self.client.post(url, {"invoice_id": self.invoice.pk})
        second = self.make_record("SECOND")
        response = self.client.post(reverse("reconciliation_match", args=[self.run.pk, second.pk]), {"invoice_id": self.invoice.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.run.records.filter(matched_invoice=self.invoice).count(), 1)
        self.client.post(reverse("reconciliation_unmatch", args=[self.run.pk, self.record.pk]))
        self.assertEqual(ReconciliationAction.objects.filter(record=self.record).count(), 2)
        self.assertEqual(self.client.post(reverse("reconciliation_match", args=[self.foreign_run.pk, self.record.pk]), {"invoice_id": self.invoice.pk}).status_code, 404)

    def test_audit_exports_and_analytics_are_isolated(self):
        own = AuditEvent.objects.create(business=self.business, user=self.user, action="create", module="invoices", description="OWNER EVENT")
        response = self.client.get(reverse("audit_events"))
        self.assertContains(response, "OWNER EVENT")
        self.assertNotContains(response, "FOREIGN PRIVATE EVENT")
        response = self.client.get(reverse("audit_export", args=["csv"]))
        content = b"".join(response.streaming_content)
        self.assertNotIn(b"FOREIGN PRIVATE EVENT", content)
        context = build_analytics_context(self.user, date(2026, 10, 1), date(2026, 10, 31), "custom")
        self.assertEqual(context["totals"]["purchases"], Decimal("100"))
        self.assertEqual(context["totals"]["expenses"], Decimal("0"))
        self.assertNotIn("Foreign supplier", str(context["chart_data"]))
        _, rows, _ = report_rows(self.user, self.business, {})
        self.assertFalse(rows)
        self.assertEqual(safe_data({"smtp_password": "secret", "credentials": "secret", "token": "secret", "amount": "10"}), {"amount": "10"})

    def test_csrf_and_state_changing_get_requests(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        routes = [("invoice_delete", [self.invoice.pk]), ("invoice_verify", [self.invoice.pk]),
                  ("expense_payment", [self.foreign_expense.pk]), ("reconciliation_match", [self.run.pk, self.record.pk])]
        for name, args in routes:
            self.assertEqual(client.post(reverse(name, args=args), {}).status_code, 403)
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 405)

    def test_invoice_cancellation_retains_record_and_document(self):
        self.invoice.source_file.save("original.pdf", SimpleUploadedFile("original.pdf", valid_pdf()))
        original_path = self.invoice.source_file.path
        self.client.post(reverse("invoice_delete", args=[self.invoice.pk]))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "cancelled")
        self.assertTrue(Path(original_path).exists())
        self.assertEqual(AuditEvent.objects.filter(action="cancel", object_id=str(self.invoice.pk)).count(), 1)
        self.client.post(reverse("invoice_delete", args=[self.invoice.pk]))
        self.assertEqual(AuditEvent.objects.filter(action="cancel", object_id=str(self.invoice.pk)).count(), 1)

    def test_file_content_size_page_pixel_and_ocr_limits(self):
        for name, body in [("fake.pdf", b"%PDF-1.7 fake"), ("fake.png", b"not image"), ("fake.exe", valid_pdf())]:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate_document(SimpleUploadedFile(name, body))
        with override_settings(DOCUMENT_MAX_PAGES=0), self.assertRaises(ValidationError):
            validate_document(SimpleUploadedFile("valid.pdf", valid_pdf()))
        image = BytesIO()
        Image.new("RGB", (10, 10)).save(image, "PNG")
        with override_settings(DOCUMENT_MAX_PIXELS=99), self.assertRaises(ValidationError):
            validate_document(SimpleUploadedFile("image.png", image.getvalue()))
        with self.assertRaises(ValidationError):
            validate_document(SimpleUploadedFile("huge.pdf", b"x" * (10 * 1024 * 1024 + 1)))
        with patch("invoices.views.extract_invoice_data") as extract:
            self.client.post(reverse("invoice_upload"), {"invoice_type": "purchase", "source_file": SimpleUploadedFile("fake.pdf", b"not a pdf")})
            extract.assert_not_called()
        from .extraction import _ocr_image
        with patch("invoices.extraction.pytesseract.image_to_string", return_value="text") as ocr:
            _ocr_image(Image.new("RGB", (100, 100)))
            self.assertEqual(ocr.call_args.kwargs["timeout"], 30)

    def test_spreadsheet_formula_injection_is_escaped(self):
        self.invoice.invoice_number = "=1+1"
        self.invoice.save()
        response = self.client.get(reverse("party_ledger_export", args=[self.party.pk]))
        workbook = load_workbook(BytesIO(response.content))
        self.assertEqual(workbook.active["B2"].value, "'=1+1")
        self.assertEqual(workbook.active["B2"].data_type, "s")

    def test_cash_reports_filter_payment_dates(self):
        expense = self.make_invoice(self.user, self.party, "OLD-EXP", "expense")
        expense.invoice_date = date(2026, 9, 1)
        expense.save()
        ExpenseDetail.objects.create(invoice=expense, category=self.category, title="Old bill")
        InvoicePayment.objects.create(user=self.user, party=self.party, invoice=expense, payment_date=date(2026, 10, 5), amount=Decimal("20"), payment_mode="cash", reference_id="OCT")
        form, rows, totals = report_rows(self.user, self.business, {"report_type": "payments", "start_date": "2026-10-01", "end_date": "2026-10-31"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(rows), 1)
        self.assertEqual(totals["payment_amount"], Decimal("20"))
        context = build_analytics_context(self.user, date(2026, 10, 1), date(2026, 10, 31), "custom")
        self.assertEqual(context["totals"]["payments_made"], Decimal("20"))
        self.assertEqual(context["totals"]["expenses"], Decimal("0"))
        self.assertIsNotNone(dashboard_date_range({"period": "custom", "start_date": "0001-01-01", "end_date": "9999-12-31"})[3])

    def test_cancelled_schedule_cannot_generate_even_if_active_flag_is_stale(self):
        schedule = RecurringExpense.objects.create(supplier_state_code="27", place_of_supply="27", supply_category="standard", tax_treatment="taxable", business=self.business, category=self.category, supplier=self.party,
            title="Recurring", taxable_amount=Decimal("50"), frequency="monthly", start_date=date(2026, 10, 1),
            next_due_date=date(2026, 10, 1), is_active=True, is_cancelled=True)
        self.assertEqual(generate_due_expenses(self.business, date(2026, 10, 1),allow_backfill=True), [])
        schedule.is_cancelled = False
        schedule.save()
        self.assertEqual(len(generate_due_expenses(self.business, date(2026, 10, 1),allow_backfill=True)), 1)
        self.assertEqual(generate_due_expenses(self.business, date(2026, 10, 1),allow_backfill=True), [])

    def test_line_item_id_tampering_cannot_edit_another_invoice(self):
        self.invoice.status = "review"
        self.invoice.save()
        foreign_line = InvoiceLineItem.objects.create(invoice=self.foreign_invoice, description="FOREIGN LINE", quantity=1,
            rate=Decimal("100"), taxable_amount=Decimal("100"), tax_rate=0, tax_amount=0, total_amount=Decimal("100"))
        self.client.post(reverse("invoice_edit", args=[self.invoice.pk]), {
            "invoice_number": "OWN", "invoice_date": "2026-10-01", "supplier_name": self.party.name,
            "taxable_amount": "100", "cgst_amount": "0", "sgst_amount": "0", "igst_amount": "0", "other_charges": "0", "total_amount": "100",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1", "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-id": foreign_line.pk, "items-0-description": "ATTACKED", "items-0-quantity": "1", "items-0-rate": "1",
            "items-0-tax_rate": "0", "items-0-taxable_amount": "1", "items-0-tax_amount": "0", "items-0-total_amount": "1",
        })
        foreign_line.refresh_from_db()
        self.assertEqual(foreign_line.description, "FOREIGN LINE")
        self.assertEqual(foreign_line.invoice_id, self.foreign_invoice.pk)

    def test_staff_business_admin_is_scoped_and_financial_admin_is_read_only(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from accounts.admin import BusinessProfileAdmin
        from audit.admin import AuditEventAdmin
        from expenses.admin import ExpenseDetailAdmin
        self.user.is_staff = True
        self.user.save()
        request = RequestFactory().get("/admin/")
        request.user = self.user
        profile_admin = BusinessProfileAdmin(BusinessProfile, AdminSite())
        self.assertEqual(list(profile_admin.get_queryset(request)), [self.business])
        audit_admin = AuditEventAdmin(AuditEvent, AdminSite())
        self.assertFalse(audit_admin.get_queryset(request).filter(pk=self.event.pk).exists())
        expense_admin = ExpenseDetailAdmin(ExpenseDetail, AdminSite())
        self.assertFalse(expense_admin.has_change_permission(request))
        self.assertFalse(expense_admin.has_delete_permission(request))
        self.assertFalse(profile_admin.has_delete_permission(request))

    def test_sqlite_transaction_mode_and_retry_response(self):
        from django.conf import settings
        from django.db import OperationalError
        from django.test import RequestFactory
        from config.middleware import SecurityBoundaryMiddleware
        self.assertEqual(settings.DATABASES["default"]["OPTIONS"]["transaction_mode"], "IMMEDIATE")
        response = SecurityBoundaryMiddleware(lambda request: None).process_exception(RequestFactory().post("/"), OperationalError("database is locked"))
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(b"database", response.content)

    def test_verification_confirmation_requires_csrf_and_get_is_read_only(self):
        from django.conf import settings
        from django.core import signing
        from accounts.models import AccountVerification
        pending = get_user_model().objects.create_user("pending-account", "pending@example.com", "secret", is_active=False)
        AccountVerification.objects.create(user=pending, email=pending.email)
        link = reverse("verify_email", args=[signing.dumps({"uid": pending.pk, "email": pending.email}, salt="accounts.verify")])
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(link).status_code, 200)
        pending.refresh_from_db()
        self.assertFalse(pending.is_active)
        self.assertEqual(client.post(link).status_code, 403)
        self.assertEqual(client.post(link, HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value).status_code, 302)
        pending.refresh_from_db()
        self.assertTrue(pending.is_active)

    def test_reconciliation_rejects_nonfinite_and_negative_amounts(self):
        from taxation.reconciliation import _decimal, ReconciliationFileError, parse_uploaded_file
        for value in ("NaN", "Infinity", "-1", "99999999999999999999999999999999"):
            with self.subTest(value=value), self.assertRaises(ReconciliationFileError):
                _decimal(value, "tax")
        with self.assertRaises(ReconciliationFileError):
            parse_uploaded_file(SimpleUploadedFile("invalid.json", b"this is not JSON"))

    def test_csrf_denial_audit_redacts_verification_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.post(reverse("verify_email", args=["sensitive-verification-token"]))
        event = AuditEvent.objects.filter(action="security", success=False).latest("pk")
        self.assertNotIn("sensitive-verification-token", str(event.metadata))
        self.assertIn("[redacted]", str(event.metadata))
