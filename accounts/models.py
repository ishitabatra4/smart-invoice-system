
from django.conf import settings
from django.db import models


class BusinessProfile(models.Model):
    BUSINESS_TYPES = [
        ("proprietorship", "Sole Proprietorship"),
        ("partnership", "Partnership"),
        ("llp", "Limited Liability Partnership (LLP)"),
        ("private_limited", "Private Limited Company"),
        ("public_limited", "Public Limited Company"),
        ("individual", "Individual"),
        ("other", "Other"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="business_profile",
    )

    business_name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)
    business_type = models.CharField(
        max_length=30,
        choices=BUSINESS_TYPES,
        default="proprietorship",
    )

    gstin = models.CharField(
        max_length=15,
        unique=True,
        null=True,
        blank=True,
    )
    pan = models.CharField(max_length=10, blank=True)

    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=15, blank=True)

    address_line = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=6, blank=True)

    financial_year_start_month = models.PositiveSmallIntegerField(
        default=4,
        help_text="April is 4; India's usual financial year starts in April.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["business_name"]
        verbose_name = "Business Profile"
        verbose_name_plural = "Business Profiles"

    def __str__(self):
        return f"{self.business_name} ({self.user.username})"

class BusinessParty(models.Model):

    PARTY_TYPES = [
        ("vendor", "Vendor"),
        ("customer", "Customer"),
        ("both", "Vendor & Customer"),
    ]

    business = models.ForeignKey(
        BusinessProfile,
        on_delete=models.CASCADE,
        related_name="parties",
    )

    party_type = models.CharField(
        max_length=20,
        choices=PARTY_TYPES,
        default="vendor",
    )

    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)

    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)

    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=15, blank=True)

    address_line = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=6, blank=True)

    is_active = models.BooleanField(default=True)

    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "gstin"],
                condition=~models.Q(gstin=""),
                name="unique_business_party_gstin",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_party_type_display()})"

class AccountVerification(models.Model):
    """Only new registrations require verification; legacy users are unchanged."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    email = models.EmailField(unique=True)
    verified_at = models.DateTimeField(null=True, blank=True)
