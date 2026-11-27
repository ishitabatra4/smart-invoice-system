"""Typed links to the existing audit log; deterministic detection, no external calls."""
import hashlib
import json
import re
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError, ImproperlyConfigured
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac
from .models import SecurityRecord, SecurityAlert, SecurityAlertHistory, SecurityRuleConfiguration, SessionSecurityRecord
RULES = {'AUTH_FAILURE_BURST': {'types': ['AUTH_LOGIN_FAILURE'],
                        'threshold': 5,
                        'window': 900,
                        'severity': 'HIGH',
                        'scope': 'principal',
                        'description': 'Repeated failed sign-in attempts; shared accounts/typing '
                                       'mistakes can cause false positives.'},
 'ACCESS_DENIAL_BURST': {'types': ['ACCESS_PERMISSION_DENIED',
                                   'ACCESS_RESOURCE_NOT_FOUND',
                                   'ACCESS_INVALID_REQUEST',
                                   'FILE_ACCESS_DENIED'],
                         'threshold': 5,
                         'window': 600,
                         'severity': 'HIGH',
                         'scope': 'principal',
                         'description': 'Repeated denials/not-found responses; stale links and '
                                        'automated navigation can cause false positives. Foreign '
                                        'record existence is never asserted.'},
 'EXPORT_BURST': {'types': ['EXPORT_GENERATED'],
                  'threshold': 20,
                  'window': 600,
                  'severity': 'MEDIUM',
                  'scope': 'principal',
                  'description': 'Frequent generated exports; legitimate reporting batches can '
                                 'trigger this rule.'},
 'UPLOAD_REJECTION_BURST': {'types': ['FILE_UPLOAD_REJECTED'],
                            'threshold': 5,
                            'window': 600,
                            'severity': 'MEDIUM',
                            'scope': 'principal',
                            'description': 'Repeated rejected uploads; damaged documents can cause '
                                           'false positives.'},
 'FINANCIAL_CHANGE_BURST': {'types': ['FINANCIAL_RECORD_MODIFIED',
                                      'FINANCIAL_RECORD_FINALIZED',
                                      'FINANCIAL_ADJUSTMENT_CREATED'],
                            'threshold': 50,
                            'window': 600,
                            'severity': 'MEDIUM',
                            'scope': 'principal',
                            'description': 'High recorded financial activity; legitimate bulk work '
                                           'can cause false positives.'},
 'AUTH_THROTTLE_BURST': {'types': ['AUTH_REQUEST_THROTTLED'],
                         'threshold': 5,
                         'window': 900,
                         'severity': 'MEDIUM',
                         'scope': 'network',
                         'description': 'Repeated existing request throttles; shared networks can '
                                        'cause false positives. This is not an account lock.'}}

def ready(request=None):
    key = '_security_schema_ready'
    if request is not None and hasattr(request, key):
        return getattr(request, key)
    required = {m._meta.db_table for m in (SecurityRecord, SecurityAlert, SecurityAlertHistory, SecurityRuleConfiguration, SessionSecurityRecord)}
    tables = set(connection.introspection.table_names())
    present = required & tables
    result = required <= tables
    if not result:
        from django.db.migrations.recorder import MigrationRecorder
        applied = 'django_migrations' in tables and MigrationRecorder.Migration.objects.filter(app='audit', name='0004_securityalert_securityalerthistory_securityrecord_and_more').exists()
        if present or applied:
            raise ImproperlyConfigured('Security monitoring schema is incomplete; restore or repair before financial writes.')
    if request is not None:
        setattr(request, key, result)
    return result

def administrator(user):
    return bool(user.is_authenticated and user.is_active and user.is_staff and (user.is_superuser or user.has_perm('audit.manage_security')))

def privacy_key(value):
    return salted_hmac('audit.security.principal.v1', str(value).strip().lower()).hexdigest()

def session_digest(key):
    return hashlib.sha256(('session-monitor-v1:' + str(key)).encode()).hexdigest()

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)

def event_payload(event):
    fields = ('id', 'business_id', 'user_id', 'actor_label', 'action', 'module', 'object_type', 'object_id', 'related_object_type', 'related_object_id', 'description', 'before', 'after', 'ip_address', 'user_agent', 'success', 'metadata')
    data = {field: getattr(event, field) for field in fields}
    data['created_at'] = event.created_at.isoformat(timespec='microseconds')
    return data

def record_digest(record, event):
    data = {key: getattr(record, key) for key in ('sequence', 'event_type', 'severity', 'principal', 'network', 'source', 'previous_hash')}
    data['correlation_id'] = str(record.correlation_id) if record.correlation_id else None
    data['event'] = event_payload(event)
    data['created_at'] = record.created_at.isoformat(timespec='microseconds')
    return hashlib.sha256(canonical(data).encode()).hexdigest()

