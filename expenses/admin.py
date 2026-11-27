from config.admin_security import BusinessScopedAdmin
from django.contrib import admin

from .models import ExpenseCategory, ExpenseDetail


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(BusinessScopedAdmin):
    list_display = ("name", "business", "is_default", "is_active", "updated_at")
    list_filter = ("is_default", "is_active")
    search_fields = ("name", "business__business_name")


@admin.register(ExpenseDetail)
class ExpenseDetailAdmin(BusinessScopedAdmin):
    owner_lookup = "invoice__user"
    readonly_fields = tuple(field.name for field in ExpenseDetail._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    list_display = ("title", "invoice", "category", "updated_at")
    list_filter = ("category",)
    search_fields = ("title", "invoice__invoice_number", "invoice__supplier_name")
