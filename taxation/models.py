from decimal import Decimal

from django.conf import settings
from django.db import models


class ReconciliationRun(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="gstr2b_runs")
    return_period = models.CharField(max_length=7, help_text="GST return period in YYYY-MM format")
    source_file = models.FileField(upload_to="reconciliation/%Y/%m/")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"GSTR-2B {self.return_period} ({self.created_at:%Y-%m-%d %H:%M})"


class ReconciliationRecord(models.Model):
    STATUS_CHOICES = [
        ("matched", "Exact Match"),
        ("tolerance_match", "Match within tolerance"),
        ("mismatch", "Mismatch"),
        ("unmatched", "Unmatched"),
        ("manual_review", "Manual review required"),
    ]
    run = models.ForeignKey(ReconciliationRun, on_delete=models.CASCADE, related_name="records")
    supplier_gstin = models.CharField(max_length=15)
    invoice_number = models.CharField(max_length=100, help_text="Original value from GSTR-2B")
    invoice_date = models.DateField()
    taxable_value = models.DecimalField(max_digits=14, decimal_places=2)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_tax = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unmatched")
    matched_invoice = models.ForeignKey("invoices.Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="reconciliation_records")
    differences = models.JSONField(default=dict, blank=True)
    manually_matched_at = models.DateTimeField(null=True, blank=True)
    manually_matched_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="manual_reconciliation_matches")

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.matched_invoice_id and self.run_id:
            if self.matched_invoice.user_id != self.run.user_id or self.matched_invoice.invoice_type != "purchase":
                raise ValidationError({"matched_invoice": "Choose a purchase invoice owned by this account."})

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "supplier_gstin", "invoice_number", "invoice_date"], name="unique_gstr2b_record_per_run")]
        ordering = ["invoice_date", "supplier_gstin", "invoice_number"]


class ReconciliationAction(models.Model):
    record = models.ForeignKey(ReconciliationRecord, on_delete=models.CASCADE, related_name="actions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=20, choices=[("match", "Manual match"), ("unmatch", "Manual unmatch")])
    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

from .preparation_models import TaxPeriod,TaxWorkingPaper,TaxWorkingPaperEvent
