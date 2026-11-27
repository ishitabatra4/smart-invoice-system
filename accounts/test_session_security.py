"""Focused session-authentication security tests (isolated test database and accounts)."""
import json
import os
import subprocess
import sys
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from audit.models import AuditEvent, SecurityRecord, SessionSecurityRecord
from .models import BusinessProfile
from .session_policy import ACTIVITY_KEY, STARTED_KEY

PASSWORD = "Session-test-password-71!"


def events(kind):
    return SecurityRecord.objects.filter(event_type=kind).count()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   SESSION_IDLE_TIMEOUT=1800, SESSION_ABSOLUTE_TIMEOUT=43200, SESSION_ACTIVITY_UPDATE_INTERVAL=60)
class SessionSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user("session-owner", "session.owner@example.com", PASSWORD)
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Session Test Business")
        self.other = get_user_model().objects.create_user("session-other", "session.other@example.com", PASSWORD)
        BusinessProfile.objects.create(user=self.other, business_name="Other Session Business")

    def sign_in(self, client=None, email="session.owner@example.com", password=PASSWORD, url=None):
        return (client or self.client).post(url or reverse("login"), {"email": email, "password": password})

    def set_clock(self, client, started_ago, active_ago):
        session = client.session
        now = int(time.time())
        session[STARTED_KEY] = now - started_ago
        session[ACTIVITY_KEY] = now - active_ago
        session.save()

    def reuse(self, cookie_value):
        """A fresh browser presenting a previously issued session cookie."""
        attacker = Client()
        attacker.cookies[settings.SESSION_COOKIE_NAME] = cookie_value
        return attacker

    # C1 — login
    def test_successful_login_creates_server_session_with_clock_and_single_audit_event(self):
        response = self.sign_in()
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        session = self.client.session
        self.assertEqual(int(session["_auth_user_id"]), self.user.pk)
        self.assertIsInstance(session[STARTED_KEY], int)
        self.assertTrue(Session.objects.filter(session_key=session.session_key).exists())
        self.assertEqual(events("AUTH_LOGIN_SUCCESS"), 1)
        self.assertTrue(self.user.password.startswith(("pbkdf2_sha256$", "argon2", "bcrypt", "scrypt")))

    def test_invalid_credentials_and_unknown_email_share_generic_message(self):
        wrong = self.sign_in(password="not-the-password")
        unknown = self.sign_in(email="nobody@example.com")
        for response in (wrong, unknown):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "The email address or password is incorrect.")
            self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(events("AUTH_LOGIN_FAILURE"), 2)

    def test_session_id_rotates_and_preauthentication_key_is_useless(self):
        session = self.client.session
        session["pre_login_marker"] = "x"
        session.save()
        fixed_key = session.session_key
        self.sign_in()
        self.assertNotEqual(self.client.session.session_key, fixed_key)
        self.assertEqual(self.reuse(fixed_key).get(reverse("invoice_list")).status_code, 302)

    def test_csrf_enforced_on_login_and_logout(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("login"), {"email": "x@example.com", "password": "x"}).status_code, 403)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)
        self.assertEqual(client.get(reverse("invoice_list")).status_code, 200)

    def test_safe_next_redirects(self):
        target = reverse("invoice_list")
        self.assertEqual(self.sign_in(url=reverse("login") + "?next=" + target).url, target)
        for bad in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)", reverse("logout")):
            client = Client()
            self.assertEqual(self.sign_in(client, url=reverse("login") + "?next=" + bad).url, reverse("dashboard"), bad)

    def test_login_rate_limit_throttles_and_audits(self):
        with override_settings(ACCOUNT_RATE_LIMIT_ATTEMPTS=2):
            for _ in range(2):
                self.sign_in(password="wrong")
            response = self.sign_in()
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(events("AUTH_REQUEST_THROTTLED"), 1)

    def test_unauthenticated_protected_page_redirects_to_login(self):
        response = self.client.get(reverse("invoice_list"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("login") + "?next="))

    # C2 — logout
    def test_logout_is_post_only_and_invalidates_old_cookie(self):
        self.sign_in()
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("login"))
        self.assertFalse(Session.objects.filter(session_key=cookie).exists())
        self.assertEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, "")
        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 302)
        self.assertEqual(self.reuse(cookie).get(reverse("invoice_list")).status_code, 302)
        self.assertEqual(events("AUTH_LOGOUT"), 1)
        self.assertIsNotNone(SessionSecurityRecord.objects.get(user=self.user).ended_at)

    def test_sensitive_pages_are_not_cached(self):
        self.sign_in()
        for name in ("invoice_list", "analytics_dashboard", "security_sessions"):
            self.assertIn("no-store", self.client.get(reverse(name))["Cache-Control"], name)
        self.client.post(reverse("logout"))
        self.assertIn("no-store", self.client.get(reverse("login"))["Cache-Control"])

    # C3 — expiration
    def test_idle_expiry_invalidates_session_and_redirects_with_message(self):
        self.sign_in()
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.set_clock(self.client, started_ago=1900, active_ago=1801)
        response = self.client.get(reverse("invoice_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("expired=idle", response.url)
        self.assertIn("next=" + reverse("invoice_list").replace("/", "%2F"), response.url)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertFalse(Session.objects.filter(session_key=cookie).exists())
        self.assertEqual(self.reuse(cookie).get(reverse("invoice_list")).status_code, 302)
        login_page = self.client.get(response.url)
        self.assertContains(login_page, "Your session expired after inactivity.")
        self.assertEqual(events("AUTH_SESSION_EXPIRED"), 1)
        self.assertEqual(events("AUTH_LOGOUT"), 0)
        self.assertEqual(SecurityRecord.objects.get(event_type="AUTH_SESSION_EXPIRED").event.metadata, {"reason": "idle"})
        self.assertIsNotNone(SessionSecurityRecord.objects.get(user=self.user).ended_at)

    def test_absolute_expiry_applies_despite_recent_activity(self):
        self.sign_in()
        self.set_clock(self.client, started_ago=43201, active_ago=5)
        response = self.client.get(reverse("invoice_list"))
        self.assertIn("expired=absolute", response.url)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_session_within_limits_remains_valid(self):
        self.sign_in()
        self.set_clock(self.client, started_ago=43000, active_ago=1700)
        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 200)

    def test_passive_requests_never_extend_idle_window(self):
        self.sign_in()
        self.set_clock(self.client, started_ago=1000, active_ago=900)
        before = self.client.session[ACTIVITY_KEY]
        response = self.client.get(reverse("session_keepalive"), HTTP_X_SESSION_ACTIVITY="passive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session[ACTIVITY_KEY], before)
        self.assertLessEqual(response.json()["remaining"], 900)
        self.client.get(reverse("invoice_list"))
        self.assertGreater(self.client.session[ACTIVITY_KEY], before)

    def test_passive_request_on_expired_session_gets_json_401(self):
        self.sign_in()
        self.set_clock(self.client, started_ago=4000, active_ago=1900)
        response = self.client.get(reverse("session_keepalive"), HTTP_X_SESSION_ACTIVITY="passive")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["session_expired"], True)

    def test_activity_is_written_at_most_once_per_interval(self):
        self.sign_in()
        self.set_clock(self.client, started_ago=100, active_ago=10)
        before = self.client.session[ACTIVITY_KEY]
        self.client.get(reverse("invoice_list"))
        self.assertEqual(self.client.session[ACTIVITY_KEY], before)

    def test_keepalive_post_requires_csrf_and_records_activity(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.set_clock(client, started_ago=1000, active_ago=30)
        self.assertEqual(client.post(reverse("session_keepalive")).status_code, 403)
        self.client.force_login(self.user)
        self.set_clock(self.client, started_ago=1000, active_ago=30)
        response = self.client.post(reverse("session_keepalive"))
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()["remaining"], 1790)

    def test_warning_banner_rendered_without_session_secrets(self):
        self.sign_in()
        response = self.client.get(reverse("invoice_list"))
        self.assertContains(response, "data-session-timeout")
        self.assertNotContains(response, self.client.session.session_key)

    # C5 — revocation
    def test_revoked_session_cookie_rejected_and_not_logged_as_logout(self):
        victim = Client()
        self.sign_in(victim)
        cookie = victim.cookies[settings.SESSION_COOKIE_NAME].value
        self.sign_in()
        row = SessionSecurityRecord.objects.filter(user=self.user).exclude(
            token_digest=__import__("audit.security", fromlist=["session_digest"]).session_digest(self.client.session.session_key)).get()
        self.client.post(reverse("security_sessions"), {"session_id": row.pk, "password": PASSWORD})
        self.assertEqual(self.reuse(cookie).get(reverse("invoice_list")).status_code, 302)
        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 200)
        self.assertEqual(events("AUTH_SESSION_REVOKED"), 1)
        self.assertEqual(events("AUTH_LOGOUT"), 0)

    def test_logout_all_other_devices(self):
        devices = [Client(), Client()]
        for device in devices:
            self.sign_in(device)
        self.sign_in()
        self.client.post(reverse("security_sessions"), {"action": "others", "password": PASSWORD})
        for device in devices:
            self.assertEqual(device.get(reverse("invoice_list")).status_code, 302)
        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 200)

    def test_cannot_revoke_another_users_session(self):
        other_client = Client()
        self.sign_in(other_client, email="session.other@example.com")
        row = SessionSecurityRecord.objects.get(user=self.other)
        self.sign_in()
        self.assertEqual(self.client.post(reverse("security_sessions"), {"session_id": row.pk, "password": PASSWORD}).status_code, 404)
        self.assertEqual(other_client.get(reverse("invoice_list")).status_code, 200)

    # C6 — password change / reset
    def test_password_change_keeps_current_and_invalidates_other_sessions(self):
        other_device = Client()
        self.sign_in(other_device)
        self.sign_in()
        response = self.client.post(reverse("password_change"), {
            "old_password": PASSWORD, "new_password1": "Changed-session-pass-55!", "new_password2": "Changed-session-pass-55!"})
        self.assertRedirects(response, reverse("profile"))
        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 200)
        self.assertEqual(other_device.get(reverse("invoice_list")).status_code, 302)
        self.assertEqual(SessionSecurityRecord.objects.filter(user=self.user, ended_at__isnull=True).count(), 1)
        self.assertEqual(events("AUTH_PASSWORD_CHANGED"), 1)

    def test_password_reset_invalidates_all_sessions_and_never_logs_token(self):
        device = Client()
        self.sign_in(device)
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        self.user.refresh_from_db()
        token = default_token_generator.make_token(self.user)
        page = self.client.get(reverse("password_reset_confirm", args=[uid, token]))
        response = self.client.post(page.url, {"new_password1": "Reset-session-pass-66!", "new_password2": "Reset-session-pass-66!"})
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.assertEqual(device.get(reverse("invoice_list")).status_code, 302)
        self.assertFalse(SessionSecurityRecord.objects.filter(user=self.user, ended_at__isnull=True).exists())
        self.assertEqual(events("AUTH_PASSWORD_RESET_COMPLETED"), 1)
        stored = json.dumps(list(AuditEvent.objects.values()), default=str)
        self.assertNotIn(token, stored)
        self.assertNotIn("Reset-session-pass-66!", stored)


