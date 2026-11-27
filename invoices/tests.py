from invoices.test_files import valid_pdf
import os
import tempfile
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook
from PIL import Image
from pytesseract import TesseractNotFoundError

from accounts.models import BusinessProfile
from audit.models import AuditEvent
from .extraction import OCRUnavailableError, _document_text
from .models import Invoice, InvoiceBatch, InvoiceLineItem


class InvoiceDuplicateDetectionTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        override = override_settings(MEDIA_ROOT=self.media_dir.name)
        override.enable()
        self.addCleanup(override.disable)

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="invoice-owner",
            password="test-password",
        )
        BusinessProfile.objects.create(
            user=self.user,
            business_name="Example Business",
        )
        self.client.force_login(self.user)

    def make_invoice(self, invoice_type, **overrides):
        fields = {
            "user": self.user,
            "invoice_type": invoice_type,
            "invoice_number": "INV-100",
            "invoice_date": date(2026, 10, 1),
            "supplier_name": "Supplier One",
            "buyer_name": "Buyer One",
            "taxable_amount": Decimal("100.00"),
            "total_amount": Decimal("100.00"),
        }
        fields.update(overrides)
        return Invoice.objects.create(**fields)

    def upload(self, invoice_type, extracted_data):
        self.extraction_patch = patch(
            "invoices.views.extract_invoice_data",
            return_value={
                "extracted_text": "",
                "invoice_number": "INV-100",
                "invoice_date": date(2026, 10, 1),
                "supplier_name": "Supplier One",
                "supplier_gstin": "",
                "buyer_name": "Buyer One",
                "buyer_gstin": "",
                "taxable_amount": Decimal("100.00"),
                "cgst_amount": Decimal("0.00"),
                "sgst_amount": Decimal("0.00"),
                "igst_amount": Decimal("0.00"),
                "other_charges": Decimal("0.00"),
                "total_amount": Decimal("100.00"),
                "line_items": [],
                "validation_errors": [],
                **extracted_data,
            },
        )
        self.extraction_patch.start()
        self.addCleanup(self.extraction_patch.stop)

        return self.client.post(
            reverse("invoice_upload"),
            {
                "invoice_type": invoice_type,
                "source_file": SimpleUploadedFile(
                    "invoice.pdf",
                    valid_pdf(),
                    content_type="application/pdf",
                ),
            },
        )

    def assert_duplicate_rejected(self, invoice_type, invoice_overrides, upload_data):
        self.make_invoice(invoice_type, **invoice_overrides)
        response = self.upload(invoice_type, upload_data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 1)
        self.assertFalse(AuditEvent.objects.filter(module="invoices", action="create").exists())

    def test_duplicate_sales_invoice_uses_buyer_gstin_case_insensitively(self):
        self.assert_duplicate_rejected(
            "sales",
            {
                "buyer_gstin": "27ABCDE1234F1Z5",
                "invoice_number": "inv-100",
            },
            {
                "buyer_gstin": " 27abcde1234f1z5 ",
                "buyer_name": "Buyer One",
            },
        )

    def test_successful_upload_is_audited(self):
        response = self.upload("purchase", {})
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get(user=self.user, invoice_number="INV-100")
        self.assertTrue(AuditEvent.objects.filter(
            business=self.user.business_profile, action="create", module="invoices",
            object_type="invoices.invoice", object_id=str(invoice.pk),
        ).exists())

    def test_duplicate_invoice_number_ignores_leading_and_trailing_whitespace(self):
        self.assert_duplicate_rejected(
            "purchase",
            {
                "supplier_gstin": "27ABCDE1234F1Z5",
                "invoice_number": "  INV-100  ",
            },
            {"supplier_gstin": "27ABCDE1234F1Z5"},
        )

    def test_duplicate_purchase_invoice_uses_supplier_gstin(self):
        self.assert_duplicate_rejected(
            "purchase",
            {"supplier_gstin": "27ABCDE1234F1Z5"},
            {"supplier_gstin": "27abcde1234f1z5"},
        )

    def test_invoice_number_duplicate_detection_is_case_insensitive(self):
        self.assert_duplicate_rejected(
            "purchase",
            {
                "supplier_gstin": "27ABCDE1234F1Z5",
                "invoice_number": "inv-100",
            },
            {
                "supplier_gstin": "27ABCDE1234F1Z5",
                "invoice_number": "INV-100",
            },
        )

    def test_duplicate_expense_invoice_uses_supplier_gstin(self):
        self.assert_duplicate_rejected(
            "expense",
            {"supplier_gstin": "27ABCDE1234F1Z5"},
            {"supplier_gstin": "27abcde1234f1z5"},
        )

    def test_same_invoice_number_for_different_gstins_is_allowed(self):
        self.make_invoice("purchase", supplier_gstin="27ABCDE1234F1Z5")
        response = self.upload(
            "purchase",
            {"supplier_gstin": "29ABCDE1234F1Z5"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("invoice_list"))
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 2)

    def test_missing_gstin_same_normalized_name_warns_but_saves(self):
        self.make_invoice("purchase", supplier_name="  Supplier   One ")
        response = self.upload(
            "purchase",
            {"supplier_name": "supplier one"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("invoice_list"))
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 2)

        list_response = self.client.get(reverse("invoice_list"))
        messages = [
            str(message)
            for message in list_response.context["messages"]
        ]
        self.assertTrue(
            any("may match an existing record" in message for message in messages)
        )

    def test_whitespace_only_gstin_is_treated_as_missing(self):
        self.make_invoice(
            "purchase",
            supplier_name="Supplier One",
            supplier_gstin="   ",
        )
        response = self.upload(
            "purchase",
            {
                "supplier_name": "supplier one",
                "supplier_gstin": "  \t ",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 2)
        self.assertEqual(
            Invoice.objects.filter(user=self.user)
            .order_by("-pk")
            .first()
            .supplier_gstin,
            "",
        )

    def test_whitespace_only_gstin_records_do_not_conflict_with_constraints(self):
        self.make_invoice("sales", buyer_gstin="   ")
        self.make_invoice("sales", buyer_gstin="\t")
        self.make_invoice("purchase", supplier_gstin="   ")
        self.make_invoice("purchase", supplier_gstin="\t")

        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 4)

    def test_missing_gstin_different_party_name_is_not_rejected_or_warned(self):
        self.make_invoice("purchase", supplier_name="Supplier One")
        response = self.upload(
            "purchase",
            {"supplier_name": "Different Supplier"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("invoice_list"))
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 2)

        list_response = self.client.get(reverse("invoice_list"))
        messages = [
            str(message)
            for message in list_response.context["messages"]
        ]
        self.assertFalse(
            any("may match an existing record" in message for message in messages)
        )

    def test_same_invoice_number_across_types_is_allowed(self):
        self.make_invoice("purchase", supplier_gstin="27ABCDE1234F1Z5")
        response = self.upload(
            "expense",
            {"supplier_gstin": "27ABCDE1234F1Z5"},
        )
        self.assertRedirects(response, reverse("invoice_list"))
        self.assertEqual(Invoice.objects.filter(user=self.user).count(), 2)

    def test_multi_upload_creates_owned_batch_and_two_sheet_excel(self):
        def extracted(number):
            return {
                "extracted_text": "Invoice Number " + number,
                "invoice_number": number,
                "invoice_date": date(2026, 10, 1),
                "supplier_name": "Supplier One",
                "supplier_gstin": "27ABCDE1234F1Z5",
                "buyer_name": "Buyer One", "buyer_gstin": "",
                "taxable_amount": Decimal("100.00"),
                "cgst_amount": Decimal("0.00"), "sgst_amount": Decimal("0.00"),
                "igst_amount": Decimal("0.00"), "other_charges": Decimal("0.00"),
                "total_amount": Decimal("100.00"), "line_items": [], "validation_errors": [],
            }

        with patch("invoices.views.extract_invoice_data", side_effect=[extracted("BATCH-1"), extracted("BATCH-2")]):
            response = self.client.post(reverse("invoice_upload"), {
                "invoice_type": "purchase",
                "source_files": [
                    SimpleUploadedFile("one.pdf", valid_pdf(), content_type="application/pdf"),
                    SimpleUploadedFile("two.pdf", valid_pdf(), content_type="application/pdf"),
                ],
            })
        self.assertEqual(response.status_code, 302)
        batch = InvoiceBatch.objects.get(user=self.user)
        self.assertEqual(batch.invoices.count(), 2)
        self.assertEqual(batch.file_count, 2)
        export = self.client.get(reverse("invoice_batch_export", args=[batch.pk]))
        self.assertEqual(export.status_code, 200)
        workbook = load_workbook(BytesIO(export.content), data_only=True)
        self.assertEqual(workbook.sheetnames, ["Invoices", "Line Items"])
        self.assertEqual(workbook["Invoices"].max_row, 3)

        other = get_user_model().objects.create_user(username="batch-other", password="secret")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("invoice_batch_detail", args=[batch.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("invoice_batch_export", args=[batch.pk])).status_code, 404)

    def test_local_ocr_handles_image_and_scanned_pdf_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = os.path.join(directory, "invoice.png")
            pdf_path = os.path.join(directory, "scanned.pdf")
            image = Image.new("RGB", (400, 300), "white")
            image.save(image_path)
            image.save(pdf_path, "PDF")
            with patch("invoices.extraction.pytesseract.image_to_string", return_value="Invoice Number OCR-1") as ocr:
                _, image_type = _document_text(image_path)
                _, pdf_type = _document_text(pdf_path)
            self.assertEqual(image_type, "image_ocr")
            self.assertEqual(pdf_type, "scanned_pdf")
            self.assertEqual(ocr.call_count, 2)

    def test_scanned_document_reports_missing_local_tesseract(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = os.path.join(directory, "invoice.png")
            Image.new("RGB", (400, 300), "white").save(image_path)
            with patch("invoices.extraction.pytesseract.image_to_string", side_effect=TesseractNotFoundError()):
                with self.assertRaises(OCRUnavailableError):
                    _document_text(image_path)

    def test_review_edit_recalculates_extraction_and_total_warnings(self):
        invoice = Invoice.objects.create(
            user=self.user, invoice_type="purchase", invoice_number="EDIT-1",
            invoice_date=date(2026, 10, 1), supplier_name="Supplier One",
            taxable_amount=Decimal("100.00"), total_amount=Decimal("100.00"),
            validation_errors=["CGST amount could not be extracted.", "Invoice total does not match the sum of extracted amounts."],
            status="review",
        )
        item = InvoiceLineItem.objects.create(
            invoice=invoice, description="Service", quantity=Decimal("1.000"),
            rate=Decimal("100.00"), tax_rate=Decimal("0.00"),
            taxable_amount=Decimal("100.00"), tax_amount=Decimal("0.00"),
            total_amount=Decimal("100.00"),
        )
        response = self.client.post(reverse("invoice_edit", args=[invoice.pk]), {
            "invoice_number": "EDIT-1", "invoice_date": "2026-10-01", "due_date": "",
            "supplier_name": "Supplier One", "supplier_gstin": "", "buyer_name": "", "buyer_gstin": "",
            "taxable_amount": "100.00", "cgst_amount": "18.00", "sgst_amount": "0.00",
            "igst_amount": "0.00", "other_charges": "0.00", "total_amount": "118.00",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(item.pk), "items-0-description": "Service", "items-0-hsn_sac_code": "",
            "items-0-quantity": "1.000", "items-0-unit": "", "items-0-rate": "100.00",
            "items-0-tax_rate": "18.00", "items-0-taxable_amount": "100.00",
            "items-0-tax_amount": "18.00", "items-0-total_amount": "118.00",
        })
        self.assertEqual(response.status_code, 302, msg=str(response.context["form"].errors) if response.context else "")
        invoice.refresh_from_db()
        self.assertEqual(invoice.validation_errors, [])
        verified = self.client.post(reverse("invoice_verify", args=[invoice.pk]))
        self.assertEqual(verified.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "verified")

