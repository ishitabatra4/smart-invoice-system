from django.conf import settings
from django.db import models


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Audit events are append-only and cannot be modified.")

    def delete(self):
        raise ValueError("Audit events are append-only and cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("Audit events are append-only and cannot be modified.")


class AuditEventManager(models.Manager.from_queryset(AuditEventQuerySet)):
    pass


class AuditEvent(models.Model):
    """Append-only business activity record; stores deltas, never full objects."""

    ACTION_CHOICES = [
        ("create", "Create"), ("update", "Update"), ("verify", "Verify"),
        ("cancel", "Cancel"), ("delete", "Delete"), ("delete_attempt", "Delete attempt"),
        ("payment", "Payment"), ("payment_reverse", "Payment reversal"),
        ("upload", "Upload"), ("match", "Manual match"), ("unmatch", "Manual unmatch"),
        ("export", "Export"), ("login", "Login"), ("logout", "Logout"),
        ("failed_login", "Failed login"), ("security", "Security event"),
    ]
    business = models.ForeignKey(
        "accounts.BusinessProfile", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_events",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_events",
    )
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    module = models.CharField(max_length=40)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=100, blank=True)
    related_object_type = models.CharField(max_length=80, blank=True)
    related_object_id = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=500)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    success = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AuditEventManager()

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["business", "created_at"], name="audit_business_date_idx"),
            models.Index(fields=["business", "module", "action"], name="audit_biz_module_action_idx"),
            models.Index(fields=["user", "created_at"], name="audit_user_date_idx"),
            models.Index(fields=["success", "action", "created_at"], name="audit_security_date_idx"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Audit events are append-only and cannot be modified.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit events are append-only and cannot be deleted.")

    def __str__(self):
        return f"{self.module}: {self.action} ({self.object_id or '—'})"

from .security_models import SecurityRecord, SecurityAlert, SecurityAlertHistory, SecurityRuleConfiguration, SessionSecurityRecord
from .security_models import SEVERITIES, ALERT_STATUSES
