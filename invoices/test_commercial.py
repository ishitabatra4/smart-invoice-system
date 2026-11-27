from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import smtplib
from threading import Barrier
from unittest.mock import patch
import uuid

import pdfplumber
import pypdfium2 as pdfium
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import close_old_connections, IntegrityError, OperationalError, transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import BusinessParty
from analytics.services import build_analytics_context
from audit.models import AuditEvent
from . import test_manual as fixtures, email_delivery
from .models import CommercialDocument, CommercialLineItem, CommercialNumberSequence, CommercialEmailDelivery, Invoice, InvoicePayment, InvoiceNumberSequence
from .commercial_services import convert_commercial, save_commercial, stored_inputs, HEADER_FIELDS, TOTAL_FIELDS
from .commercial_pdf import generate_commercial_pdf
from .ledger import party_invoices

D = Decimal


class CommercialHelpers:
    def payload(self, kind="quotation", **updates):
        page = self.client.get(reverse("commercial_create", args=[kind]))
        self.assertEqual(page.status_code, 200)
        today = timezone.localdate()
        data = {"creation_token": page.context["form"].initial["creation_token"], "party": self.customer.pk,
            "invoice_date": today.isoformat(), "due_date": (today + timedelta(days=30)).isoformat(),
            "supplier_state_code": "27", "place_of_supply": "27", "pricing_mode": "exclusive", "supply_category": "standard",
            "invoice_discount_type": "fixed", "invoice_discount_value": "0", "adjustment": "0",
            "payment_terms": "Estimate only", "notes": "Commercial test", "terms_conditions": "Subject to review",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "100",
            "items-0-description": "Example service", "items-0-hsn_sac_code": "9983", "items-0-quantity": "1",
            "items-0-unit": "Nos", "items-0-rate": "1000", "items-0-discount": "0", "items-0-discount_type": "fixed",
            "items-0-tax_rate": "18", "items-0-tax_treatment": "taxable"}
        data.update(updates)
        return data

    def create(self, kind="quotation", **updates):
        response = self.client.post(reverse("commercial_create", args=[kind]), self.payload(kind, **updates))
        self.assertEqual(response.status_code, 302, str(response.context["form"].errors) if response.context else response.content[:300])
        return CommercialDocument.objects.get(pk=int(response.url.rstrip('/').rsplit('/', 1)[1]))

    def transition(self, document, action, **data):
        response = self.client.post(reverse("commercial_transition", args=[document.pk, action]), data)
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        return document

    def accept(self, document):
        self.transition(document, "issue")
        return self.transition(document, "accept", notes="Decision recorded by business owner")

    def request(self):
        request = RequestFactory().post('/commercial/convert/')
        request.user = self.user
        return request

    def convert(self, document, **data):
        data.setdefault("invoice_date", timezone.localdate().isoformat())
        return self.client.post(reverse("commercial_convert", args=[document.pk]), data)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEFAULT_FROM_EMAIL="business@example.test")
