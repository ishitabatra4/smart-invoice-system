from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from .models import AuditEventQuerySet
SEVERITIES = [(x, x.title()) for x in ('INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')]
ALERT_STATUSES = [(x, x.title()) for x in ('OPEN', 'ACKNOWLEDGED', 'INVESTIGATING', 'RESOLVED', 'DISMISSED')]

class RetainedQuerySet(models.QuerySet):

    def delete(self):
        raise ValidationError('Security records are retained.')

class RetainedRecord(models.Model):
    objects = AuditEventQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Security evidence is append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Security evidence is retained.')

class SecurityRecord(RetainedRecord):
    event = models.OneToOneField('audit.AuditEvent', on_delete=models.PROTECT, related_name='security_record')
    sequence = models.PositiveBigIntegerField(unique=True)
    event_type = models.CharField(max_length=64)
    severity = models.CharField(max_length=8, choices=SEVERITIES)
    correlation_id = models.UUIDField(null=True)
    principal = models.CharField(max_length=64, blank=True)
    network = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=32, default='application')
    previous_hash = models.CharField(max_length=64, unique=True)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-sequence']
        constraints = [models.CheckConstraint(condition=Q(sequence__gte=1), name='security_sequence_positive'), models.CheckConstraint(condition=Q(severity__in=[x[0] for x in SEVERITIES]), name='security_record_severity')]
        indexes = [models.Index(fields=['event_type', 'created_at'], name='security_type_date_idx'), models.Index(fields=['principal', 'created_at'], name='security_principal_date_idx'), models.Index(fields=['severity', 'created_at'], name='security_severity_date_idx')]

class SecurityAlert(models.Model):
    objects = RetainedQuerySet.as_manager()
    fingerprint = models.CharField(max_length=64, unique=True)
    rule_id = models.CharField(max_length=40)
    business = models.ForeignKey('accounts.BusinessProfile', null=True, on_delete=models.PROTECT)
    severity = models.CharField(max_length=8, choices=SEVERITIES)
    status = models.CharField(max_length=16, choices=ALERT_STATUSES, default='OPEN')
    evidence = models.JSONField(default=list)
    summary = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-pk']
        permissions = [('manage_security', 'Manage platform security alerts and rule configuration')]
        constraints = [models.CheckConstraint(condition=Q(status__in=[x[0] for x in ALERT_STATUSES]), name='security_alert_status'), models.CheckConstraint(condition=Q(severity__in=[x[0] for x in SEVERITIES]), name='security_alert_severity')]
        indexes = [models.Index(fields=['business', 'status', 'created_at'], name='security_alert_scope_idx')]

    def delete(self, *args, **kwargs):
        raise ValidationError('Alerts are retained; resolve or dismiss with a reason.')

class SecurityAlertHistory(RetainedRecord):
    alert = models.ForeignKey(SecurityAlert, on_delete=models.PROTECT, related_name='history')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=ALERT_STATUSES)
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.CheckConstraint(condition=Q(status__in=[x[0] for x in ALERT_STATUSES]), name='security_history_status')]

class SecurityRuleConfiguration(models.Model):
    objects = RetainedQuerySet.as_manager()
    rule_id = models.CharField(max_length=40, unique=True)
    threshold = models.PositiveIntegerField()
    window_seconds = models.PositiveIntegerField()
    enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(threshold__gte=2, threshold__lte=1000), name='security_rule_threshold'), models.CheckConstraint(condition=Q(window_seconds__gte=60, window_seconds__lte=3600), name='security_rule_window')]

    def delete(self, *args, **kwargs):
        raise ValidationError('Disable a rule explicitly; configuration history is retained in audit.')

class SessionSecurityRecord(models.Model):
    objects = RetainedQuerySet.as_manager()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    business = models.ForeignKey('accounts.BusinessProfile', null=True, on_delete=models.PROTECT)
    token_digest = models.CharField(max_length=64, unique=True)
    browser = models.CharField(max_length=16, default='Unknown')
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField()
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-last_seen', '-pk']
        indexes = [models.Index(fields=['user', 'revoked_at', 'expires_at'], name='security_session_user_idx')]

    def delete(self, *args, **kwargs):
        raise ValidationError('Session observations are retained.')