class CookieConfigurationTests(TestCase):
    """Settings are evaluated at import time, so each profile is loaded in a fresh interpreter."""

    def load(self, **env):
        code = ("import json,django,os;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';"
                "from django.conf import settings as s;"
                "print(json.dumps({k:getattr(s,k) for k in ('DEBUG','SESSION_COOKIE_SECURE','CSRF_COOKIE_SECURE','SESSION_COOKIE_HTTPONLY',"
                "'SESSION_COOKIE_SAMESITE','CSRF_COOKIE_SAMESITE','SESSION_COOKIE_AGE','SESSION_EXPIRE_AT_BROWSER_CLOSE','SESSION_IDLE_TIMEOUT',"
                "'SESSION_ABSOLUTE_TIMEOUT','SECURE_SSL_REDIRECT')}))")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("SESSION_", "DEBUG", "SECRET_KEY", "ALLOWED_HOSTS"))}
        result = subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env={**base, **env}, capture_output=True, text=True)
        return result

    def test_development_cookies_work_over_local_http(self):
        result = self.load(DEBUG="true")
        values = json.loads(result.stdout)
        self.assertFalse(values["SESSION_COOKIE_SECURE"])
        self.assertFalse(values["CSRF_COOKIE_SECURE"])
        self.assertFalse(values["SECURE_SSL_REDIRECT"])
        self.assertTrue(values["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual((values["SESSION_COOKIE_SAMESITE"], values["CSRF_COOKIE_SAMESITE"]), ("Lax", "Lax"))
        self.assertEqual((values["SESSION_IDLE_TIMEOUT"], values["SESSION_ABSOLUTE_TIMEOUT"], values["SESSION_COOKIE_AGE"]), (1800, 43200, 43200))
        self.assertFalse(values["SESSION_EXPIRE_AT_BROWSER_CLOSE"])

    def test_production_enforces_secure_cookies(self):
        result = self.load(DEBUG="false", SECRET_KEY="x" * 60, ALLOWED_HOSTS="invoice.example", SESSION_EXPIRE_AT_BROWSER_CLOSE="true")
        values = json.loads(result.stdout)
        self.assertTrue(values["SESSION_COOKIE_SECURE"])
        self.assertTrue(values["CSRF_COOKIE_SECURE"])
        self.assertTrue(values["SECURE_SSL_REDIRECT"])
        self.assertTrue(values["SESSION_EXPIRE_AT_BROWSER_CLOSE"])

    def test_timeouts_configurable_and_invalid_values_rejected(self):
        values = json.loads(self.load(DEBUG="true", SESSION_IDLE_TIMEOUT="900", SESSION_ABSOLUTE_TIMEOUT="7200").stdout)
        self.assertEqual((values["SESSION_IDLE_TIMEOUT"], values["SESSION_COOKIE_AGE"]), (900, 7200))
        for bad in ({"SESSION_IDLE_TIMEOUT": "abc"}, {"SESSION_IDLE_TIMEOUT": "10"}, {"SESSION_IDLE_TIMEOUT": "7200", "SESSION_ABSOLUTE_TIMEOUT": "3600"}):
            result = self.load(DEBUG="true", **bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("ImproperlyConfigured", result.stderr)
