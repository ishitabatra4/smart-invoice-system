"""Phase 13 contract tests: isolated databases/storage, no external delivery."""
import hashlib
import json
import sqlite3
import tempfile
import uuid
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction, IntegrityError
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.test import TestCase, TransactionTestCase, RequestFactory, Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from accounts.models import BusinessProfile
from accounts.logging import redact_account_tokens
from audit.models import AuditEvent, SecurityRecord, SecurityAlert, SecurityAlertHistory, SecurityRuleConfiguration, SessionSecurityRecord
from audit.services import record_event, safe_data
from audit.security import privacy_key, session_digest, record_digest, configure_rule, transition_alert, revoke_sessions, observe_session, administrator, RULES
from audit.middleware import AuditForbiddenResponseMiddleware

class SecurityTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username='security-owner', email='owner@example.test', password='safe-password')
        cls.other = User.objects.create_user(username='security-other', email='other@example.test', password='safe-password')
        cls.admin = User.objects.create_superuser(username='security-admin', email='admin@example.test', password='safe-password')
        cls.business = BusinessProfile.objects.create(user=cls.owner, business_name='Synthetic Monitoring Business')
        cls.foreign = BusinessProfile.objects.create(user=cls.other, business_name='Foreign Monitoring Business')

    def setUp(self):
        cache.clear()

    def request(self, user=None):
        request = RequestFactory().post('/audit/monitor/', REMOTE_ADDR='127.0.0.1')
        request.user = user or self.owner
        request.security_correlation_id = uuid.uuid4()
        from django.contrib.sessions.backends.db import SessionStore
        request.session = SessionStore()
        request.session.create()
        return request

    def event(self, **kwargs):
        values = {'user': self.owner, 'business': self.business, 'action': 'update', 'module': 'invoices', 'description': 'Synthetic change', 'before': {'total': '100'}, 'after': {'total': '118'}}
        values.update(kwargs)
        return record_event(**values)

    def alert(self):
        for _ in range(5):
            self.event(action='failed_login', module='accounts', success=False, event_type='AUTH_LOGIN_FAILURE')
        return SecurityAlert.objects.get(rule_id='AUTH_FAILURE_BURST')

    def test_shared_audit_gets_exactly_one_seal(self):
        event = self.event()
        self.assertEqual(SecurityRecord.objects.count(), 1)
        self.assertEqual(event.security_record.sequence, 1)
        self.assertEqual(event.security_record.digest, record_digest(event.security_record, event))

    def test_chain_previous_digest_and_sequence(self):
        a = self.event()
        b = self.event()
        self.assertEqual(b.security_record.previous_hash, a.security_record.digest)
        self.assertEqual(b.security_record.sequence, 2)

    def test_correlation_is_server_uuid(self):
        req = self.request()
        e = self.event(request=req)
        self.assertEqual(e.security_record.correlation_id, req.security_correlation_id)

    def test_sensitive_metadata_and_nested_text_redacted(self):
        e = self.event(metadata={'password': 'never-store', 'nested': {'token': 'secret', 'value': 'Bearer example-secret'}, 'raw_text': 'private body'})
        self.assertNotIn('never-store', str(e.metadata))
        self.assertNotIn('example-secret', str(e.metadata))
        self.assertNotIn('private body', str(e.metadata))

    def test_quoted_secret_assignments_redacted(self):
        for text in ['password=example-secret', '"password": "example-secret"', "'token': 'example-secret'", 'Bearer example-secret', '/accounts/reset/abc/secret/']:
            self.assertNotIn('example-secret', redact_account_tokens(text))

    def test_metadata_bounded_recursion_and_non_dict(self):
        value = {'value': list(range(10000))}
        self.assertLess(len(str(safe_data(value))), 2000)
        nested = {}
        head = nested
        for _ in range(50):
            head['nested'] = {}
            head = head['nested']
        self.assertLess(len(str(safe_data(nested))), 500)
        self.assertIsInstance(safe_data(['value']), dict)

    def test_no_raw_attempt_identity_in_security_principal(self):
        self.client.post(reverse('login'), {'email': self.owner.email, 'password': 'wrong'})
        record = SecurityRecord.objects.get(event_type='AUTH_LOGIN_FAILURE')
        self.assertEqual(len(record.principal), 40)
        self.assertNotIn(self.owner.email, str(record.__dict__))

    def test_no_forwarded_ip_trust(self):
        req = self.request()
        req.META['HTTP_X_FORWARDED_FOR'] = '8.8.8.8'
        e = self.event(request=req)
        self.assertEqual(e.ip_address, '127.0.0.1')

    def test_seal_failure_rolls_back_entire_business_transaction(self):
        original = self.business.business_name
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                BusinessProfile.objects.filter(pk=self.business.pk).update(business_name='Must roll back')
                with patch('audit.security.seal_event', side_effect=RuntimeError('unavailable')):
                    self.event()
        self.business.refresh_from_db()
        self.assertEqual(self.business.business_name, original)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_outer_rollback_has_no_success_or_chain_gap(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.event()
                raise RuntimeError('rollback')
        self.assertEqual(self.event().security_record.sequence, 1)

    def test_historical_rows_not_backfilled(self):
        old = AuditEvent.objects.create(business=self.business, action='create', module='old', description='Legacy')
        self.event()
        self.assertFalse(SecurityRecord.objects.filter(event=old).exists())

    def test_security_records_append_only(self):
        seal = self.event().security_record
        for operation in [lambda: seal.save(), lambda: seal.delete(), lambda: SecurityRecord.objects.all().update(severity='HIGH'), lambda: SecurityRecord.objects.all().delete()]:
            with self.assertRaises((ValueError, ValidationError)):
                operation()

    def test_taxonomy_financial_and_review_hooks(self):
        cases = [('invoices', 'verify', {}, 'FINANCIAL_RECORD_FINALIZED'), ('payments', 'payment', {}, 'FINANCIAL_PAYMENT_RECORDED'), ('adjustments', 'create', {}, 'FINANCIAL_ADJUSTMENT_CREATED'), ('extraction', 'update', {}, 'OCR_REVIEW_SAVED'), ('expenses', 'update', {'itc_status': 'reviewed'}, 'ITC_REVIEW_RECORDED')]
        for module, action, after, expected in cases:
            self.assertEqual(self.event(module=module, action=action, after=after).security_record.event_type, expected)

    def test_email_audit_is_not_export(self):
        self.assertEqual(self.event(action='export', metadata={'event': 'invoice_email_sent'}).security_record.event_type, 'DOCUMENT_EMAIL_ACTIVITY')

    def test_invalid_taxonomy_rolls_back(self):
        with self.assertRaises(ValidationError):
            self.event(event_type='lowercase')
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_alert_threshold_and_suppression(self):
        alert = self.alert()
        for _ in range(4):
            self.event(action='failed_login', module='accounts', success=False)
        self.assertEqual(SecurityAlert.objects.count(), 1)
        self.assertEqual(alert.history.count(), 1)
        self.assertEqual(len(alert.evidence), 5)

    def test_alert_business_isolation(self):
        for _ in range(3):
            self.event(action='failed_login', success=False)
        for _ in range(3):
            self.event(action='failed_login', success=False, business=self.foreign)
        self.assertEqual(SecurityAlert.objects.count(), 0)

    def test_disabled_rule(self):
        configure_rule(self.request(self.admin), 'AUTH_FAILURE_BURST', 2, 60, False)
        for _ in range(6):
            self.event(action='failed_login', success=False)
        self.assertEqual(SecurityAlert.objects.count(), 0)

    def test_rule_override_is_audited_and_bounded(self):
        req = self.request(self.admin)
        configure_rule(req, 'AUTH_FAILURE_BURST', 2, 60, True)
        self.assertTrue(SecurityRecord.objects.filter(event_type='SECURITY_CONFIGURATION_CHANGED').exists())
        for threshold, window in [(1, 60), (1001, 60), (2, 59), (2, 3601)]:
            with self.assertRaises(ValidationError):
                configure_rule(req, 'AUTH_FAILURE_BURST', threshold, window, True)
        with self.assertRaises(ValidationError):
            configure_rule(req, 'python:execute', 2, 60, True)

    def test_rule_configuration_requires_privilege(self):
        with self.assertRaises(PermissionDenied):
            configure_rule(self.request(), 'AUTH_FAILURE_BURST', 2, 60, True)

    def test_staff_without_explicit_permission_not_platform_admin(self):
        self.owner.is_staff = True
        self.owner.save()
        self.assertFalse(administrator(self.owner))
        self.owner.user_permissions.add(Permission.objects.get(codename='manage_security'))
        self.owner = get_user_model().objects.get(pk=self.owner.pk)
        self.assertTrue(administrator(self.owner))

    def test_alert_lifecycle_history_idempotency_and_terminal_state(self):
        alert = self.alert()
        req = self.request(self.admin)
        for status in ['ACKNOWLEDGED', 'INVESTIGATING', 'RESOLVED']:
            transition_alert(req, alert.pk, status, 'Reviewed synthetic evidence')
        transition_alert(req, alert.pk, 'RESOLVED', 'Same decision')
        self.assertEqual(alert.history.count(), 4)
        with self.assertRaises(ValidationError):
            transition_alert(req, alert.pk, 'INVESTIGATING', 'Reopen')

    def test_alert_reason_required_and_owner_cannot_modify(self):
        alert = self.alert()
        with self.assertRaises(ValidationError):
            transition_alert(self.request(self.admin), alert.pk, 'RESOLVED', ' ')
        with self.assertRaises(PermissionDenied):
            transition_alert(self.request(), alert.pk, 'RESOLVED', 'Reviewed')

    def test_retention_guards_alert_history_config_sessions(self):
        alert = self.alert()
        req = self.request(self.admin)
        config = configure_rule(req, 'AUTH_FAILURE_BURST', 2, 60, True)
        session = observe_session(req)
        for model in [SecurityAlert, SecurityAlertHistory, SecurityRuleConfiguration, SessionSecurityRecord]:
            with self.assertRaises((ValidationError, ValueError)):
                model.objects.all().delete()

    def test_detect_failure_rolls_back_event(self):
        with patch('audit.security.detect', side_effect=RuntimeError('detector unavailable')):
            with self.assertRaises(RuntimeError):
                self.event()
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertEqual(SecurityRecord.objects.count(), 0)

    def test_owner_dashboard_scope_and_details(self):
        own = self.event()
        foreign = self.event(user=self.other, business=self.foreign, description='Do not show foreign event')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('security_monitor'))
        self.assertContains(response, 'Synthetic change')
        self.assertNotContains(response, 'Do not show foreign event')
        self.assertEqual(self.client.get(reverse('security_event_detail', args=[own.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('security_event_detail', args=[foreign.pk])).status_code, 404)

    def test_platform_admin_dashboard(self):
        self.event(user=self.other, business=self.foreign, description='Platform visible')
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('security_monitor')), 'Platform visible')

    def test_foreign_business_filter_no_data(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('security_monitor'), {'business': self.foreign.pk})
        self.assertContains(response, 'Use your own workspace.')
        self.assertEqual(response.context['counts']['total'], 0)

    def test_anonymous_monitor_redirects(self):
        self.assertEqual(self.client.get(reverse('security_monitor')).status_code, 302)

    def test_unapplied_schema_returns_explicit_unavailable(self):
        self.client.force_login(self.owner)
        with patch('audit.security_views.ready', return_value=False):
            self.assertEqual(self.client.get(reverse('security_monitor')).status_code, 503)

    def test_dashboard_pagination_bounded_and_no_n_plus_one(self):
        for _ in range(35):
            self.event()
        self.client.force_login(self.owner)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('security_monitor'))
        self.assertEqual(len(response.context['rows']), 25)
        self.assertLess(len(queries), 35)

    def test_invalid_filters_never_broaden_results(self):
        self.event()
        self.client.force_login(self.owner)
        for values in [{'start_date': 'bad'}, {'start_date': '2020-01-01', 'end_date': '2026-01-01'}, {'severity': 'INVALID'}, {'correlation': 'bad'}]:
            response = self.client.get(reverse('security_monitor'), values)
            self.assertEqual(response.context['counts']['total'], 0)
            self.assertEqual(self.client.get(reverse('security_export', args=['csv']), values).status_code, 400)

    def test_denial_event_does_not_assert_foreign_existence(self):
        req = self.request()
        req.path = '/invoices/999999/'
        AuditForbiddenResponseMiddleware(lambda r: HttpResponseNotFound())(req)
        e = AuditEvent.objects.get()
        self.assertEqual(e.security_record.event_type, 'ACCESS_RESOURCE_NOT_FOUND')
        self.assertNotIn('cross-business', e.description.lower())

    def test_denial_logged_once_and_correlation_generated(self):
        req = self.request()

        def view(r):
            record_event(request=r, action='security', module='security', success=False, description='Denied')
            return HttpResponseForbidden()
        response = AuditForbiddenResponseMiddleware(view)(req)
        self.assertEqual(AuditEvent.objects.count(), 1)
        uuid.UUID(response['X-Request-ID'])

    def test_event_exports_all_formats_scoped_and_redacted(self):
        own = self.event(description='password=example-secret')
        self.event(user=self.other, business=self.foreign, description='FOREIGN-SECRET')
        self.client.force_login(self.owner)
        for fmt in ['csv', 'xlsx', 'pdf']:
            response = self.client.get(reverse('security_export', args=[fmt]), {'event_type': 'FINANCIAL_RECORD_MODIFIED'})
            self.assertEqual(response.status_code, 200)
            if fmt == 'csv':
                data = b''.join(response.streaming_content)
                self.assertIn(b'[redacted]', data)
                self.assertNotIn(b'example-secret', data)
                self.assertNotIn(b'FOREIGN-SECRET', data)
            elif fmt == 'xlsx':
                workbook = load_workbook(BytesIO(response.content), read_only=True)
                rows = list(workbook.active.values)
                self.assertEqual(len(rows), 2)
                self.assertIn('[redacted]', str(rows))
            else:
                self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertEqual(SecurityRecord.objects.filter(event_type='EXPORT_GENERATED').count(), 3)

    def test_formula_and_xml_controls_safe_in_exports(self):
        self.event(description='=1+1\x01')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('security_export', args=['xlsx']), {'event_type': 'FINANCIAL_RECORD_MODIFIED'})
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content))
        self.assertEqual(workbook.active['K2'].data_type, 's')
        self.assertIn('[control]', workbook.active['K2'].value)

    def test_alert_exports_match_status_and_owner_scope(self):
        alert = self.alert()
        self.client.force_login(self.owner)
        for fmt in ['csv', 'xlsx', 'pdf']:
            response = self.client.get(reverse('security_export', args=[fmt]), {'dataset': 'alerts', 'status': 'OPEN'})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(reverse('security_export', args=['csv']), {'dataset': 'alerts', 'actor': self.owner.pk}).status_code, 400)

    def test_pdf_export_limit(self):
        AuditEvent.objects.bulk_create([AuditEvent(business=self.business, action='create', module='legacy', description='Legacy') for _ in range(101)])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('security_export', args=['pdf'])).status_code, 400)

    def test_export_row_limit(self):
        AuditEvent.objects.bulk_create([AuditEvent(business=self.business, action='create', module='legacy', description='Legacy') for _ in range(10001)])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('security_export', args=['csv'])).status_code, 400)

    def test_sessions_store_only_digest_not_raw_key(self):
        req = self.request()
        row = observe_session(req)
        self.assertEqual(row.token_digest, session_digest(req.session.session_key))
        self.assertNotIn(req.session.session_key, str(row.__dict__))

    def test_sessions_page_does_not_expose_digest_or_key(self):
        self.client.force_login(self.owner)
        row = SessionSecurityRecord.objects.get(user=self.owner)
        response = self.client.get(reverse('security_sessions'))
        self.assertNotContains(response, row.token_digest)
        self.assertNotContains(response, self.client.session.session_key)

    def test_session_revoke_requires_password(self):
        req = self.request()
        row = observe_session(req)
        with self.assertRaises(ValidationError):
            revoke_sessions(req, 'wrong', row.pk)
        row.refresh_from_db()
        self.assertIsNone(row.revoked_at)

    def test_session_revoke_other_user_forbidden(self):
        req = self.request(self.other)
        row = observe_session(req)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse('security_sessions'), {'session_id': row.pk, 'password': 'safe-password'}).status_code, 404)
        row.refresh_from_db()
        self.assertIsNone(row.revoked_at)

    def test_revoked_session_blocked_next_request(self):
        other_client = Client()
        other_client.force_login(self.owner)
        self.client.force_login(self.owner)
        req = self.request()
        req.session = self.client.session
        revoke_sessions(req, 'safe-password', others=True)
        self.assertEqual(other_client.get(reverse('security_monitor')).status_code, 302)
        self.assertEqual(self.client.get(reverse('security_monitor')).status_code, 200)
        self.assertEqual(SecurityRecord.objects.filter(event_type='AUTH_SESSION_REVOKED').count(), 1)

    def test_current_session_can_be_revoked(self):
        self.client.force_login(self.owner)
        row = SessionSecurityRecord.objects.get(user=self.owner)
        self.client.post(reverse('security_sessions'), {'session_id': row.pk, 'password': 'safe-password'})
        self.assertEqual(self.client.get(reverse('security_monitor')).status_code, 302)

    def test_sessions_and_rules_reject_non_get_post(self):
        self.client.force_login(self.admin)
        for name in ['security_sessions', 'security_rules']:
            self.assertEqual(self.client.put(reverse(name)).status_code, 405)

    def test_alert_privilege_enforced_over_http(self):
        alert = self.alert()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse('security_alert_transition', args=[alert.pk]), {'status': 'RESOLVED', 'reason': 'No privilege'}).status_code, 403)
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'OPEN')

    def test_rule_database_constraints(self):
        for threshold, window in [(0, 60), (2, 1)]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                SecurityRuleConfiguration.objects.create(rule_id='AUTH_FAILURE_BURST', threshold=threshold, window_seconds=window, updated_by=self.admin)

    def test_window_excludes_old_events(self):
        configure_rule(self.request(self.admin), 'AUTH_FAILURE_BURST', 2, 60, True)
        self.event(action='failed_login', success=False)
        with patch.object(SecurityRecord._meta.get_field('created_at'), 'get_default', return_value=timezone.now() + timedelta(seconds=61)):
            self.event(action='failed_login', success=False)
        self.assertFalse(SecurityAlert.objects.exists())

    def test_nonstring_event_metadata_cannot_break_taxonomy(self):
        self.assertEqual(self.event(metadata={'event': ['not a string']}).security_record.event_type, 'FINANCIAL_RECORD_MODIFIED')

    def test_partial_schema_fails_closed(self):
        from audit.security import ready
        from django.core.exceptions import ImproperlyConfigured
        with patch('audit.security.connection.introspection.table_names', return_value=['audit_securityrecord']):
            with self.assertRaises(ImproperlyConfigured):
                ready()

    def test_recovery_request_is_generic_and_typed(self):
        for email in ['absent@example.test', self.owner.email]:
            response = self.client.post(reverse('password_reset'), {'email': email})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(SecurityRecord.objects.filter(event_type='AUTH_PASSWORD_RESET_REQUEST').count(), 2)
        for record in SecurityRecord.objects.filter(event_type='AUTH_PASSWORD_RESET_REQUEST'):
            self.assertNotIn(self.owner.email, str(record.event.metadata))
            self.assertIsNone(record.event.business_id)

    def test_actual_throttle_records_request_limit_not_account_lock(self):
        from django.test import override_settings
        with override_settings(ACCOUNT_RATE_LIMIT_ATTEMPTS=2):
            for _ in range(3):
                response = self.client.post(reverse('login'), {'email': 'absent@example.test', 'password': 'wrong'})
        self.assertEqual(response.status_code, 429)
        self.assertTrue(SecurityRecord.objects.filter(event_type='AUTH_REQUEST_THROTTLED').exists())
        self.assertFalse(SecurityRecord.objects.filter(event_type='AUTH_ACCOUNT_LOCKED').exists())

    def test_alert_history_is_paginated(self):
        alert = self.alert()
        SecurityAlertHistory.objects.bulk_create([SecurityAlertHistory(alert=alert, status='OPEN', reason='Synthetic observation') for _ in range(30)])
        self.client.force_login(self.owner)
        response = self.client.get(reverse('security_alert_detail', args=[alert.pk]))
        self.assertEqual(len(response.context['history']), 25)

    def test_foreign_alert_read_and_export_denied(self):
        alert = self.alert()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('security_alert_detail', args=[alert.pk])).status_code, 404)
        response = self.client.get(reverse('security_export', args=['csv']), {'dataset': 'alerts', 'business': self.business.pk})
        self.assertEqual(response.status_code, 400)

    def test_legacy_export_rejects_bad_dates(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('audit_export', args=['csv']), {'start_date': 'bad'}).status_code, 400)

    def test_legacy_export_redacts_existing_unsealed_secrets(self):
        AuditEvent.objects.create(business=self.business, action='create', module='legacy', description='password=example-secret', before={'password': 'never-store', 'total': '100'}, after={})
        self.client.force_login(self.owner)
        response = self.client.get(reverse('audit_export', args=['csv']))
        content = b''.join(response.streaming_content)
        self.assertNotIn(b'example-secret', content)
        self.assertNotIn(b'never-store', content)

    def test_recent_session_observation_does_not_issue_update(self):
        req = self.request()
        row = observe_session(req)
        with CaptureQueriesContext(connection) as queries:
            observe_session(req)
        self.assertFalse(any((q['sql'].lstrip().upper().startswith('UPDATE ') for q in queries)))

    def test_busy_post_response_heartbeat_preserves_committed_response(self):
        from django.db import OperationalError
        from django.http import HttpResponse
        with patch('audit.security.observe_session', side_effect=OperationalError('database table is locked')):
            response = AuditForbiddenResponseMiddleware(lambda request: HttpResponse(status=201))(self.request())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['X-Session-Observation'], 'deferred-busy')

    def test_alert_status_post_requires_csrf(self):
        alert = self.alert()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse('security_alert_transition', args=[alert.pk]), {'status': 'RESOLVED', 'reason': 'No CSRF'}).status_code, 403)
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'OPEN')

    def test_event_metadata_html_is_escaped(self):
        event = self.event(description='<script>malicious()</script>')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('security_event_detail', args=[event.pk]))
        self.assertNotContains(response, '<script>malicious()</script>')
        self.assertContains(response, '&lt;script&gt;malicious()&lt;/script&gt;')

    def test_each_supported_rule_threshold_and_single_suppression(self):
        req=self.request(self.admin)
        for rule_id,defaults in RULES.items():
            configure_rule(req,rule_id,2,600,True)
            kind=defaults['types'][0]
            for _ in range(3):self.event(event_type=kind,success=False if kind.startswith(('AUTH_','ACCESS_','FILE_')) else True)
            self.assertEqual(SecurityAlert.objects.filter(rule_id=rule_id).count(),1,rule_id)
    def test_invalid_security_request_has_distinct_type(self):
        from django.http import HttpResponseBadRequest
        req=self.request();req.path='/invoices/invalid/'
        AuditForbiddenResponseMiddleware(lambda request:HttpResponseBadRequest())(req)
        self.assertTrue(SecurityRecord.objects.filter(event_type='ACCESS_INVALID_REQUEST').exists())