def taxonomy(event):
    if event.action in ('login', 'logout', 'failed_login'):
        return {'login': 'AUTH_LOGIN_SUCCESS', 'logout': 'AUTH_LOGOUT', 'failed_login': 'AUTH_LOGIN_FAILURE'}[event.action]
    if event.action == 'security' and (not event.success):
        return 'ACCESS_PERMISSION_DENIED'
    event_name = event.metadata.get('event', '')
    if isinstance(event_name, str) and event_name.startswith(('invoice_email_', 'commercial_email_', 'adjustment_email_')):
        return 'DOCUMENT_EMAIL_ACTIVITY'
    if event.action == 'export':
        return 'EXPORT_GENERATED'
    if event.module == 'taxation' and event.object_type == 'taxation.taxworkingpaper':
        return 'TAX_WORKING_PAPER_REVIEWED' if event.action == 'verify' else 'TAX_WORKING_PAPER_PREPARED' if event.action == 'create' else 'TAX_WORKING_PAPER_UPDATED'
    if event.module == 'extraction' and event.action == 'update':
        return 'OCR_REVIEW_SAVED'
    if event.module == 'expenses' and 'itc_status' in event.after:
        return 'ITC_REVIEW_RECORDED'
    if event.module == 'adjustments' and event.action == 'create':
        return 'FINANCIAL_ADJUSTMENT_CREATED'
    if event.module in ('invoices', 'expenses', 'payments', 'adjustments', 'commercial'):
        return {'verify': 'FINANCIAL_RECORD_FINALIZED', 'cancel': 'FINANCIAL_RECORD_CANCELLED', 'payment': 'FINANCIAL_PAYMENT_RECORDED', 'create': 'FINANCIAL_RECORD_CREATED'}.get(event.action, 'FINANCIAL_RECORD_MODIFIED')
    return 'AUDIT_ACTIVITY'

