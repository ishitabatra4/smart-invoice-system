from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from datetime import date
from decimal import Decimal

from accounts.models import BusinessProfile
from audit.models import AuditEvent
from invoices.models import Invoice
from .models import ReconciliationRecord, ReconciliationRun


class ReconciliationAuditTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reconcile-audit", password="secret")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Reconciliation Audit Co")
        self.client.force_login(self.user)
        self.invoice = Invoice.objects.create(
            user=self.user, invoice_type="purchase", invoice_number="GST-100",
            invoice_date=date(2026, 10, 12), supplier_name="Supplier",
            supplier_gstin="27ABCDE1234F1Z5", taxable_amount=Decimal("100.00"),
            cgst_amount=Decimal("9.00"), sgst_amount=Decimal("9.00"),
            total_amount=Decimal("118.00"), status="verified",
        )
        self.run = ReconciliationRun.objects.create(user=self.user, return_period="2026-10", source_file="reconciliation/test.json")
        self.record = ReconciliationRecord.objects.create(
            run=self.run, supplier_gstin=self.invoice.supplier_gstin,
            invoice_number=self.invoice.invoice_number, invoice_date=self.invoice.invoice_date,
            taxable_value=self.invoice.taxable_amount, cgst_amount=self.invoice.cgst_amount,
            sgst_amount=self.invoice.sgst_amount, total_tax=Decimal("18.00"),
        )

    def test_manual_match_and_unmatch_are_audited_and_scoped(self):
        response = self.client.post(reverse("reconciliation_match", args=[self.run.pk, self.record.pk]), {"invoice_id": self.invoice.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(business=self.business, module="taxation", action="match", object_id=str(self.record.pk)).exists())
        response = self.client.post(reverse("reconciliation_unmatch", args=[self.run.pk, self.record.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(business=self.business, module="taxation", action="unmatch", object_id=str(self.record.pk)).exists())
