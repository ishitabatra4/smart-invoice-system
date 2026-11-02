
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Invoice(models.Model):

    INVOICE_TYPES = [
        ("sales", "Sales Invoice"),
        ("purchase", "Purchase Invoice"),
        ("expense", "Expense Bill"),
    ]

    STATUS_CHOICES = [
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
            models.UniqueConstraint(
                fields=["user", "supplier_gstin", "invoice_number"],
                condition=~Q(supplier_gstin=""),
                name="unique_invoice_supplier_gstin_number",
            ),
        ]

        indexes = [
            models.Index(fields=["user", "invoice_date"]),
            models.Index(fields=["user", "invoice_type", "status"]),
            models.Index(fields=["supplier_party", "invoice_date"]),
            models.Index(fields=["buyer_party", "invoice_date"]),
        ]

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

    @property
    def calculated_tax(self):
        return (
            self.taxable_amount * self.tax_rate / Decimal("100")
        ).quantize(Decimal("0.01"))

    @property
    def calculated_taxable_amount(self):
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