class CommercialTests(CommercialHelpers, TestCase):
    def setUp(self):
        fixtures.ManualInvoiceTests.setUp(self)
        self.customer.email = "customer@example.test"
        self.customer.save()

    def test_quotation_and_proforma_separate_numbering_no_invoice_number(self):
        quote, proforma = self.create(), self.create("proforma")
        self.assertTrue(quote.number.startswith("QT/"))
        self.assertTrue(proforma.number.startswith("PF/"))
        self.assertTrue(quote.number.endswith("000001"))
        self.assertTrue(proforma.number.endswith("000001"))
        self.assertEqual(CommercialNumberSequence.objects.count(), 2)
        self.assertFalse(InvoiceNumberSequence.objects.exists())
        self.assertFalse(Invoice.objects.exists())

    def test_numbering_business_scoped(self):
        first = self.create()
        self.client.force_login(self.other)
        original = self.customer
        self.customer = self.other_customer
        other = self.create()
        self.customer = original
        self.assertEqual(first.number, other.number)
        self.assertNotEqual(first.business_id, other.business_id)

    def test_creation_replay_does_not_allocate_twice(self):
        data = self.payload()
        url = reverse("commercial_create", args=["quotation"])
        one, two = self.client.post(url, data), self.client.post(url, data)
        self.assertEqual(one.url, two.url)
        self.assertEqual(CommercialDocument.objects.count(), 1)
        self.assertEqual(CommercialNumberSequence.objects.get().next_value, 2)

    def test_edit_draft_recalculates_and_audits(self):
        document = self.create()
        response = self.client.post(reverse("commercial_edit", args=[document.pk]), self.payload(**{"items-0-rate": "2000"}))
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        self.assertEqual(document.total_amount, D("2360"))
        self.assertTrue(AuditEvent.objects.filter(module="commercial", action="update", object_id=str(document.pk)).exists())
        self.assertEqual(CommercialNumberSequence.objects.get().next_value, 2)

    def test_dates_customer_quantities_rates_discounts_and_tax_validation(self):
        for changes in ({"due_date": (timezone.localdate()-timedelta(days=1)).isoformat()}, {"party": self.other_customer.pk},
            {"items-0-quantity": "0"}, {"items-0-rate": "-1"}, {"items-0-discount": "1001"},
            {"items-0-tax_rate": "17"}, {"items-0-tax_treatment": "exempt"}, {"place_of_supply": ""}, {"supply_category": "sez"}):
            with self.subTest(changes=changes):
                response = self.client.post(reverse("commercial_create", args=["quotation"]), self.payload(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(CommercialDocument.objects.exists())

    def test_hidden_financial_status_currency_and_number_are_ignored(self):
        document = self.create(status="accepted", currency="USD", total_amount="0.01", cgst_amount="9999", invoice_number="FORGED")
        self.assertEqual(document.status, "draft")
        self.assertEqual(document.currency, "INR")
        self.assertEqual(document.total_amount, D("1180"))
        self.assertNotEqual(document.number, "FORGED")

    def test_exclusive_intra_and_interstate_gst(self):
        intra, inter = self.create(), self.create(place_of_supply="29")
        self.assertEqual((intra.taxable_amount, intra.cgst_amount, intra.sgst_amount), (D("1000"),D("90"),D("90")))
        self.assertEqual((inter.cgst_amount, inter.igst_amount), (D("0"),D("180")))

    def test_inclusive_line_and_invoice_percentage_discounts(self):
        document = self.create(pricing_mode="inclusive", invoice_discount_type="percent", invoice_discount_value="10",
            **{"items-0-rate": "1180", "items-0-discount_type": "percent", "items-0-discount": "10"})
        self.assertEqual((document.taxable_amount, document.total_amount), (D("810"),D("955.80")))
        self.assertEqual(document.line_items.get().discount, D("118"))
        self.assertEqual(document.invoice_discount_amount, D("106.20"))

    def test_mixed_rates_and_fixed_allocation(self):
        document = self.create(invoice_discount_value="100", **{"items-TOTAL_FORMS": "2", "items-1-description": "Second", "items-1-quantity": "1", "items-1-unit":"Nos", "items-1-rate":"1000", "items-1-discount":"0", "items-1-tax_rate":"5"})
        self.assertEqual(document.taxable_amount, D("1900"))
        self.assertEqual(document.total_amount, D("2118.50"))
        self.assertEqual([line.invoice_discount_allocated for line in document.line_items.all()], [D("50"),D("50")])

    def test_decimal_rounding_and_distinct_treatments(self):
        tiny = self.create(**{"items-0-rate":"0.03"})
        self.assertEqual((tiny.taxable_amount,tiny.total_amount),(D("0.03"),D("0.04")))
        for treatment in ("taxable", "exempt", "nil_rated", "non_gst"):
            document = self.create(**{"items-0-tax_rate":"0", "items-0-tax_treatment":treatment})
            self.assertEqual(document.treatment_breakdown[treatment], "1000.00")
            self.assertEqual(document.total_amount, D("1000"))

    def test_preview_matches_save_and_has_no_write(self):
        data = self.payload(pricing_mode="inclusive", **{"items-0-rate":"1180"})
        response = self.client.post(reverse("commercial_calculate", args=["quotation"]), data)
        self.assertEqual(response.status_code,200)
        self.assertFalse(CommercialDocument.objects.exists())
        self.client.post(reverse("commercial_create", args=["quotation"]),data)
        self.assertEqual(CommercialDocument.objects.get().total_amount, D(response.json()["total_amount"]))

    def test_explicit_issuance_locks_edit_and_does_not_send(self):
        document = self.create()
        self.client.get(reverse("commercial_detail", args=[document.pk]))
        document.refresh_from_db();self.assertEqual(document.status,"draft")
        self.transition(document,"issue")
        self.assertIsNotNone(document.issued_at)
        self.assertEqual(document.status,"sent")
        self.assertEqual(self.client.get(reverse("commercial_edit", args=[document.pk])).status_code,400)
        self.assertEqual(len(mail.outbox),0)

    def test_issue_rejects_stale_snapshots_and_tampered_saved_amounts(self):
        document = self.create()
        CommercialLineItem.objects.filter(document=document).update(cgst_amount=D("0"))
        self.transition(document,"issue")
        self.assertEqual(document.status,"draft")
        second = self.create()
        self.customer.name="Changed";self.customer.save()
        self.transition(second,"issue")
        self.assertEqual(second.status,"draft")

    def test_acceptance_and_rejection_record_business_actor(self):
        document = self.accept(self.create())
        self.assertEqual(document.status,"accepted")
        self.assertEqual(document.decision_by,self.user)
        self.assertIsNotNone(document.decision_at)
        self.assertIn("business owner",document.decision_notes)
        rejected=self.create();self.transition(rejected,"issue");self.transition(rejected,"reject")
        self.assertEqual(rejected.status,"rejected")
        self.assertTrue(AuditEvent.objects.filter(module="commercial",description__contains="recorded by business user").exists())

    def test_invalid_transitions_and_cancellation_preserve_history(self):
        document=self.create()
        self.transition(document,"accept");self.assertEqual(document.status,"draft")
        self.transition(document,"expired");self.assertEqual(document.status,"draft")
        self.transition(document,"cancel");self.assertEqual(document.status,"cancelled")
        self.transition(document,"issue");self.assertEqual(document.status,"cancelled")
        self.assertEqual(CommercialDocument.objects.count(),1)
        self.assertTrue(self.create().number.endswith("000002"))

    def test_expiry_local_date_boundary_and_no_read_mutation(self):
        today=timezone.localdate()
        document=self.create(due_date=today.isoformat())
        self.transition(document,"issue")
        self.assertEqual(document.effective_status,"sent")
        with patch("invoices.commercial_models.timezone.localdate",return_value=today+timedelta(days=1)):
            self.assertEqual(document.effective_status,"expired")
            self.transition(document,"accept");self.assertEqual(document.status,"sent")
            self.assertEqual(self.client.get(reverse("commercial_email",args=[document.pk])).status_code,400)
            self.assertContains(self.client.get(reverse("commercial_list"),{"status":"expired"}),document.number)
        document.refresh_from_db();self.assertEqual(document.status,"sent")

    def test_pdf_labels_saved_totals_and_no_accounting_payment_sections(self):
        for kind,label in (("quotation","QUOTATION"),("proforma","PROFORMA INVOICE")):
            document=self.create(kind)
            response=self.client.get(reverse("commercial_pdf",args=[document.pk]))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response["Content-Type"],"application/pdf")
            self.assertIn("no-store",response["Cache-Control"])
            with pdfplumber.open(BytesIO(response.content)) as pdf:
                self.assertAlmostEqual(pdf.pages[0].width,595.28,places=1)
                text="\n".join(page.extract_text() or "" for page in pdf.pages)
            for value in (label,"DRAFT",document.number,"1,180.00","Not a tax invoice","estimates"):
                self.assertIn(value,text)
            for value in ("TAX INVOICE","Current payment summary","Outstanding:"):
                self.assertNotIn(value,text)
            self.assertFalse(Invoice.objects.exists())

    def test_cancelled_and_expired_draft_pdf_marking(self):
        document=self.create();self.transition(document,"cancel")
        with pdfplumber.open(BytesIO(generate_commercial_pdf(document))) as pdf:
            self.assertIn("CANCELLED",pdf.pages[0].extract_text())
        draft=self.create()
        with patch("invoices.commercial_models.timezone.localdate",return_value=draft.valid_until+timedelta(days=1)):
            with pdfplumber.open(BytesIO(generate_commercial_pdf(draft))) as pdf:
                text="\n".join(page.extract_text() or "" for page in pdf.pages)
            self.assertIn("DRAFT",text);self.assertIn("expired",text)

    def test_issued_snapshot_pdf_stable_despite_directory_edits_no_recalculation(self):
        document=self.create();self.transition(document,"issue")
        before=generate_commercial_pdf(document)
        BusinessParty.objects.filter(pk=self.customer.pk).update(name="New customer",address_line="New address")
        self.business.business_name="New business";self.business.save()
        document.refresh_from_db()
        with patch("invoices.manual_calculations.calculate_invoice",side_effect=AssertionError("No recalculation")):
            after=generate_commercial_pdf(document)
        self.assertEqual(before,after)

    def test_multipage_long_descriptions_and_literal_markup(self):
        document=self.create()
        source=document.line_items.get()
        values={f.name:getattr(source,f.name) for f in source._meta.fields if f.name not in {"id","document"}}
        CommercialLineItem.objects.bulk_create([CommercialLineItem(document=document,**{**values,"description":'<img src="file:///etc/passwd"> '+"Wrapped text "*30}) for _ in range(50)])
        content=generate_commercial_pdf(document)
        with pdfplumber.open(BytesIO(content)) as pdf:
            self.assertGreater(len(pdf.pages),2)
            self.assertIn("Example service",pdf.pages[0].extract_text())
            repeated=[page for page in pdf.pages if "Wrapped text" in (page.extract_text() or "")]
            for page in repeated:
                self.assertIn("Description /",page.extract_text())
                self.assertFalse(page.hyperlinks)
        pdf=pdfium.PdfDocument(content)
        try:
            page=pdf[0];bitmap=page.render(scale=1);self.assertGreater(bitmap.width,500);bitmap.close();page.close()
        finally:pdf.close()

    def test_accept_then_convert_both_types_to_one_unfinalized_invoice(self):
        for kind in ("quotation","proforma"):
            document=self.accept(self.create(kind))
            saved={key:getattr(document,key) for key in TOTAL_FIELDS}
            lines=list(document.line_items.values())
            first,second=self.convert(document),self.convert(document)
            self.assertEqual(first.status_code,302);self.assertEqual(first.url,second.url)
            document.refresh_from_db();invoice=document.converted_invoice
            self.assertEqual(document.status,"converted")
            self.assertEqual(invoice.document_state,"draft");self.assertEqual(invoice.status,"draft")
            self.assertIsNone(invoice.finalized_at)
            self.assertEqual(invoice.buyer_party,self.customer)
            self.assertEqual(invoice.party_snapshot,document.party_snapshot)
            self.assertEqual(invoice.line_items.get().description,document.line_items.get().description)
            self.assertEqual(invoice.total_amount,document.total_amount)
            self.assertEqual({key:getattr(document,key) for key in TOTAL_FIELDS},saved)
            self.assertEqual(list(document.line_items.values()),lines)
        self.assertEqual(Invoice.objects.count(),2);self.assertFalse(InvoicePayment.objects.exists())

    def test_conversion_uses_current_rules_and_exposes_snapshot_differences(self):
        document=self.accept(self.create())
        self.customer.address_line="Changed current address";self.customer.save()
        response=self.convert(document,invoice_date=(timezone.localdate()+timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code,302)
        document.refresh_from_db()
        self.assertTrue(any(item["field"]=="Customer address_line" for item in document.conversion_changes))
        self.assertNotEqual(document.party_snapshot,document.converted_invoice.party_snapshot)
        page=self.client.get(response.url)
        self.assertContains(page,"Converted from")
        self.assertContains(page,"Changed current address")
        self.assertContains(page,"Review this invoice draft")

    def test_conversion_rejects_newly_unsupported_rate_atomically(self):
        document=self.accept(self.create())
        with override_settings(GST_ALLOWED_RATES=["0","5"]):
            self.convert(document)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(InvoiceNumberSequence.objects.exists())
        document.refresh_from_db();self.assertEqual(document.status,"accepted")

    def test_invalid_and_expired_conversion_and_inactive_customer_are_rejected(self):
        document=self.create();self.convert(document);self.assertFalse(Invoice.objects.exists())
        self.accept(document)
        with patch("invoices.commercial_models.timezone.localdate",return_value=document.valid_until+timedelta(days=1)):
            self.convert(document);self.assertFalse(Invoice.objects.exists())
        self.customer.is_active=False;self.customer.save()
        self.convert(document);self.assertFalse(Invoice.objects.exists())

    def test_conversion_link_constraint_prevents_shared_invoice(self):
        one=self.accept(self.create());two=self.accept(self.create())
        self.convert(one);one.refresh_from_db()
        with self.assertRaises(IntegrityError),transaction.atomic():
            CommercialDocument.objects.filter(pk=two.pk).update(status="converted",converted_invoice=one.converted_invoice)

    def test_no_accounting_effect_before_or_after_draft_conversion(self):
        today=timezone.localdate()
        before=build_analytics_context(self.user,today,today,"custom")["totals"]
        document=self.accept(self.create("proforma"));self.convert(document)
        after=build_analytics_context(self.user,today,today,"custom")["totals"]
        self.assertEqual(before,after)
        self.assertEqual(party_invoices(self.user,self.customer),[])
        self.assertFalse(InvoicePayment.objects.exists())
        self.assertEqual(self.client.get(reverse("reconciliation_list")).status_code,200)

    def test_email_confirmation_actual_message_pdf_and_history(self):
        for kind,label in (("quotation","Quotation"),("proforma","Proforma Invoice")):
            document=self.create(kind);self.transition(document,"issue")
            url=reverse("commercial_email",args=[document.pk])
            before_count=len(mail.outbox)
            page=self.client.get(url)
            self.assertEqual(page.status_code,200)
            self.assertEqual(len(mail.outbox),before_count)
            data={"send_token":page.context["form"].initial["send_token"],"recipient":"customer@example.test","confirm":"on"}
            self.assertEqual(self.client.post(url,data).status_code,302)
            self.assertEqual(self.client.post(url,data).status_code,302)
            message=mail.outbox[-1]
            self.assertEqual(message.subject,f"{label} {document.number} from Manual business")
            self.assertEqual(message.to,["customer@example.test"])
            self.assertIn("not a tax invoice",message.body)
            self.assertIn("estimates",message.alternatives[0].content)
            self.assertEqual(message.attachments[0].mimetype,"application/pdf")
            self.assertEqual(message.attachments[0].content,generate_commercial_pdf(document))
            self.assertEqual(document.email_deliveries.count(),1)
            self.assertEqual(document.email_deliveries.get().status,"sent")
            document.refresh_from_db();self.assertEqual(document.status,"sent")
            self.assertIsNone(document.decision_at)
        self.assertEqual(len(mail.outbox),2)

    def test_email_failure_retry_history_and_no_customer_acceptance(self):
        document=self.create();self.transition(document,"issue")
        values=dict(user=self.user,document_id=document.pk,recipient="customer@example.test")
        with patch("django.core.mail.EmailMultiAlternatives.send",side_effect=smtplib.SMTPAuthenticationError(535,b"SECRET")):
            result=email_delivery.send_commercial_email(**values,request_key=uuid.uuid4())
        self.assertEqual(result.status,"failed")
        with patch.object(email_delivery,"COOLDOWN",timedelta(0)):
            retried=email_delivery.send_commercial_email(**values,request_key=uuid.uuid4(),retry_id=result.delivery_id)
        self.assertEqual(retried.status,"sent")
        self.assertEqual(document.email_deliveries.count(),2)
        self.assertEqual(document.email_deliveries.get(pk=result.delivery_id).status,"failed")
        document.refresh_from_db();self.assertEqual(document.status,"sent");self.assertIsNone(document.decision_at)

    def test_email_draft_rejected_and_uncertain_does_not_issue_or_accept(self):
        document=self.create()
        self.assertEqual(self.client.get(reverse("commercial_email",args=[document.pk])).status_code,400)
        with self.assertRaises(ValidationError):
            email_delivery.send_commercial_email(user=self.user,document_id=document.pk,recipient="customer@example.test",request_key=uuid.uuid4())
        self.transition(document,"issue");issued=document.issued_at
        with patch("django.core.mail.EmailMultiAlternatives.send",side_effect=TimeoutError("SECRET")):
            result=email_delivery.send_commercial_email(user=self.user,document_id=document.pk,recipient="customer@example.test",request_key=uuid.uuid4())
        self.assertEqual(result.status,"uncertain")
        document.refresh_from_db();self.assertEqual(document.issued_at,issued);self.assertIsNone(document.decision_at)
        self.assertEqual(self.client.get(reverse("commercial_email_retry",args=[document.pk,result.delivery_id])).status_code,400)

    def test_cross_business_every_document_endpoint_and_customer(self):
        document=self.create();self.transition(document,"issue")
        self.client.force_login(self.other)
        for name in ("commercial_detail","commercial_edit","commercial_pdf","commercial_email"):
            self.assertEqual(self.client.get(reverse(name,args=[document.pk])).status_code,404,name)
        self.assertEqual(self.client.post(reverse("commercial_transition",args=[document.pk,"accept"])).status_code,404)
        self.assertEqual(self.client.post(reverse("commercial_convert",args=[document.pk]),{"invoice_date":timezone.localdate()}).status_code,404)
        self.assertNotContains(self.client.get(reverse("commercial_list")),document.number)

    def test_csrf_methods_token_scope_and_unauthenticated(self):
        document=self.create();client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        self.assertEqual(client.post(reverse("commercial_transition",args=[document.pk,"issue"])).status_code,403)
        self.assertEqual(self.client.get(reverse("commercial_transition",args=[document.pk,"issue"])).status_code,405)
        self.assertEqual(self.client.get(reverse("commercial_convert",args=[document.pk])).status_code,405)
        self.assertEqual(self.client.get(reverse("commercial_calculate",args=["quotation"])).status_code,405)
        data=self.payload();data['creation_token']='invalid'
        self.assertEqual(self.client.post(reverse("commercial_create",args=["quotation"]),data).status_code,200)
        self.client.logout();self.assertEqual(self.client.get(reverse("commercial_pdf",args=[document.pk])).status_code,302)

    def test_pagination_search_filters_sort_and_feature_gate(self):
        document=self.create()
        for index in range(26):
            data={field.name:getattr(document,field.name) for field in document._meta.fields if field.name not in {"id","created_at","updated_at","creation_key","number"}}
            CommercialDocument.objects.create(**data,number=f"QT/EXAMPLE/{index}")
        page=self.client.get(reverse("commercial_list"),{"sort":"number"})
        self.assertEqual(len(page.context['page']),25)
        self.assertTrue(page.context['page'].has_next())
        page=self.client.get(reverse("commercial_list"),{"q":document.number,"kind":"quotation","status":"draft"})
        self.assertEqual(len(page.context['page']),1)
        with patch("invoices.commercial_views.commercial_available",return_value=False):
            self.assertContains(self.client.get(reverse("commercial_list")),"pending database migration")
            self.assertEqual(self.client.get(reverse("commercial_pdf",args=[document.pk])).status_code,503)

    def test_only_finalized_converted_invoice_enters_financial_totals(self):
        document=self.accept(self.create())
        self.create("proforma")
        response=self.convert(document)
        document.refresh_from_db();invoice=document.converted_invoice
        self.client.post(reverse("invoice_finalize",args=[invoice.pk]))
        invoice.refresh_from_db();self.assertEqual(invoice.document_state,"finalized")
        today=timezone.localdate()
        totals=build_analytics_context(self.user,today,today,"custom")["totals"]
        self.assertEqual(totals["sales"],invoice.total_amount)
        self.assertEqual(totals["output_gst"],D("180"))
        self.assertEqual(len(party_invoices(self.user,self.customer)),1)
        self.assertFalse(InvoicePayment.objects.exists())

    def test_shared_email_business_limit_across_commercial_and_invoice(self):
        document=self.accept(self.create())
        result=email_delivery.send_commercial_email(user=self.user,document_id=document.pk,recipient="customer@example.test",request_key=uuid.uuid4())
        self.assertEqual(result.status,"sent")
        self.convert(document);document.refresh_from_db();invoice=document.converted_invoice
        self.client.post(reverse("invoice_finalize",args=[invoice.pk]))
        with patch.object(email_delivery,"HOURLY_BUSINESS_LIMIT",1),self.assertRaises(ValidationError):
            email_delivery.send_invoice_email(user=self.user,invoice_id=invoice.pk,recipient="customer@example.test",request_key=uuid.uuid4())
        self.assertEqual(len(mail.outbox),1)

    def test_commercial_email_recipient_and_token_scope(self):
        document=self.create();self.transition(document,"issue")
        url=reverse("commercial_email",args=[document.pk]);page=self.client.get(url)
        data={"send_token":page.context["form"].initial["send_token"],"recipient":"different@example.test","confirm":"on"}
        self.assertEqual(self.client.post(url,data).status_code,200)
        self.assertFalse(CommercialEmailDelivery.objects.exists())
        data["confirm_override"]="on"
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(mail.outbox[0].to,["different@example.test"])
        self.customer.refresh_from_db();self.assertEqual(self.customer.email,"customer@example.test")
        another=self.create();self.transition(another,"issue")
        self.assertEqual(self.client.post(reverse("commercial_email",args=[another.pk]),data).status_code,200)
        self.assertEqual(len(mail.outbox),1)

    def test_commercial_retry_limit_and_authorization(self):
        document=self.create();self.transition(document,"issue")
        args=dict(user=self.user,document_id=document.pk,recipient="customer@example.test")
        with patch.object(email_delivery,"COOLDOWN",timedelta(0)),patch("django.core.mail.EmailMultiAlternatives.send",return_value=0):
            result=email_delivery.send_commercial_email(**args,request_key=uuid.uuid4())
            first_id=result.delivery_id
            for _ in range(2):
                result=email_delivery.send_commercial_email(**args,request_key=uuid.uuid4(),retry_id=result.delivery_id)
            with self.assertRaises(ValidationError):
                email_delivery.send_commercial_email(**args,request_key=uuid.uuid4(),retry_id=result.delivery_id)
        self.assertEqual(document.email_deliveries.count(),3)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("commercial_email_retry",args=[document.pk,first_id])).status_code,404)

    def test_preview_rejects_foreign_or_issued_id_and_posts_only(self):
        document=self.create();data=self.payload(document_id=str(document.pk))
        self.transition(document,"issue")
        url=reverse("commercial_calculate",args=["quotation"])
        self.assertEqual(self.client.post(url,data).status_code,404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url,data).status_code,404)

    def test_document_date_cannot_move_reserved_number_to_different_fy(self):
        document=self.create()
        next_year=document.year_start.replace(year=document.year_start.year+1)
        response=self.client.post(reverse("commercial_edit",args=[document.pk]),self.payload(invoice_date=next_year.isoformat(),due_date=(next_year+timedelta(days=30)).isoformat()))
        self.assertEqual(response.status_code,200)
        document.refresh_from_db();self.assertNotEqual(document.document_date,next_year)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CommercialConcurrencyTests(CommercialHelpers, TransactionTestCase):
    def setUp(self):
        fixtures.ManualInvoiceTests.setUp(self)

    def test_concurrent_conversion_produces_one_linked_draft(self):
        document=self.accept(self.create())
        barrier=Barrier(2)
        def convert(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return convert_commercial(request=self.request(),document_id=document.pk,invoice_date=timezone.localdate()).pk
            except OperationalError:return None
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(convert,[0,1]))
        invoice=convert_commercial(request=self.request(),document_id=document.pk,invoice_date=timezone.localdate())
        self.assertEqual(Invoice.objects.count(),1)
        document.refresh_from_db();self.assertEqual(document.converted_invoice_id,invoice.pk)
        self.assertEqual(invoice.document_state,"draft")

    def test_concurrent_creation_keeps_commercial_numbers_unique(self):
        source=self.create()
        data={key:getattr(source,key) for key in HEADER_FIELDS}
        data.update(party=self.customer,invoice_date=source.document_date,due_date=source.valid_until,adjustment=source.other_charges)
        rows=stored_inputs(source);barrier=Barrier(2)
        def create(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return save_commercial(request=self.request(),kind="quotation",data=data,line_data=rows,creation_key=uuid.uuid4()).pk
            except OperationalError:return None
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(create,[0,1]))
        self.assertTrue(any(results))
        numbers=list(CommercialDocument.objects.values_list("number",flat=True))
        self.assertEqual(len(numbers),len(set(numbers)))
        self.assertFalse(InvoiceNumberSequence.objects.exists())
