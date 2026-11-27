"""Separate, immutable-at-finalization adjustment documents; original invoices stay intact."""
import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, F
from django.db.models.functions import Round
from django.db.models.lookups import Exact
from .models import EmailAttemptFields


class InvoiceAdjustmentNote(models.Model):
    KINDS = [("credit", "Credit Note"), ("debit", "Debit Note")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT)
    original_invoice = models.ForeignKey("invoices.Invoice", on_delete=models.PROTECT, related_name="adjustment_notes")
    party = models.ForeignKey("accounts.BusinessParty", on_delete=models.PROTECT)
    kind = models.CharField(max_length=6, choices=KINDS)
    direction = models.CharField(max_length=8, choices=[("issued","Issued"),("received","Received")])
    number = models.CharField(max_length=16)
    supplier_reference = models.CharField(max_length=16, blank=True)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    year_start = models.DateField()
    document_date = models.DateField()
    reason = models.CharField(max_length=500)
    tax_effect = models.CharField(max_length=12, choices=[("gst","Value and GST"),("financial","Financial only; no GST adjustment")], default="gst")
    status = models.CharField(max_length=10, choices=[("draft","Draft"),("finalized","Finalized"),("cancelled","Cancelled")], default="draft")
    currency = models.CharField(max_length=3, default="INR", editable=False)
    original_snapshot = models.JSONField(default=dict)
    eligibility_reviewed = models.BooleanField(default=False)
    annual_return_date = models.DateField(null=True, blank=True)
    pricing_mode = models.CharField(max_length=10, choices=[("exclusive", "GST exclusive"), ("inclusive", "GST inclusive")])
    supply_category = models.CharField(max_length=16, default="standard")
    supplier_state_code = models.CharField(max_length=2)
    place_of_supply = models.CharField(max_length=2)
    calculation_version = models.PositiveSmallIntegerField(default=1, editable=False)
    invoice_discount_type = models.CharField(max_length=10, default="fixed")
    invoice_discount_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    invoice_discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    treatment_breakdown = models.JSONField(default=dict)
    business_snapshot = models.JSONField(default=dict)
    party_snapshot = models.JSONField(default=dict)
    payment_terms = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    terms_conditions = models.TextField(blank=True)
    finalized_at = models.DateTimeField(null=True, editable=False)
    cancelled_at = models.DateTimeField(null=True, editable=False)
    cancellation_date = models.DateField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-document_date", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["business","number"], name="adjustment_unique_number"),
            models.UniqueConstraint(fields=["party","kind","year_start","supplier_reference"], condition=Q(direction="received"), name="adjustment_supplier_reference"),
            models.CheckConstraint(condition=Q(kind__in=["credit","debit"]), name="adjustment_valid_kind"),
            models.CheckConstraint(condition=Q(direction__in=["issued","received"]), name="adjustment_valid_direction"),
            models.CheckConstraint(condition=Q(tax_effect__in=["gst","financial"]), name="adjustment_valid_tax_effect"),
            models.CheckConstraint(condition=Q(currency="INR",total_amount__gt=0,taxable_amount__gte=0,cgst_amount__gte=0,sgst_amount__gte=0,igst_amount__gte=0,other_charges=0), name="adjustment_positive_amounts"),
            models.CheckConstraint(condition=Exact(Round(F("total_amount"),2),Round(F("taxable_amount")+F("cgst_amount")+F("sgst_amount")+F("igst_amount"),2)), name="adjustment_components_sum"),
            models.CheckConstraint(condition=Q(status="draft",finalized_at__isnull=True,cancelled_at__isnull=True)|Q(status="finalized",finalized_at__isnull=False,cancelled_at__isnull=True)|Q(status="cancelled",cancelled_at__isnull=False), name="adjustment_valid_lifecycle"),
        ]
        indexes = [models.Index(fields=["business","document_date","status"], name="adjustment_business_date_idx")]

    def clean(self):
        if self.business_id and self.business.user_id != self.user_id:
            raise ValidationError("Choose your own business.")
        if self.original_invoice_id and self.original_invoice.user_id != self.user_id:
            raise ValidationError("Choose an invoice belonging to your business.")
        if self.party_id and self.party.business_id != self.business_id:
            raise ValidationError("Choose a party belonging to your business.")

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.finalized_at:
                allowed = {"status", "cancelled_at", "cancellation_date", "cancellation_reason", "updated_at"}
                if any(getattr(old, f.attname) != getattr(self, f.attname) for f in self._meta.fields if f.name not in allowed):
                    raise ValidationError("Finalized financial values and snapshots are immutable.")
                if old.status == "cancelled" or self.status not in {"finalized", "cancelled"}:
                    raise ValidationError("Finalized notes cannot be reopened.")
        super().save(*args, **kwargs)

    @property
    def invoice_number(self): return self.number
    @property
    def invoice_date(self): return self.document_date
    @property
    def due_date(self): return None
    @property
    def buyer_party(self): return self.party
    @property
    def buyer_party_id(self): return self.party_id


class InvoiceAdjustmentLine(models.Model):
    document = models.ForeignKey(InvoiceAdjustmentNote, on_delete=models.CASCADE, related_name="line_items")
    original_line = models.ForeignKey("invoices.InvoiceLineItem", on_delete=models.PROTECT)
    mode = models.CharField(max_length=8, choices=[("quantity","Quantity"),("value","Value only")])
    input_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    description = models.CharField(max_length=500)
    hsn_sac_code = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit = models.CharField(max_length=30, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=2)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_type = models.CharField(max_length=10, default="fixed")
    discount_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_treatment = models.CharField(max_length=12, default="taxable")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    invoice_discount_allocated = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2)

    def clean(self):
        if self.original_line_id and self.document_id and self.original_line.invoice_id != self.document.original_invoice_id:
            raise ValidationError("Choose a line from the original invoice.")

    def save(self, *args, **kwargs):
        if InvoiceAdjustmentNote.objects.get(pk=self.document_id).status != "draft":
            raise ValidationError("Finalized adjustment lines cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if InvoiceAdjustmentNote.objects.get(pk=self.document_id).status != "draft":
            raise ValidationError("Finalized adjustment lines cannot be deleted.")
        return super().delete(*args, **kwargs)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["document","original_line"], name="adjustment_unique_source_line"),
            models.CheckConstraint(condition=Q(mode="quantity",quantity__gt=0)|Q(mode="value",quantity=0), name="adjustment_line_mode"),
            models.CheckConstraint(condition=Q(rate__gte=0,discount__gte=0,taxable_amount__gte=0,cgst_amount__gte=0,sgst_amount__gte=0,igst_amount__gte=0,total_amount__gt=0), name="adjustment_line_nonnegative")]


class AdjustmentNoteSequence(models.Model):
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT)
    kind = models.CharField(max_length=6, choices=InvoiceAdjustmentNote.KINDS)
    direction = models.CharField(max_length=8)
    year_start = models.DateField()
    next_value = models.PositiveBigIntegerField(default=1)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["business","kind","direction","year_start"], name="adjustment_unique_sequence")]


class AdjustmentNoteEmailDelivery(EmailAttemptFields):
    document = models.ForeignKey(InvoiceAdjustmentNote, on_delete=models.PROTECT, related_name="email_deliveries")
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT)
    class Meta:
        indexes = [models.Index(fields=["business","created_at"], name="adjustment_email_business_idx")]
