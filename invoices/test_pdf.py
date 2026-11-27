from datetime import date
from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pdfplumber
import pypdfium2 as pdfium
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import BusinessParty, BusinessProfile
from audit.models import AuditEvent
from . import test_manual as fixtures
from .models import Invoice, InvoiceLineItem, InvoicePayment
from .pdf_generator import (
    PDFGenerationError, amount_in_words, generate_invoice_pdf, indian_number,
    invoice_pdf_data, invoice_pdf_filename, render_invoice_pdf,
)

D = Decimal


def extracted(content):
    with pdfplumber.open(BytesIO(content)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


class AmountWordsTests(SimpleTestCase):
    def test_zero_rupees_and_paise(self):
        for value, expected in (("0", "Rupees Zero Only"), ("0.05", "Rupees Zero and Five Paise Only"), ("1180", "Rupees One Thousand One Hundred Eighty Only"), ("125450.25", "Rupees One Lakh Twenty Five Thousand Four Hundred Fifty and Twenty Five Paise Only")):
            with self.subTest(value=value):
                self.assertEqual(amount_in_words(D(value)), expected)

    def test_crore_and_maximum_supported_amount(self):
        self.assertEqual(amount_in_words(D("10000000")), "Rupees One Crore Only")
        self.assertEqual(amount_in_words(D("999999999999.99")), "Rupees Ninety Nine Thousand Nine Hundred Ninety Nine Crore Ninety Nine Lakh Ninety Nine Thousand Nine Hundred Ninety Nine and Ninety Nine Paise Only")

    def test_invalid_amounts_are_not_rounded(self):
        for value in ("-1", "1.001", "NaN", "Infinity", "1000000000000"):
            with self.subTest(value=value), self.assertRaises(PDFGenerationError):
                amount_in_words(D(value))

    def test_indian_grouping_and_adjustments(self):
        for value, expected in (("0", "0.00"), ("125450", "1,25,450.00"), ("-0.25", "-0.25"), ("999999999999.99", "9,99,99,99,99,999.99")):
            self.assertEqual(indian_number(D(value)), expected)


class InvoicePDFTests(TestCase):
    setUp = fixtures.ManualInvoiceTests.setUp
    payload = fixtures.ManualInvoiceTests.payload
    create = fixtures.ManualInvoiceTests.create
    finalize = fixtures.ManualInvoiceTests.finalize

    def loaded(self, invoice):
        return Invoice.objects.prefetch_related("line_items", "payments", "adjustment_notes").get(pk=invoice.pk)

    def download(self, invoice, **query):
        return self.client.get(reverse("invoice_pdf", args=[invoice.pk]), query)

    def text(self, invoice):
        response = self.download(invoice)
        self.assertEqual(response.status_code, 200, response.content[:200])
        return extracted(response.content)

    def payment(self, invoice, amount, reference="PRIVATE-REFERENCE"):
        return InvoicePayment.objects.create(user=self.user, invoice=invoice, party=self.customer,
            payment_date=date(2026, 10, 2), amount=D(amount), payment_mode="bank_transfer", reference_id=reference)

    def test_real_pdf_headers_filename_and_a4(self):
        invoice = self.create()
        response = self.download(invoice)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(invoice_pdf_filename(invoice), response["Content-Disposition"])
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertTrue(response.content.rstrip().endswith(b"%%EOF"))
        with pdfplumber.open(BytesIO(response.content)) as document:
            self.assertAlmostEqual(document.pages[0].width, 595.28, places=1)
            self.assertAlmostEqual(document.pages[0].height, 841.89, places=1)

    def test_content_names_items_and_saved_totals(self):
        invoice = self.create()
        text = self.text(invoice)
        for value in (invoice.invoice_number, "Manual business", "Customer", "Consulting", "9983", "224.20", "190.00", "17.10", "Net 30", "Thank you", "Pay by due date", "Rupees Two Hundred Twenty Four and Twenty Paise Only"):
            self.assertIn(value, text)

    def test_draft_has_watermark_and_no_tax_invoice_title(self):
        invoice = self.create()
        text = self.text(invoice)
        self.assertIn("DRAFT", text)
        self.assertNotIn("TAX INVOICE", text)
        self.assertIn("not an active payment demand", text)

    def test_finalized_registered_taxable_sales_title(self):
        invoice = self.create()
        self.finalize(invoice)
        text = self.text(invoice)
        self.assertIn("TAX INVOICE", text)
        self.assertIn("FINALIZED", text)
        self.assertNotIn("DRAFT", text)
        self.assertIn("Authorized signatory", text)

    def test_unregistered_sales_do_not_claim_tax_invoice(self):
        self.business.gstin = None
        self.business.save(update_fields=["gstin"])
        invoice = self.create(**{'items-0-tax_rate':'0','items-0-tax_treatment':'non_gst'})
        self.finalize(invoice)
        self.assertEqual(invoice.total_tax, D('0'))
        self.assertNotIn("TAX INVOICE", self.text(invoice))

    def test_cancelled_copy_retains_saved_totals(self):
        invoice = self.create()
        self.finalize(invoice)
        self.client.post(reverse("invoice_delete", args=[invoice.pk]))
        text = self.text(invoice)
        self.assertIn("CANCELLED", text)
        self.assertIn("224.20", text)

    def test_inclusive_saved_amounts_and_discounts(self):
        invoice = self.create(pricing_mode="inclusive", invoice_discount_type="percent", invoice_discount_value="10",
            **{"items-0-quantity": "1", "items-0-rate": "1180", "items-0-discount_type": "percent", "items-0-discount": "10"})
        data = invoice_pdf_data(self.loaded(invoice))
        self.assertEqual(data["totals"]["subtotal"], D("1180"))
        self.assertEqual(data["totals"]["line_discounts"], D("118"))
        self.assertEqual(data["totals"]["invoice_discount_amount"], D("106.20"))
        text = self.text(invoice)
        for value in ("inclusive", "955.80", "810.00", "118.00", "106.20"):
            self.assertIn(value, text)

    def test_exclusive_subtotal_is_presentation_from_saved_values(self):
        invoice = self.create(invoice_discount_value="19")
        data = invoice_pdf_data(self.loaded(invoice))
        self.assertEqual(data["totals"]["subtotal"], D("200"))
        self.assertEqual(data["totals"]["taxable_amount"], D("171"))

    def test_interstate_components(self):
        invoice = self.create(place_of_supply="29")
        data = invoice_pdf_data(self.loaded(invoice))
        self.assertEqual(data["tax_groups"][0]["igst"], D("34.20"))
        self.assertEqual(data["tax_groups"][0]["cgst"], D("0"))
        self.assertIn("34.20", self.text(invoice))

    def test_mixed_rates_summary_sums_only_saved_components(self):
        data = self.payload(**{"items-TOTAL_FORMS": "2", "items-1-description": "Second product", "items-1-hsn_sac_code": "1234", "items-1-quantity": "1", "items-1-unit": "Nos", "items-1-rate": "100", "items-1-discount": "0", "items-1-tax_rate": "5"})
        response = self.client.post(reverse("invoice_create", args=["sales"]), data)
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get(user=self.user)
        groups = invoice_pdf_data(self.loaded(invoice))["tax_groups"]
        self.assertEqual([group["rate"] for group in groups], [D("5"), D("18")])
        self.assertEqual([group["cgst"] for group in groups], [D("2.50"), D("17.10")])
        self.assertIn("Second product", self.text(invoice))

    def test_distinct_zero_exempt_nil_and_non_gst_treatments(self):
        for treatment in ("taxable", "exempt", "nil_rated", "non_gst"):
            with self.subTest(treatment=treatment):
                invoice = self.create(**{"items-0-tax_rate": "0", "items-0-tax_treatment": treatment})
                self.finalize(invoice)
                data = invoice_pdf_data(self.loaded(invoice))
                self.assertEqual(data["tax_groups"][0]["treatment"], treatment)
                self.assertNotEqual(data["title"], "TAX INVOICE")
                self.assertIn("190.00", self.text(invoice))

    def test_purchase_roles_and_document_type(self):
        invoice = self.create(kind="purchase")
        data = invoice_pdf_data(self.loaded(invoice))
        self.assertEqual(data["seller"]["name"], "Supplier")
        self.assertEqual(data["buyer"]["name"], "Manual business")
        self.assertEqual(data["title"], "PURCHASE RECORD")
        self.assertIn("PURCHASE RECORD", self.text(invoice))

    def test_partial_payment_uses_existing_ledger_and_no_identifiers(self):
        invoice = self.create()
        self.finalize(invoice)
        self.payment(invoice, "100")
        text = self.text(invoice)
        for value in ("Partial", "100.00", "124.20", "Current payment summary", "not an archived payment statement"):
            self.assertIn(value, text)
        self.assertNotIn("PRIVATE-REFERENCE", text)

    def test_full_payment(self):
        invoice = self.create()
        self.finalize(invoice)
        self.payment(invoice, "224.20")
        data = invoice_pdf_data(self.loaded(invoice))
        self.assertEqual(data["payment"], {"status": "paid", "paid": D("224.20"), "outstanding": D("0")})

    def test_foreign_owner_payment_is_excluded(self):
        invoice = self.create()
        self.finalize(invoice)
        InvoicePayment.objects.create(user=self.other, invoice=invoice, party=self.other_customer,
            payment_date=date(2026, 10, 2), amount=D("100"), payment_mode="cash", reference_id="foreign")
        self.assertEqual(invoice_pdf_data(self.loaded(invoice))["payment"]["paid"], D("0"))

    def test_finalized_downloads_are_byte_identical_despite_profile_changes(self):
        self.business.address_line = "Saved business address"
        self.business.save()
        self.customer.address_line = "Saved customer address"
        self.customer.save()
        invoice = self.create()
        self.finalize(invoice)
        before = self.download(invoice).content
        BusinessProfile.objects.filter(pk=self.business.pk).update(business_name="NEW PROFILE", address_line="NEW ADDRESS")
        BusinessParty.objects.filter(pk=self.customer.pk).update(name="NEW CUSTOMER", address_line="NEW CUSTOMER ADDRESS")
        after = self.download(invoice).content
        self.assertEqual(before, after)
        self.assertIn("Saved customer address", extracted(after))
        self.assertNotIn("NEW PROFILE", extracted(after))

    def test_dynamic_payment_copy_changes_without_changing_invoice_content(self):
        invoice = self.create()
        self.finalize(invoice)
        before = invoice_pdf_data(self.loaded(invoice))
        self.payment(invoice, "100")
        after = invoice_pdf_data(self.loaded(invoice))
        self.assertEqual(before["totals"], after["totals"])
        self.assertEqual(before["lines"], after["lines"])
        self.assertEqual(before["seller"], after["seller"])
        self.assertNotEqual(before["revision"], after["revision"])

    def test_legacy_totals_not_recalculated_or_inferred(self):
        invoice = Invoice.objects.create(user=self.user, invoice_type="purchase", invoice_number="LEGACY", invoice_date=date(2026, 1, 1), supplier_name="Historic supplier", supplier_gstin="", buyer_name="Historic buyer", taxable_amount=D("101.23"), cgst_amount=D("9.11"), sgst_amount=D("9.12"), total_amount=D("119.46"), status="verified")
        InvoiceLineItem.objects.create(invoice=invoice, description="Historical source line", quantity=D("1"), rate=D("999"), taxable_amount=D("101.23"), tax_rate=D("18"), tax_amount=D("18.23"), total_amount=D("119.46"))
        with patch("invoices.manual_calculations.calculate_invoice", side_effect=AssertionError("Must not recalculate")):
            text = self.text(invoice)
        for value in ("119.46", "9.11", "9.12", "Historic supplier", "Not recorded", "snapshots are incomplete"):
            self.assertIn(value, text)
        self.assertNotIn("Manual business", text)
        invoice.refresh_from_db()
        self.assertIsNone(invoice.calculation_version)
        self.assertEqual(invoice.total_amount, D("119.46"))

    def test_header_line_inconsistency_is_disclosed_without_repair(self):
        invoice = self.create()
        InvoiceLineItem.objects.filter(invoice=invoice).update(total_amount=D("1"))
        self.assertIn("Saved line totals and header totals differ", self.text(invoice))
        self.assertEqual(InvoiceLineItem.objects.get(invoice=invoice).total_amount, D("1"))

    def test_download_changes_only_one_audit_event(self):
        invoice = self.create()
        before = Invoice.objects.filter(pk=invoice.pk).values().get()
        lines = list(invoice.line_items.values())
        audit_count = AuditEvent.objects.count()
        self.download(invoice)
        self.assertEqual(Invoice.objects.filter(pk=invoice.pk).values().get(), before)
        self.assertEqual(list(invoice.line_items.values()), lines)
        self.assertEqual(AuditEvent.objects.count(), audit_count + 1)
        self.assertEqual(AuditEvent.objects.first().action, "export")
        self.assertFalse(InvoicePayment.objects.exists())

    def test_anonymous_and_cross_business_and_missing_access(self):
        invoice = self.create()
        self.client.logout()
        self.assertEqual(self.download(invoice).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.download(invoice).status_code, 404)
        self.assertEqual(self.client.get(reverse("invoice_pdf", args=[9999999])).status_code, 404)
        events = AuditEvent.objects.filter(action="security", success=False, business=self.other_business)
        self.assertEqual(events.count(), 2)
        self.assertEqual(events.first().object_id, "")

    def test_profile_required_for_business_scope(self):
        invoice = self.create()
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user("no-business")
        Invoice.objects.filter(pk=invoice.pk).update(user=user)
        self.client.force_login(user)
        self.assertEqual(self.download(invoice).status_code, 404)

    def test_only_get_and_preview_disposition(self):
        invoice = self.create()
        self.assertEqual(self.client.post(reverse("invoice_pdf", args=[invoice.pk])).status_code, 405)
        self.assertIn("inline", self.download(invoice, preview="1")["Content-Disposition"])
        self.assertIn("preview", AuditEvent.objects.first().description)

    def test_filename_and_untrusted_content_remain_literal_text(self):
        invoice = self.create()
        Invoice.objects.filter(pk=invoice.pk).update(invoice_number='../../unsafe\r\nX-Evil: yes', notes='<img src="file:///etc/passwd"/> & <b>not markup</b>')
        InvoiceLineItem.objects.filter(invoice=invoice).update(description='<a href="https://evil.invalid">Hello</a> & Goodbye')
        response = self.download(invoice)
        self.assertEqual(response.status_code, 200)
        filename = response["Content-Disposition"]
        self.assertNotIn("\r", filename)
        self.assertNotIn("\n", filename)
        self.assertNotIn("../", filename)
        text = extracted(response.content)
        self.assertIn("not markup</b>", text)
        self.assertIn("Goodbye", text)
        with pdfplumber.open(BytesIO(response.content)) as pdf:
            self.assertFalse(any(page.hyperlinks for page in pdf.pages))

    def test_original_upload_is_unchanged_and_separate(self):
        invoice = self.create()
        with TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            original = b"original-third-party-file"
            invoice.source_file.save("original.pdf", ContentFile(original))
            # A source-backed legacy record must never become a supplier-issued tax invoice.
            Invoice.objects.filter(pk=invoice.pk).update(entry_mode="uploaded", document_state="legacy", status="verified")
            self.assertIn("INVOICE RECORD COPY", self.text(invoice))
            with invoice.source_file.open("rb") as stored:
                self.assertEqual(stored.read(), original)
            response = self.client.get(reverse("invoice_source", args=[invoice.pk]), {"download": "1"})
            self.assertEqual(b"".join(response.streaming_content), original)

    def test_multipage_long_descriptions_headers_and_footer_bounds(self):
        invoice = self.create()
        original = invoice.line_items.get()
        values = {field.name: getattr(original, field.name) for field in original._meta.fields if field.name not in {"id", "created_at", "invoice"}}
        InvoiceLineItem.objects.bulk_create([InvoiceLineItem(invoice=invoice, **{**values, "description": "Long description " + ("wrapped text " * 30) + f" item {index}"}) for index in range(50)])
        content = self.download(invoice).content
        with pdfplumber.open(BytesIO(content)) as pdf:
            self.assertGreater(len(pdf.pages), 2)
            repeated = [page for page in pdf.pages if "wrapped text" in (page.extract_text() or "")]
            self.assertGreater(len(repeated), 1)
            for page in repeated:
                text = page.extract_text()
                self.assertIn("Description /", text)
                self.assertIn("Unit rate", text)
                self.assertIn("Page ", text)
                for word in page.extract_words():
                    if word["top"] < 800:
                        self.assertLess(word["bottom"], page.height - 35)
            self.assertIn("10 Long description", "\n".join(page.extract_text() or "" for page in pdf.pages))
        document = pdfium.PdfDocument(content)
        try:
            page = document[0]
            bitmap = page.render(scale=1)
            self.assertGreater(bitmap.width, 500)
            bitmap.close()
            page.close()
        finally:
            document.close()

    def test_missing_optional_fields_and_unsupported_image_metadata(self):
        invoice = self.create()
        Invoice.objects.filter(pk=invoice.pk).update(business_snapshot={"name": "Saved", "logo": "https://evil.invalid/logo.png", "signature": "/etc/passwd"}, party_snapshot={})
        self.assertEqual(self.download(invoice).status_code, 200)

    def test_excessive_line_count_fails_without_partial_pdf(self):
        invoice = self.create()
        InvoiceLineItem.objects.bulk_create([InvoiceLineItem(invoice=invoice, description="overflow") for _ in range(500)])
        response = self.download(invoice)
        self.assertEqual(response.status_code, 422)
        self.assertNotEqual(response["Content-Type"], "application/pdf")
        self.assertNotIn(b"%PDF", response.content)
        self.assertFalse(AuditEvent.objects.first().success)

    def test_oversized_text_is_bounded(self):
        invoice = self.create()
        Invoice.objects.filter(pk=invoice.pk).update(notes="A" * 12001)
        self.assertEqual(self.download(invoice).status_code, 422)

    def test_page_limit_fails_safely(self):
        invoice = self.create()
        # A short invoice now fits one page; use genuinely long terms to test the cap.
        invoice.terms_conditions = "Terms retained without clipping. " * 200
        invoice.save(update_fields=["terms_conditions"])
        with pdfplumber.open(BytesIO(self.download(invoice).content)) as pdf:
            self.assertGreater(len(pdf.pages), 1)
        with patch("invoices.pdf_generator.MAX_PAGES", 1):
            self.assertEqual(self.download(invoice).status_code, 422)

    def test_render_failure_never_leaks_exception_content(self):
        invoice = self.create()
        with patch("invoices.pdf_views.generate_invoice_pdf", side_effect=RuntimeError("PRIVATE ACCOUNT /etc/secret")):
            response = self.download(invoice)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(b"PRIVATE", response.content)
        self.assertNotIn(b"secret", response.content)

    def test_preloaded_service_has_no_database_queries_or_writes(self):
        invoice = self.loaded(self.create())
        with self.assertNumQueries(0):
            first = generate_invoice_pdf(invoice)
            second = render_invoice_pdf(invoice_pdf_data(invoice))
        self.assertEqual(first, second)

    def test_invoice_pages_expose_pdf_actions(self):
        invoice = self.create()
        self.assertContains(self.client.get(reverse("invoice_detail", args=[invoice.pk])), "Preview PDF")
        self.finalize(invoice)
        self.assertContains(self.client.get(reverse("invoice_detail", args=[invoice.pk])), "Download PDF")