class ChainVerificationTests(TransactionTestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='chain-user')
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'chain.sqlite3'
        for _ in range(3):
            record_event(user=self.user, action='create', module='test', description='Synthetic sealed event')
        dest = sqlite3.connect(self.path)
        connection.connection.backup(dest)
        dest.close()

    def verify(self, **kwargs):
        output = StringIO()
        call_command('verify_audit_security', database_path=str(self.path), stdout=output, **kwargs)
        return json.loads(output.getvalue())

    def mutate(self, sql):
        c = sqlite3.connect(self.path)
        c.execute(sql)
        c.commit()
        c.close()

    def test_valid_chain_read_only_and_checkpoint(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = self.verify()
        self.assertTrue(result['ok'])
        self.assertEqual(result['covered_records'], 3)
        checkpoint = Path(self.folder.name) / 'checkpoint.json'
        checkpoint.write_text(json.dumps(result['checkpoint']))
        self.assertTrue(self.verify(checkpoint=str(checkpoint))['ok'])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_tampered_event_detected(self):
        self.mutate("UPDATE audit_auditevent SET description='changed' WHERE id=(SELECT MIN(event_id) FROM audit_securityrecord)")
        with self.assertRaises(CommandError):
            self.verify()

    def test_tampered_seal_detected(self):
        self.mutate("UPDATE audit_securityrecord SET severity='HIGH' WHERE sequence=2")
        with self.assertRaises(CommandError):
            self.verify()

    def test_tampered_seal_time_detected(self):
        self.mutate("UPDATE audit_securityrecord SET created_at='2000-01-01 00:00:00' WHERE sequence=2")
        with self.assertRaises(CommandError):
            self.verify()

    def test_deleted_middle_detected(self):
        self.mutate('DELETE FROM audit_securityrecord WHERE sequence=2')
        with self.assertRaises(CommandError):
            self.verify()

    def test_deleted_tail_requires_trusted_checkpoint(self):
        checkpoint = Path(self.folder.name) / 'checkpoint.json'
        checkpoint.write_text(json.dumps(self.verify()['checkpoint']))
        self.mutate('DELETE FROM audit_securityrecord WHERE sequence=3')
        self.assertTrue(self.verify()['ok'])
        with self.assertRaises(CommandError):
            self.verify(checkpoint=str(checkpoint))

    def test_invalid_checkpoint_rejected_safely(self):
        checkpoint = Path(self.folder.name) / 'checkpoint.json'
        for value in [[], None, {'checkpoint': []}, {'version': 1, 'last_sequence': True, 'last_digest': '0' * 64}]:
            checkpoint.write_text(json.dumps(value))
            with self.assertRaises(CommandError):
                self.verify(checkpoint=str(checkpoint))

    def test_unsealed_history_explicit(self):
        self.mutate('INSERT INTO audit_auditevent (actor_label,action,module,object_type,object_id,related_object_type,related_object_id,description,"before","after",user_agent,success,metadata,created_at) VALUES (\'\',\'create\',\'old\',\'\',\'\',\'\',\'\',\'Legacy\',\'{}\',\'{}\',\'\',1,\'{}\',\'2000-01-01 00:00:00\')')
        self.assertEqual(self.verify()['unsealed_events'], 1)

class ConcurrentSecurityTests(TransactionTestCase):

    def test_parallel_writers_never_commit_duplicate_sequences_or_orphan_success(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections, OperationalError
        user = get_user_model().objects.create_user(username='parallel-user')

        def run(index):
            close_old_connections()
            try:
                record_event(user=get_user_model().objects.get(pk=user.pk), action='update', module='test', description='Parallel synthetic mutation')
                return True
            except OperationalError:
                return False
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, range(2)))
        self.assertGreaterEqual(sum(results), 1)
        self.assertEqual(AuditEvent.objects.count(), sum(results))
        self.assertEqual(SecurityRecord.objects.count(), sum(results))
        previous = '0' * 64
        for index, record in enumerate(SecurityRecord.objects.select_related('event').order_by('sequence'), 1):
            self.assertEqual(record.sequence, index)
            self.assertEqual(record.previous_hash, previous)
            self.assertEqual(record.digest, record_digest(record, record.event))
            previous = record.digest
