from config.admin_security import BusinessScopedAdmin

from django.contrib import admin
from .models import BusinessProfile


@admin.register(BusinessProfile)
class BusinessProfileAdmin(BusinessScopedAdmin):
    owner_lookup = "user"
    list_display = (
        "business_name",
        "user",
        "business_type",
        "gstin",
        "city",
        "created_at",
    )

    search_fields = (
        "business_name",
        "legal_name",
        "user__username",
        "gstin",
        "pan",
    )

    list_filter = ("business_type", "state", "created_at")
    readonly_fields = ("created_at", "updated_at")
