from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import smtplib
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
import uuid

import pdfplumber
from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import close_old_connections, connection, DatabaseError, OperationalError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import BusinessParty, BusinessProfile
from audit.models import AuditEvent
from . import email_delivery, test_manual as fixtures
from .email_forms import TOKEN_SALT
from .models import Invoice, InvoiceEmailDelivery, InvoiceLineItem, InvoicePayment
from .pdf_generator import generate_invoice_pdf

D = Decimal


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEFAULT_FROM_EMAIL="billing@example.test", EMAIL_TIMEOUT=15)
class InvoiceEmailTests(TestCase):
    payload = fixtures.ManualInvoiceTests.payload
    create = fixtures.ManualInvoiceTests.create
    finalize = fixtures.ManualInvoiceTests.finalize

    def setUp(self):
        fixtures.ManualInvoiceTests.setUp(self)
        self.customer.email = "customer@example.test"
        self.customer.save()
        self.business.email = "business@example.test"
        self.business.save()

    def finalized(self, **updates):
        invoice = self.create(**updates)
        self.finalize(invoice)
        invoice.refresh_from_db()
        return invoice

    def send(self, invoice, **updates):
        values = dict(user=self.user, invoice_id=invoice.pk, recipient="customer@example.test", request_key=uuid.uuid4())
        values.update(updates)
        return email_delivery.send_invoice_email(**values)

    def confirmation(self, invoice, retry=None):
        url = reverse("invoice_email_retry", args=[invoice.pk, retry.pk]) if retry else reverse("invoice_email", args=[invoice.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return url, {"send_token": response.context["form"].initial["send_token"],
                     "recipient": response.context["form"].initial["recipient"], "confirm": "on"}

    def test_actual_message_plain_html_sender_recipient_subject_and_attachment(self):
        invoice = self.finalized()
        result = self.send(invoice)
        self.assertEqual(result.status, "sent")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["customer@example.test"])
        self.assertEqual(message.from_email, "billing@example.test")
        self.assertEqual(message.subject, f"Invoice {invoice.invoice_number} from Manual business")
        for value in ("Customer", "Manual business", invoice.invoice_number, "INR 224.20", "PDF", "No payments recorded"):
            self.assertIn(value, message.body)
        self.assertEqual(message.alternatives[0].mimetype, "text/html")
        self.assertIn("Grand total", message.alternatives[0].content)
        attachment = message.attachments[0]
        self.assertEqual(attachment.mimetype, "application/pdf")
        self.assertTrue(attachment.filename.endswith(".pdf"))
        self.assertTrue(attachment.content.startswith(b"%PDF-"))
        with pdfplumber.open(BytesIO(attachment.content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        for value in (invoice.invoice_number, "224.20", "17.10", "Consulting"):
            self.assertIn(value, text)
        history = InvoiceEmailDelivery.objects.get()
        self.assertEqual(history.attachment_sha256, sha256(attachment.content).hexdigest())
        self.assertEqual(history.backend, "django.core.mail.backends.locmem.EmailBackend")
        self.assertEqual(history.recipient, message.to[0])
        self.assertEqual(history.subject, message.subject)
        self.assertIsNotNone(history.started_at)
        self.assertIsNotNone(history.completed_at)
        expected = generate_invoice_pdf(Invoice.objects.prefetch_related("line_items", "payments").get(pk=invoice.pk))
        self.assertEqual(attachment.content, expected)

    def test_inclusive_discounted_invoice_attachment_keeps_saved_values(self):
        invoice = self.finalized(pricing_mode="inclusive", invoice_discount_type="percent", invoice_discount_value="10",
            **{"items-0-quantity": "1", "items-0-rate": "1180", "items-0-discount_type": "percent", "items-0-discount": "10"})
        with patch("invoices.manual_calculations.calculate_invoice", side_effect=AssertionError("No recalculation")):
            self.assertEqual(self.send(invoice).status, "sent")
        self.assertIn("955.80", mail.outbox[0].body)
        with pdfplumber.open(BytesIO(mail.outbox[0].attachments[0].content)) as pdf:
            self.assertIn("810.00", "\n".join(page.extract_text() or "" for page in pdf.pages))

    def test_mixed_rate_interstate_attachment(self):
        invoice = self.finalized(place_of_supply="29", **{"items-TOTAL_FORMS": "2", "items-1-description": "Hosting", "items-1-hsn_sac_code": "9983", "items-1-quantity": "1", "items-1-unit": "Month", "items-1-rate": "100", "items-1-discount": "0", "items-1-tax_rate": "5"})
        self.send(invoice)
        with pdfplumber.open(BytesIO(mail.outbox[0].attachments[0].content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        for value in ("Hosting", "5.00%", "18.00%", "34.20", "329.20"):
            self.assertIn(value, text)

    def test_partial_and_fully_paid_messages_use_payment_records(self):
        invoice = self.finalized()
        InvoicePayment.objects.create(user=self.user, invoice=invoice, party=self.customer, payment_date=date.today(), amount=D("100"), payment_mode="cash", reference_id="PRIVATE-ID")
        self.send(invoice)
        self.assertIn("Partially paid", mail.outbox[-1].body)
        self.assertIn("INR 100.00", mail.outbox[-1].body)
        self.assertNotIn("PRIVATE-ID", mail.outbox[-1].body)
        InvoicePayment.objects.create(user=self.user, invoice=invoice, party=self.customer, payment_date=date.today(), amount=D("124.20"), payment_mode="cash", reference_id="FULL")
        Invoice.objects.filter(pk=invoice.pk).update(status="paid")
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)):
            self.assertEqual(self.send(invoice).status, "sent")
        self.assertIn("Payment records: Paid", mail.outbox[-1].body)

    def test_paid_status_without_receipts_does_not_claim_payment_received(self):
        invoice = self.finalized()
        Invoice.objects.filter(pk=invoice.pk).update(status="paid")
        self.send(invoice)
        self.assertIn("No payments recorded", mail.outbox[0].body)
        self.assertNotIn("Payment records: Paid", mail.outbox[0].body)

    def test_draft_cancelled_purchase_and_uploaded_are_rejected(self):
        draft = self.create()
        purchase = self.create(kind="purchase")
        for invoice in (draft, purchase):
            with self.assertRaises(ValidationError):
                self.send(invoice)
            self.assertEqual(self.client.get(reverse("invoice_email", args=[invoice.pk])).status_code, 400)
        self.finalize(draft)
        self.client.post(reverse("invoice_delete", args=[draft.pk]))
        with self.assertRaises(ValidationError):
            self.send(draft)
        uploaded = self.finalized()
        Invoice.objects.filter(pk=uploaded.pk).update(entry_mode="uploaded", document_state="legacy")
        with self.assertRaises(ValidationError):
            self.send(uploaded)
        self.assertFalse(InvoiceEmailDelivery.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_anonymous_cross_business_missing_and_foreign_retry(self):
        invoice = self.finalized()
        self.client.logout()
        self.assertEqual(self.client.get(reverse("invoice_email", args=[invoice.pk])).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("invoice_email", args=[invoice.pk]), {}).status_code, 404)
        self.assertEqual(self.client.get(reverse("invoice_email", args=[999999])).status_code, 404)
        with self.assertRaises(ValidationError):
            self.send(invoice, user=self.other)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(AuditEvent.objects.filter(action="security", business=self.other_business).count(), 2)

    def test_csrf_is_required_and_get_does_not_send(self):
        invoice = self.finalized()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.get(reverse("invoice_email", args=[invoice.pk])).status_code, 200)
        self.assertEqual(client.post(reverse("invoice_email", args=[invoice.pk]), {}).status_code, 403)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(InvoiceEmailDelivery.objects.exists())

    def test_recipient_prefill_uses_snapshot_and_override_needs_confirmation(self):
        invoice = self.finalized()
        self.customer.email = "new-directory@example.test"
        self.customer.save()
        url, payload = self.confirmation(invoice)
        self.assertEqual(payload["recipient"], "customer@example.test")
        payload["recipient"] = "corrected@example.test"
        self.assertEqual(self.client.post(url, payload).status_code, 200)
        self.assertFalse(InvoiceEmailDelivery.objects.exists())
        payload["confirm_override"] = "on"
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(mail.outbox[0].to, ["corrected@example.test"])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, "new-directory@example.test")
        event = AuditEvent.objects.filter(metadata__event="invoice_email_requested").get()
        self.assertTrue(event.metadata["recipient_changed"])
        self.assertNotIn("corrected@example.test", str(event.metadata))

    def test_missing_email_can_be_corrected_without_directory_write(self):
        self.customer.email = ""
        self.customer.save()
        invoice = self.finalized()
        url, data = self.confirmation(invoice)
        self.assertEqual(data["recipient"], "")
        data.update(recipient="new@example.test", confirm_override="on")
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, "")

    def test_invalid_multiple_and_header_injected_recipients(self):
        invoice = self.finalized()
        for value in ("", "invalid", "a@example.test,b@example.test", "a@example.test\r\nBcc: victim@example.test", "\na@example.test"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.send(invoice, recipient=value, confirm_override=True)
        self.assertFalse(InvoiceEmailDelivery.objects.exists())

    def test_confirmation_and_signed_token_cannot_be_bypassed(self):
        invoice = self.finalized()
        url, data = self.confirmation(invoice)
        data["confirm"] = ""
        self.assertEqual(self.client.post(url, data).status_code, 200)
        data.update(confirm="on", send_token="tampered")
        self.assertEqual(self.client.post(url, data).status_code, 200)
        data["send_token"] = signing.dumps({"key":str(uuid.uuid4()), "user":self.other.pk, "invoice":invoice.pk, "retry":None}, salt=TOKEN_SALT)
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_repeated_post_and_refresh_send_only_once(self):
        invoice = self.finalized()
        url, data = self.confirmation(invoice)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.client.get(reverse("invoice_detail", args=[invoice.pk]))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(InvoiceEmailDelivery.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(metadata__event__in=["invoice_email_requested", "invoice_email_accepted"]).count(), 2)

    def test_same_request_key_with_different_recipient_is_rejected(self):
        invoice = self.finalized()
        key = uuid.uuid4()
        self.send(invoice, request_key=key)
        with self.assertRaises(ValidationError):
            self.send(invoice, request_key=key, recipient="other@example.test", confirm_override=True)
        self.assertEqual(len(mail.outbox), 1)

    def test_authentication_rejection_is_failed_and_safe_to_retry(self):
        invoice = self.finalized()
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPAuthenticationError(535, b"SECRET-SMTP-PASSWORD")), self.assertLogs("invoices.email_delivery") as logs:
            result = self.send(invoice)
        self.assertEqual(result.status, "failed")
        previous = InvoiceEmailDelivery.objects.get()
        self.assertEqual(previous.error_category, "authentication_failed")
        self.assertNotIn("SECRET-SMTP-PASSWORD", str(logs.output))
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)):
            url, data = self.confirmation(invoice, previous)
            self.assertEqual(self.client.post(url, data).status_code, 302)
        previous.refresh_from_db()
        self.assertEqual(previous.status, "failed")
        child = InvoiceEmailDelivery.objects.get(retry_of=previous)
        self.assertEqual(child.status, "sent")
        self.assertEqual(child.attempt_number, 2)
        self.assertEqual(len(mail.outbox), 1)

    def test_recipient_rejection_and_backend_zero_are_known_failures(self):
        invoice = self.finalized()
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPRecipientsRefused({"private": (550, b"SECRET")})):
            self.assertEqual(self.send(invoice).error_category, "recipient_rejected")
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)), patch("django.core.mail.EmailMultiAlternatives.send", return_value=0):
            result = self.send(invoice)
        self.assertEqual((result.status, result.error_category), ("failed", "not_accepted"))

    def test_timeout_is_uncertain_no_automatic_retry(self):
        invoice = self.finalized()
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=TimeoutError("PRIVATE")):
            result = self.send(invoice)
        self.assertEqual(result.status, "uncertain")
        previous = InvoiceEmailDelivery.objects.get()
        self.assertEqual(self.client.get(reverse("invoice_email_retry", args=[invoice.pk, previous.pk])).status_code, 400)
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)):
            with self.assertRaises(ValidationError):
                self.send(invoice)
            self.assertEqual(self.send(invoice, confirm_resend=True).status, "sent")
        self.assertEqual(len(mail.outbox), 1)

    def test_three_attempt_retry_limit_and_no_retry_of_success(self):
        invoice = self.finalized()
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)), patch("django.core.mail.EmailMultiAlternatives.send", return_value=0):
            result = self.send(invoice)
            result = self.send(invoice, retry_id=result.delivery_id)
            result = self.send(invoice, retry_id=result.delivery_id)
            with self.assertRaises(ValidationError):
                self.send(invoice, retry_id=result.delivery_id)
        self.assertEqual(InvoiceEmailDelivery.objects.count(), 3)

    def test_retry_form_cannot_retarget_another_invoice(self):
        invoice = self.finalized()
        with patch("django.core.mail.EmailMultiAlternatives.send", return_value=0):
            self.send(invoice)
        previous = InvoiceEmailDelivery.objects.get()
        another = self.finalized()
        self.assertEqual(self.client.get(reverse("invoice_email_retry", args=[another.pk, previous.pk])).status_code, 404)
        with self.assertRaises(ValidationError):
            self.send(another, retry_id=previous.pk)

    def test_retrying_same_failed_parent_twice_reuses_its_child(self):
        invoice = self.finalized()
        with patch("django.core.mail.EmailMultiAlternatives.send", return_value=0):
            result = self.send(invoice)
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)):
            first = self.send(invoice, retry_id=result.delivery_id)
            second = self.send(invoice, retry_id=result.delivery_id)
        self.assertEqual(first.delivery_id, second.delivery_id)
        self.assertTrue(second.reused)
        self.assertEqual(len(mail.outbox), 1)

    def test_cooldown_hourly_limits_and_deliberate_new_send(self):
        invoice = self.finalized()
        self.send(invoice)
        with self.assertRaises(ValidationError):
            self.send(invoice)
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)), patch.object(email_delivery, "HOURLY_INVOICE_LIMIT", 1):
            with self.assertRaises(ValidationError):
                self.send(invoice)
        with patch.object(email_delivery, "COOLDOWN", timedelta(0)):
            self.assertEqual(self.send(invoice).status, "sent")
        self.assertEqual(InvoiceEmailDelivery.objects.filter(retry_of=None).count(), 2)

    def test_business_hourly_limit(self):
        invoice = self.finalized()
        with patch.object(email_delivery, "HOURLY_BUSINESS_LIMIT", 0):
            with self.assertRaises(ValidationError):
                self.send(invoice)
        self.assertFalse(InvoiceEmailDelivery.objects.exists())

    def test_pdf_failure_never_calls_transport(self):
        invoice = self.finalized()
        with patch("invoices.email_delivery.generate_invoice_pdf", side_effect=RuntimeError("PRIVATE/PATH")), patch("invoices.email_delivery.delivery_error") as transport:
            result = self.send(invoice)
        self.assertEqual((result.status, result.error_category), ("failed", "pdf_failed"))
        transport.assert_not_called()

    def test_no_send_when_history_or_claim_cannot_be_written(self):
        invoice = self.finalized()
        with patch("invoices.email_delivery._reserve", side_effect=DatabaseError("SECRET")):
            self.assertEqual(self.send(invoice).status, "unavailable")
        with patch("invoices.email_delivery._claim", side_effect=DatabaseError("SECRET")):
            self.assertEqual(self.send(invoice).status, "unavailable")
        self.assertEqual(len(mail.outbox), 0)

    def test_recording_failure_after_acceptance_stays_uncertain_and_never_auto_resends(self):
        invoice = self.finalized()
        key = uuid.uuid4()
        with patch("invoices.email_delivery._finish", side_effect=DatabaseError("SECRET")):
            self.assertEqual(self.send(invoice, request_key=key).status, "uncertain")
        self.assertEqual(InvoiceEmailDelivery.objects.get().status, "sending")
        self.assertTrue(self.send(invoice, request_key=key).reused)
        with self.assertRaises(ValidationError):
            self.send(invoice)
        self.assertEqual(len(mail.outbox), 1)

    def test_migration_gate_keeps_existing_detail_usable_and_does_not_send(self):
        invoice = self.finalized()
        with patch("invoices.email_views.history_available", return_value=False):
            self.assertContains(self.client.get(reverse("invoice_detail", args=[invoice.pk])), "pending database migration")
            self.assertEqual(self.client.post(reverse("invoice_email", args=[invoice.pk]), {}).status_code, 503)
        with patch("invoices.email_delivery.history_available", return_value=False):
            self.assertEqual(self.send(invoice).status, "unavailable")
        self.assertEqual(len(mail.outbox), 0)

    def test_invoice_payments_party_and_original_file_remain_unchanged(self):
        invoice = self.finalized()
        with TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            invoice.source_file.save("original.pdf", ContentFile(b"original-source"))
            before = Invoice.objects.filter(pk=invoice.pk).values().get()
            lines = list(invoice.line_items.values())
            parties = list(BusinessParty.objects.values())
            self.send(invoice)
            self.assertEqual(Invoice.objects.filter(pk=invoice.pk).values().get(), before)
            self.assertEqual(list(invoice.line_items.values()), lines)
            self.assertEqual(list(BusinessParty.objects.values()), parties)
            self.assertFalse(InvoicePayment.objects.exists())
            with invoice.source_file.open("rb") as source:
                self.assertEqual(source.read(), b"original-source")

    def test_html_escapes_names_and_header_cannot_be_injected(self):
        invoice = self.finalized()
        Invoice.objects.filter(pk=invoice.pk).update(business_snapshot={"name": "Business\r\nBcc: attacker@example.test", "email": "business@example.test"}, party_snapshot={"name": '<img src="https://evil.invalid">', "email":"customer@example.test"})
        self.send(invoice)
        message = mail.outbox[0]
        self.assertNotIn("\n", message.subject)
        self.assertNotIn("\r", message.subject)
        self.assertNotIn('<img src="https://evil.invalid">', message.alternatives[0].content)
        self.assertIn("&lt;img", message.alternatives[0].content)
        self.assertEqual(message.bcc, [])

    def test_attachment_limit_and_timeout_configuration(self):
        invoice = self.finalized()
        with override_settings(EMAIL_TIMEOUT=999), patch("invoices.email_delivery.get_connection", wraps=mail.get_connection) as backend:
            self.send(invoice)
        self.assertEqual(backend.call_args.kwargs["timeout"], 30)

    def test_cancellation_during_pdf_generation_prevents_transport(self):
        invoice = self.finalized()
        original = email_delivery.generate_invoice_pdf
        def cancel_after_render(value):
            result = original(value)
            Invoice.objects.filter(pk=invoice.pk).update(document_state="cancelled", status="cancelled")
            return result
        with patch("invoices.email_delivery.generate_invoice_pdf", side_effect=cancel_after_render):
            result = self.send(invoice)
        self.assertEqual((result.status, result.error_category), ("failed", "ineligible"))
        self.assertEqual(len(mail.outbox), 0)

    def test_eligibility_and_history_actions_on_invoice_detail(self):
        draft = self.create()
        self.assertNotContains(self.client.get(reverse("invoice_detail", args=[draft.pk])), "Send Invoice Email")
        self.finalize(draft)
        self.assertContains(self.client.get(reverse("invoice_detail", args=[draft.pk])), "Send Invoice Email")
        self.send(draft)
        response = self.client.get(reverse("invoice_detail", args=[draft.pk]))
        self.assertContains(response, "customer@example.test")
        self.assertContains(response, "inbox unconfirmed")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEFAULT_FROM_EMAIL="billing@example.test")
class InvoiceEmailConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("email-concurrent", password="secret")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Email business")
        self.invoice = Invoice.objects.create(user=self.user, invoice_type="sales", entry_mode="manual", document_state="finalized", status="verified", invoice_number="EMAIL-CONCURRENT", invoice_date=date.today(), supplier_name="Email business", total_amount=D("100"), taxable_amount=D("100"), business_snapshot={"name":"Email business"}, party_snapshot={"name":"Customer", "email":"customer@example.test"})
        InvoiceLineItem.objects.create(invoice=self.invoice, description="Saved item", quantity=D("1"), rate=D("100"), taxable_amount=D("100"), total_amount=D("100"))

    def test_transport_is_outside_database_transaction(self):
        def accepted(message):
            self.assertFalse(connection.in_atomic_block)
            return ""
        with patch("invoices.email_delivery.delivery_error", side_effect=accepted):
            result = email_delivery.send_invoice_email(user=self.user, invoice_id=self.invoice.pk, recipient="customer@example.test", request_key=uuid.uuid4())
        self.assertEqual(result.status, "sent")

    def test_concurrent_claims_allow_only_one_winner(self):
        delivery = InvoiceEmailDelivery.objects.create(invoice=self.invoice, business=self.business, initiated_by=self.user, recipient="customer@example.test", subject="Invoice", request_key=uuid.uuid4(), backend="test")
        barrier = Barrier(2)
        def claim(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return email_delivery._claim(delivery)
            except OperationalError:
                return False
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = list(pool.map(claim, [0, 1]))
        self.assertEqual(sum(winners), 1)
        self.assertFalse(email_delivery._claim(delivery))

    def test_concurrent_same_request_never_sends_twice(self):
        barrier = Barrier(2)
        key = uuid.uuid4()
        def send(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return email_delivery.send_invoice_email(user=self.user, invoice_id=self.invoice.pk, recipient="customer@example.test", request_key=key).status
            finally:
                close_old_connections()
        with patch("invoices.email_delivery.delivery_error", return_value="") as transport:
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(send, [0, 1]))
        self.assertLessEqual(transport.call_count, 1, statuses)
        self.assertLessEqual(InvoiceEmailDelivery.objects.filter(request_key=key).count(), 1)
