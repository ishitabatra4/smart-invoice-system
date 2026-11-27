
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Value
from django.db.models.functions import Lower, Trim
from django.db.models.lookups import GreaterThan


NORMALIZATION_WHITESPACE = " \t\n\r\v\f"


class InvoiceBatch(models.Model):
    """A user-owned upload batch; failures are retained for review and audit."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invoice_batches")
    invoice_type = models.CharField(max_length=20, choices=[
        ("sales", "Sales Invoice"), ("purchase", "Purchase Invoice"), ("expense", "Expense Bill"),
    ])
    file_count = models.PositiveIntegerField(default=0)
    failed_files = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.get_invoice_type_display()} batch {self.pk}"


class Invoice(models.Model):

    INVOICE_TYPES = [
        ("sales", "Sales Invoice"),
        ("purchase", "Purchase Invoice"),
        ("expense", "Expense Bill"),
    ]

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("uploaded", "Uploaded"),
        ("processing", "Processing"),
        ("review", "Needs Review"),
        ("verified", "Verified"),
        ("paid", "Paid"),
        ("cancelled", "Cancelled"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="invoices",
    )
    batch = models.ForeignKey(
        InvoiceBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices",
    )

    # Links to the business's vendor and customer directory
    supplier_party = models.ForeignKey(
        "accounts.BusinessParty",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplied_invoices",
    )

    buyer_party = models.ForeignKey(
        "accounts.BusinessParty",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchased_invoices",
    )

    invoice_type = models.CharField(
        max_length=20,
        choices=INVOICE_TYPES,
        default="purchase",
    )

    invoice_number = models.CharField(max_length=100)
    @property
    def source_dates(self):
        from .date_evidence import source_dates
        return source_dates(self)

    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)

    supplier_name = models.CharField(max_length=255)
    supplier_gstin = models.CharField(max_length=15, blank=True)

    buyer_name = models.CharField(max_length=255, blank=True)
    buyer_gstin = models.CharField(max_length=15, blank=True)

    taxable_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    cgst_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    sgst_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    igst_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    other_charges = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="uploaded",
    )

    entry_mode = models.CharField(max_length=10, choices=[("legacy", "Existing record"), ("uploaded", "Uploaded"), ("manual", "Manual")], default="legacy")
    document_state = models.CharField(max_length=12, choices=[("legacy", "Existing workflow"), ("draft", "Draft"), ("finalized", "Finalized"), ("cancelled", "Cancelled")], default="legacy")
    creation_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    numbering_year_start = models.DateField(null=True, blank=True, editable=False)
    supplier_state_code = models.CharField(max_length=2, blank=True)
    place_of_supply = models.CharField(max_length=2, blank=True)
    payment_terms = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    terms_conditions = models.TextField(blank=True)
    business_snapshot = models.JSONField(default=dict, blank=True)
    party_snapshot = models.JSONField(default=dict, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True, editable=False)
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False)

    calculation_version = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    pricing_mode = models.CharField(max_length=10, null=True, blank=True, choices=[("exclusive", "GST exclusive"), ("inclusive", "GST inclusive")])
    supply_category = models.CharField(max_length=16, null=True, blank=True)
    invoice_discount_type = models.CharField(max_length=10, null=True, blank=True)
    invoice_discount_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    invoice_discount_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_discount_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    treatment_breakdown = models.JSONField(null=True, blank=True)

    source_file = models.FileField(
        upload_to="invoices/%Y/%m/",
        blank=True,
    )

    extracted_text = models.TextField(blank=True)

    validation_errors = models.JSONField(default=list, blank=True)

    is_duplicate = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-invoice_date", "-created_at"]

        constraints = [
            models.CheckConstraint(condition=~Q(entry_mode="manual") | Q(document_state="draft", status="draft") | Q(document_state="finalized", status__in=["verified", "paid"]) | Q(document_state="cancelled", status="cancelled"), name="manual_document_status_consistent"),
            models.UniqueConstraint("user", Lower(Trim("invoice_number", chars=NORMALIZATION_WHITESPACE)), condition=Q(entry_mode="manual", invoice_type="sales"), name="unique_manual_sales_number"),
            models.UniqueConstraint(
                "user",
                "invoice_type",
                Lower(Trim("buyer_gstin", chars=NORMALIZATION_WHITESPACE)),
                Lower(Trim("invoice_number", chars=NORMALIZATION_WHITESPACE)),
                condition=Q(invoice_type="sales")
                & GreaterThan(
                    Trim("buyer_gstin", chars=NORMALIZATION_WHITESPACE),
                    Value(""),
                ),
                name="unique_sales_buyer_invoice_number",
            ),
            models.UniqueConstraint(
                "user",
                "invoice_type",
                Lower(Trim("supplier_gstin", chars=NORMALIZATION_WHITESPACE)),
                Lower(Trim("invoice_number", chars=NORMALIZATION_WHITESPACE)),
                condition=Q(invoice_type__in=["purchase", "expense"])
                & GreaterThan(
                    Trim("supplier_gstin", chars=NORMALIZATION_WHITESPACE),
                    Value(""),
                ),
                name="unique_purchase_expense_supplier_invoice_number",
            ),
        ]

        indexes = [
            models.Index(fields=["user", "invoice_date"]),
            models.Index(fields=["user", "invoice_type", "status"]),
            models.Index(fields=["supplier_party", "invoice_date"]),
            models.Index(fields=["buyer_party", "invoice_date"]),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        errors = {}
        for field in ("supplier_party", "buyer_party"):
            party = getattr(self, field, None)
            if party and party.business.user_id != self.user_id:
                errors[field] = "Choose a party from the invoice owner's business."
        if self.batch_id and self.batch.user_id != self.user_id:
            errors["batch"] = "Choose an upload batch from the invoice owner's account."
        if self.entry_mode == "manual":
            valid_states = {f"{value:02}" for value in (*range(1, 39), 97)}
            if self.invoice_type not in {"sales", "purchase"}:
                errors["invoice_type"] = "Manual documents must be sales or purchase invoices."
            for name in ("supplier_state_code", "place_of_supply"):
                if getattr(self, name) not in valid_states:
                    errors[name] = "Enter a valid state code."
            if self.supplier_gstin and self.supplier_state_code != self.supplier_gstin[:2]:
                errors["supplier_state_code"] = "Supplier state must match its GSTIN."
            if self.invoice_date and self.due_date and self.due_date < self.invoice_date:
                errors["due_date"] = "Due date cannot be earlier than invoice date."
            if self.other_charges is not None and abs(self.other_charges) > Decimal("1.00"):
                errors["other_charges"] = "Round-off adjustment cannot exceed one rupee."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.invoice_number} - {self.supplier_name}"

    @property
    def total_tax(self):
        return (
            self.cgst_amount
            + self.sgst_amount
            + self.igst_amount
        )

    @property
    def calculated_total(self):
        return (
            self.taxable_amount
            + self.total_tax
            + self.other_charges
        )

    @property
    def total_mismatch(self):
        return self.total_amount != self.calculated_total


class InvoiceLineItem(models.Model):

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name="line_items",
    )

    description = models.CharField(max_length=500)
    hsn_sac_code = models.CharField(max_length=20, blank=True)

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("1.000"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )

    unit = models.CharField(max_length=30, blank=True)

    rate = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    discount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])

    discount_type = models.CharField(max_length=10, null=True, blank=True)
    discount_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    tax_treatment = models.CharField(max_length=12, null=True, blank=True)
    invoice_discount_allocated = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    taxable_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    tax_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.description} - Invoice {self.invoice.invoice_number}"

    def _gst_basis(self):
        from .manual_calculations import money
        return money(self.quantity*self.rate)-self.discount-(self.invoice_discount_allocated or Decimal("0"))

    @property
    def calculated_tax(self):
        if self.invoice.calculation_version == 1:
            from .manual_calculations import base_amounts
            return base_amounts(self._gst_basis(), self.tax_rate, self.invoice.pricing_mode)["tax_amount"]
        if self.invoice.entry_mode == "manual":
            from .manual_calculations import money
            return money(self.taxable_amount * self.tax_rate / Decimal("100"))
        return (
            self.taxable_amount * self.tax_rate / Decimal("100")
        ).quantize(Decimal("0.01"))

    @property
    def calculated_taxable_amount(self):
        if self.invoice.calculation_version == 1:
            from .manual_calculations import base_amounts
            return base_amounts(self._gst_basis(), self.tax_rate, self.invoice.pricing_mode)["taxable_amount"]
        if self.invoice.entry_mode == "manual":
            from .manual_calculations import money
            return money(self.quantity * self.rate) - self.discount
        return (
            self.quantity * self.rate
        ).quantize(Decimal("0.01"))

    @property
    def calculated_line_total(self):
        return self.taxable_amount + self.tax_amount

    @property
    def taxable_amount_mismatch(self):
        return (
            self.taxable_amount != self.calculated_taxable_amount
        )

    @property
    def tax_amount_mismatch(self):
        return self.tax_amount != self.calculated_tax


class InvoicePayment(models.Model):
    PAYMENT_MODES = [
        ("cash", "Cash"),
        ("bank_transfer", "Bank Transfer"),
        ("upi", "UPI"),
        ("cheque", "Cheque"),
        ("other", "Other"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invoice_payments")
    party = models.ForeignKey("accounts.BusinessParty", on_delete=models.PROTECT, related_name="payments")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    payment_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_MODES)
    reference_id = models.CharField(max_length=100)
    reference_id_normalized = models.CharField(max_length=100, editable=False, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["payment_date", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "reference_id_normalized"],
                condition=~Q(reference_id_normalized=""),
                name="unique_invoice_payment_reference",
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        self.reference_id_normalized = (self.reference_id or "").strip(
            NORMALIZATION_WHITESPACE
        ).lower()
        if self.invoice_id and self.user_id and self.invoice.user_id != self.user_id:
            raise ValidationError({"invoice": "The invoice must belong to your account."})
        if self.party_id and self.user_id and self.party.business.user_id != self.user_id:
            raise ValidationError({"party": "The party must belong to your business."})
        if self.invoice_id and self.party_id:
            if self.invoice.invoice_type not in {"sales", "purchase", "expense"}:
                raise ValidationError({"invoice": "Payments can only be recorded for sales, purchase, or expense invoices."})
            if self.invoice.status not in {"verified", "paid"}:
                raise ValidationError({"invoice": "Only verified invoices can receive payments."})
            linked_party_id = (
                self.invoice.buyer_party_id if self.invoice.invoice_type == "sales"
                else self.invoice.supplier_party_id
            )
            if linked_party_id and linked_party_id != self.party_id:
                raise ValidationError({"party": "The selected party does not match this invoice."})

    def __str__(self):
        return f"Payment {self.reference_id} for {self.invoice.invoice_number}"

    def save(self, *args, **kwargs):
        self.reference_id_normalized = (self.reference_id or "").strip(
            NORMALIZATION_WHITESPACE
        ).lower()
        return super().save(*args, **kwargs)


class InvoiceNumberSequence(models.Model):
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT)
    year_start = models.DateField()
    next_value = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "year_start"], name="unique_sales_sequence_year")]


class EmailAttemptFields(models.Model):
    """One immutable request identity per attempt; backend acceptance is not inbox delivery."""
    STATUS_CHOICES = [("pending", "Pending"), ("sending", "Sending / outcome not yet confirmed"),
                      ("sent", "Accepted by backend — inbox unconfirmed"), ("failed", "Failed"),
                      ("uncertain", "Uncertain — check before resending")]
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    request_key = models.UUIDField(unique=True, editable=False)
    retry_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="retry_attempt")
    attempt_number = models.PositiveSmallIntegerField(default=1)
    recipient = models.EmailField(max_length=254)
    subject = models.CharField(max_length=255)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    backend = models.CharField(max_length=255)
    error_category = models.CharField(max_length=40, blank=True)
    attachment_sha256 = models.CharField(max_length=64, blank=True)
    attachment_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class InvoiceEmailDelivery(EmailAttemptFields):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="email_deliveries")
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.PROTECT, related_name="invoice_email_deliveries")

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["business", "created_at"], name="email_business_created_idx"),
                   models.Index(fields=["invoice", "created_at"], name="email_invoice_created_idx")]


from .commercial_models import CommercialDocument, CommercialLineItem, CommercialNumberSequence, CommercialEmailDelivery

from .adjustment_models import InvoiceAdjustmentNote, InvoiceAdjustmentLine, AdjustmentNoteSequence, AdjustmentNoteEmailDelivery

from .extraction_models import InvoiceExtractionAttempt, InvoiceExtractionReview
