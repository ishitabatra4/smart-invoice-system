import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateExpenseStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(location=settings.PRIVATE_EXPENSE_ROOT, base_url=None)


private_expense_storage = PrivateExpenseStorage()


def expense_attachment_path(instance, filename):
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"expenses/{instance.invoice.user_id}/{uuid.uuid4().hex}.{extension}"


class ExpenseCategory(models.Model):
    business = models.ForeignKey(
        "accounts.BusinessProfile",
        on_delete=models.CASCADE,
        related_name="expense_categories",
    )
    name = models.CharField(max_length=100)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="unique_expense_category_per_business"),
        ]

    def __str__(self):
        return self.name


class ExpenseDetail(models.Model):
    """Expense-only metadata attached to the existing financial Invoice record."""

    invoice = models.OneToOneField(
        "invoices.Invoice",
        on_delete=models.CASCADE,
        related_name="expense_detail",
    )
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    reference_number = models.CharField(max_length=100, blank=True)
    discount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    gst_inclusive = models.BooleanField(default=False)
    attachment = models.FileField(
        upload_to=expense_attachment_path,
        storage=private_expense_storage,
        blank=True,
    )
    attachment_display_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-invoice__invoice_date", "-created_at"]

    def clean(self):
        if self.invoice_id and self.invoice.invoice_type not in {"expense", "purchase"}:
            raise ValidationError({"invoice": "Expense metadata requires an expense or purchase invoice."})
        if (
            self.invoice_id
            and self.category_id
            and self.invoice.user_id != self.category.business.user_id
        ):
            raise ValidationError({"category": "The category must belong to the invoice owner's business."})

    def __str__(self):
        return f"{self.title} ({self.invoice.invoice_number})"


class RecurringExpense(models.Model):
    FREQUENCIES = [
        ("weekly", "Weekly"), ("monthly", "Monthly"),
        ("quarterly", "Quarterly"), ("yearly", "Yearly"),
    ]
    business = models.ForeignKey("accounts.BusinessProfile", on_delete=models.CASCADE, related_name="recurring_expenses")
    title = models.CharField(max_length=200)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="recurring_expenses")
    supplier = models.ForeignKey("accounts.BusinessParty", on_delete=models.SET_NULL, null=True, blank=True, related_name="recurring_expenses")
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    gst_inclusive = models.BooleanField(default=False)
    supplier_state_code = models.CharField(max_length=2, null=True, blank=True)
    place_of_supply = models.CharField(max_length=2, null=True, blank=True)
    supply_category = models.CharField(max_length=16, null=True, blank=True)
    tax_treatment = models.CharField(max_length=12, null=True, blank=True)

    @property
    def gst_review_required(self):
        if not self.supplier_state_code or not self.place_of_supply or self.supply_category != "standard" or not self.tax_treatment:
            return True
        from .recurrence import _schedule_calculation
        try:
            _schedule_calculation(self)
        except ValidationError:
            return True
        return False

    frequency = models.CharField(max_length=12, choices=FREQUENCIES)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    next_due_date = models.DateField()
    schedule_day = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    is_cancelled = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.category_id and self.category.business_id != self.business_id:
            raise ValidationError({"category": "Choose a category from this business."})
        if self.supplier_id and self.supplier.business_id != self.business_id:
            raise ValidationError({"supplier": "Choose a supplier from this business."})
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be earlier than start date."})
        if self.next_due_date and self.start_date and self.next_due_date < self.start_date:
            raise ValidationError({"next_due_date": "Next due date cannot be earlier than start date."})


class RecurringExpenseGeneration(models.Model):
    recurring_expense = models.ForeignKey(RecurringExpense, on_delete=models.PROTECT, related_name="generations")
    period_key = models.CharField(max_length=10)
    invoice = models.OneToOneField("invoices.Invoice", on_delete=models.PROTECT, related_name="recurrence_generation")
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["recurring_expense", "period_key"], name="unique_recurring_expense_period",
        )]


class ExpenseITCReview(models.Model):
    STATUSES = [('pending','Pending review'),('potential','Potentially eligible'),('ineligible','Ineligible'),('confirmed','Confirmed by owner'),('reversed','Reversed')]
    invoice = models.ForeignKey('invoices.Invoice',on_delete=models.PROTECT,related_name='itc_reviews')
    business = models.ForeignKey('accounts.BusinessProfile',on_delete=models.PROTECT)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    status = models.CharField(max_length=12,choices=STATUSES)
    reason = models.TextField(max_length=2000)
    fingerprint = models.CharField(max_length=64)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-pk']
        indexes=[models.Index(fields=['business','invoice','created_at'],name='expense_itc_review_idx')]
        constraints=[models.CheckConstraint(condition=models.Q(status__in=['pending','potential','ineligible','confirmed','reversed']),name='expense_itc_valid_status')]
    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('ITC review history is append-only.')
        if self.invoice.user_id!=self.reviewer_id or self.business.user_id!=self.reviewer_id:raise ValidationError('Choose an owned invoice and business.')
        super().save(*args,**kwargs)


class ExpenseAttachmentRevision(models.Model):
    invoice = models.ForeignKey('invoices.Invoice',on_delete=models.PROTECT,related_name='expense_attachments')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    storage_kind = models.CharField(max_length=10,choices=[('private','Private expense'),('invoice','Original invoice')])
    file_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-pk']
        constraints=[models.UniqueConstraint(fields=['invoice','storage_kind','file_name'],name='expense_original_revision_unique')]
    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('Receipt history is append-only.')
        if self.invoice.user_id!=self.actor_id:raise ValidationError('Choose an owned invoice.')
        super().save(*args,**kwargs)
