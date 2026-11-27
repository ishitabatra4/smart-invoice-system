from config.admin_security import BusinessScopedAdmin
from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(BusinessScopedAdmin):
    list_display = ("created_at", "business", "actor_label", "module", "action", "object_id", "success")
    list_filter = ("module", "action", "success", "created_at")
    search_fields = ("description", "object_id", "actor_label")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
