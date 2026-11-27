"""Non-accounting commercial documents; deliberately separate from Invoice/payment tables."""
from decimal import Decimal
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, F
from django.utils import timezone

from .models import EmailAttemptFields


class CommercialDocument(models.Model):
    KINDS = [("quotation", "Quotation"), ("proforma", "Proforma Invoice")]
    STATUSES = [(value, value.title()) for value in ("draft", "sent", "accepted", "rejected", "converted", "cancelled")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT)
    customer = models.ForeignKey("accounts.BusinessParty", on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=KINDS)
    number = models.CharField(max_length=100)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    year_start = models.DateField(editable=False)
    document_date = models.DateField()
    valid_until = models.DateField()
    currency = models.CharField(max_length=3, default="INR", editable=False)
    status = models.CharField(max_length=12, choices=STATUSES, default="draft")
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
    issued_at = models.DateTimeField(null=True, blank=True, editable=False)
    decision_at = models.DateTimeField(null=True, blank=True, editable=False)
    decision_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="commercial_decisions")
    decision_notes = models.TextField(blank=True)
    converted_invoice = models.OneToOneField("invoices.Invoice", null=True, blank=True, on_delete=models.PROTECT, related_name="commercial_source")
    conversion_changes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-document_date", "-pk"]
        constraints = [models.UniqueConstraint(fields=["business", "kind", "number"], name="unique_commercial_number"),
            models.CheckConstraint(condition=Q(valid_until__gte=F("document_date")), name="commercial_validity_order"),
            models.CheckConstraint(condition=Q(currency="INR"), name="commercial_currency_inr"),
            models.CheckConstraint(condition=Q(kind__in=["quotation", "proforma"]), name="commercial_valid_kind"),
            models.CheckConstraint(condition=Q(status__in=["draft", "sent", "accepted", "rejected", "converted", "cancelled"]), name="commercial_valid_status"),
            models.CheckConstraint(condition=(Q(status="converted", converted_invoice__isnull=False) | (~Q(status="converted") & Q(converted_invoice__isnull=True))), name="commercial_conversion_link"),
            models.CheckConstraint(condition=Q(total_amount__gt=0), name="commercial_positive_total")]
        indexes = [models.Index(fields=["business", "kind", "document_date"], name="commercial_business_date_idx")]

    def clean(self):
        if self.business_id and self.business.user_id != self.user_id:
            raise ValidationError("The document must belong to its business owner.")
        if self.customer_id and self.customer.business_id != self.business_id:
            raise ValidationError("Choose a customer from this business.")
        if self.converted_invoice_id and self.converted_invoice.user_id != self.user_id:
            raise ValidationError("The converted invoice must belong to this account.")

    @property
    def effective_status(self):
        if self.status in {"draft", "sent", "accepted"} and self.valid_until < timezone.localdate():
            return "expired"
        return self.status

    @property
    def invoice_number(self):
        return self.number

    @property
    def invoice_date(self):
        return self.document_date

    @property
    def due_date(self):
        return self.valid_until

    @property
    def buyer_party_id(self):
        return self.customer_id

    @property
    def buyer_party(self):
        return self.customer

    @property
    def supplier_name(self):
        return self.business_snapshot.get("name", "")

    @property
    def buyer_name(self):
        return self.party_snapshot.get("name", "")


class CommercialLineItem(models.Model):
    document = models.ForeignKey(CommercialDocument, on_delete=models.CASCADE, related_name="line_items")
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

    class Meta:
        ordering = ["pk"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0, rate__gte=0, discount__gte=0, taxable_amount__gte=0, tax_amount__gte=0, total_amount__gte=0), name="commercial_line_positive")]


class CommercialNumberSequence(models.Model):
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=CommercialDocument.KINDS)
    year_start = models.DateField()
    next_value = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "kind", "year_start"], name="unique_commercial_sequence")]


class CommercialEmailDelivery(EmailAttemptFields):
    document = models.ForeignKey(CommercialDocument, on_delete=models.PROTECT, related_name="email_deliveries")
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT, related_name="commercial_email_deliveries")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["business", "created_at"], name="commercial_email_business_idx"),
                   models.Index(fields=["document", "created_at"], name="commercial_email_document_idx")]