def seal_event(event, request=None, event_type=None, severity=None, principal=None, source='application'):
    kind = event_type or taxonomy(event)
    if not isinstance(kind,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',kind) or source not in ('application', 'authentication', 'middleware', 'operator'):
        raise ValidationError('Invalid security taxonomy/source.')
    level = severity or ('MEDIUM' if not event.success else 'INFO')
    if level not in {'INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}:
        raise ValidationError('Invalid severity.')
    if SecurityRecord.objects.filter(event=event).exists():
        return event.security_record
    previous = SecurityRecord.objects.order_by('-sequence').first()
    record = SecurityRecord(event=event, sequence=previous.sequence + 1 if previous else 1, event_type=kind, severity=level, correlation_id=getattr(request, 'security_correlation_id', None), principal=principal or privacy_key('user:' + str(event.user_id)) if event.user_id else principal or privacy_key('network:' + str(event.ip_address)), network=privacy_key('network:' + str(event.ip_address)), source=source, previous_hash=previous.digest if previous else '0' * 64)
    record.digest = record_digest(record, event)
    record.save()
    detect(record)
    return record

def detect(record):
    now = record.created_at
    for rule_id, defaults in RULES.items():
        if record.event_type not in defaults['types']:
            continue
        override = SecurityRuleConfiguration.objects.filter(rule_id=rule_id).first()
        if override and (not override.enabled):
            continue
        threshold = override.threshold if override else defaults['threshold']
        window = override.window_seconds if override else defaults['window']
        scope = defaults['scope']
        value = getattr(record, scope)
        matches = SecurityRecord.objects.filter(event_type__in=defaults['types'], created_at__gte=now - timedelta(seconds=window), created_at__lte=now, event__business_id=record.event.business_id, **{scope: value}).order_by('-sequence')
        ids = list(matches.values_list('pk', flat=True)[:threshold])
        if len(ids) < threshold:
            continue
        bucket = int(now.timestamp()) // window
        fingerprint = hashlib.sha256(f'{rule_id}:{record.event.business_id}:{value}:{bucket}'.encode()).hexdigest()
        alert, created = SecurityAlert.objects.get_or_create(fingerprint=fingerprint, defaults={'rule_id': rule_id, 'business_id': record.event.business_id, 'severity': defaults['severity'], 'summary': defaults['description'][:300], 'evidence': ids[:50]})
        if created:
            SecurityAlertHistory.objects.create(alert=alert, actor=None, status='OPEN', reason=f'Rule threshold {threshold} within {window} seconds; evidence capped at 50 IDs.')
            from .services import record_event
            record_event(user=None, business=record.event.business, action='security', module='monitoring', description='Security alert created by a deterministic rule.', obj=alert, metadata={'rule_id': rule_id, 'alert_id': alert.pk}, event_type='SECURITY_ALERT_CREATED')

@transaction.atomic
def transition_alert(request, alert_id, status, reason):
    if not administrator(request.user):
        raise PermissionDenied
    if status not in {'ACKNOWLEDGED', 'INVESTIGATING', 'RESOLVED', 'DISMISSED'} or not reason.strip():
        raise ValidationError('Choose an allowed status and explain the decision.')
    SecurityAlert.objects.filter(pk=alert_id).update(updated_at=F('updated_at'))
    alert = SecurityAlert.objects.select_for_update().get(pk=alert_id)
    if alert.status == status:
        return alert
    if alert.status in {'RESOLVED', 'DISMISSED'}:
        raise ValidationError('Closed alerts are retained and cannot be reopened.')
    from accounts.logging import redact_account_tokens
    previous = alert.status
    alert.status = status
    alert.save(update_fields=['status', 'updated_at'])
    SecurityAlertHistory.objects.create(alert=alert, actor=request.user, status=status, reason=redact_account_tokens(reason)[:500])
    from .services import record_event
    record_event(request=request, business=alert.business, action='security', module='monitoring', description='Security alert decision recorded.', obj=alert, before={'status': previous}, after={'status': status}, event_type='SECURITY_ALERT_RESOLVED' if status in {'RESOLVED', 'DISMISSED'} else 'SECURITY_ALERT_UPDATED')
    return alert

@transaction.atomic
def configure_rule(request, rule_id, threshold, window_seconds, enabled):
    if not administrator(request.user):
        raise PermissionDenied
    if rule_id not in RULES or not 2 <= threshold <= 1000 or (not 60 <= window_seconds <= 3600):
        raise ValidationError('Choose a supported rule, threshold 2–1,000 and window 60–3,600 seconds.')
    old = SecurityRuleConfiguration.objects.filter(rule_id=rule_id).values('threshold', 'window_seconds', 'enabled').first() or {}
    config, _ = SecurityRuleConfiguration.objects.update_or_create(rule_id=rule_id, defaults={'threshold': threshold, 'window_seconds': window_seconds, 'enabled': enabled, 'updated_by': request.user})
    from .services import record_event
    record_event(request=request, action='security', module='monitoring', description='Bounded security rule configuration changed.', obj=config, before=old, after={'threshold': threshold, 'window_seconds': window_seconds, 'enabled': enabled}, metadata={'rule_id': rule_id}, event_type='SECURITY_CONFIGURATION_CHANGED')
    return config

def observe_session(request, user=None):
    actor = user or request.user
    if not getattr(request, 'session', None) or not request.session.session_key:
        return
    from accounts.models import BusinessProfile
    key = session_digest(request.session.session_key)
    now = timezone.now()
    agent = request.META.get('HTTP_USER_AGENT', '')
    browser = next((name for marker, name in [('Edg/', 'Edge'), ('Firefox', 'Firefox'), ('Chrome', 'Chrome'), ('Safari', 'Safari')] if marker in agent), 'Unknown')
    record, created = SessionSecurityRecord.objects.get_or_create(token_digest=key, defaults={'user': actor, 'business': BusinessProfile.objects.filter(user=actor).first(), 'browser': browser, 'last_seen': now, 'expires_at': request.session.get_expiry_date()})
    if record.user_id != actor.pk:
        raise ValidationError('Session ownership mismatch.')
    if not created and record.last_seen < now - timedelta(seconds=60):
        SessionSecurityRecord.objects.filter(pk=record.pk, last_seen__lt=now - timedelta(seconds=60)).update(last_seen=now, expires_at=request.session.get_expiry_date())
    return record

@transaction.atomic
def revoke_sessions(request, password, record_id=None, others=False):
    if not request.user.check_password(password):
        raise ValidationError('Reauthenticate with your current password.')
    qs = SessionSecurityRecord.objects.filter(user=request.user, revoked_at__isnull=True, ended_at__isnull=True, expires_at__gt=timezone.now())
    if others:
        qs = qs.exclude(token_digest=session_digest(request.session.session_key))
    else:
        qs = qs.filter(pk=record_id)
    if qs.count() > 1000:
        raise ValidationError('Revocation exceeds the interactive 1,000-observation limit; contact an operator.')
    ids = list(qs.values_list('pk', flat=True)[:1000])
    count = qs.update(revoked_at=timezone.now())
    if not count:
        raise ValidationError('No matching active session observation.')
    from .services import record_event
    record_event(request=request, action='security', module='accounts', description='Owned session observations revoked; subsequent authenticated requests are blocked.', metadata={'revoked_count': count, 'others': others, 'observation_ids': ids}, event_type='AUTH_SESSION_REVOKED')
    return count
