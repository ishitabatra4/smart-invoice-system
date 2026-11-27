import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from audit.models import AuditEvent
from .models import AccountVerification, BusinessProfile


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AccountSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user("owner", "owner@example.com", "Initial-secret-42!")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Original")

    def registration(self, **changes):
        data = dict(email="NEW@example.com", business_name="New business", password1="New-secret-42!", password2="New-secret-42!")
        data.update(changes)
        return self.client.post(reverse("register"), data)

    def test_registration_verification_and_replay(self):
        self.assertRedirects(self.registration(), reverse("registration_done"))
        user = get_user_model().objects.get(email="new@example.com")
        self.assertFalse(user.is_active)
        self.assertTrue(user.check_password("New-secret-42!"))
        self.assertNotIn("_auth_user_id", self.client.session)
        link = re.search(r"http://testserver(/\S+)", mail.outbox[0].body)[1]
        self.client.get(link)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.client.post(link)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(AccountVerification.objects.get(user=user).verified_at)
        self.client.post(link)
        self.assertEqual(AuditEvent.objects.filter(description="Account email verified.").count(), 1)

    def test_duplicate_email_and_weak_password(self):
        self.assertContains(self.registration(email="OWNER@EXAMPLE.COM"), "already registered")
        self.assertEqual(get_user_model().objects.count(), 1)
        self.registration(password1="123", password2="123")
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_verification_expiry_resend_and_delivery_failure(self):
        self.registration()
        link = re.search(r"http://testserver(/\S+)", mail.outbox[0].body)[1]
        with override_settings(EMAIL_VERIFICATION_TIMEOUT=-1):
            self.client.post(link)
        self.assertFalse(get_user_model().objects.get(email="new@example.com").is_active)
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("private")):
            response = self.client.post(reverse("resend_verification"), {"email": "new@example.com"})
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse("resend_verification"), {"email": "new@example.com"})
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.client.get(reverse("resend_verification")).status_code, 200)

    def test_reset_non_enumeration_and_email_failure(self):
        known = self.client.post(reverse("password_reset"), {"email": self.user.email})
        unknown = self.client.post(reverse("password_reset"), {"email": "absent@example.com"})
        self.assertEqual(known.url, unknown.url)
        self.assertEqual(len(mail.outbox), 1)
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("private")):
            self.assertEqual(self.client.post(reverse("password_reset"), {"email": self.user.email}).status_code, 302)

    def reset_link(self):
        token = default_token_generator.make_token(self.user)
        return reverse("password_reset_confirm", args=[urlsafe_base64_encode(force_bytes(self.user.pk)), token]), token

    def test_reset_validators_success_and_single_use(self):
        link, token = self.reset_link()
        response = self.client.get(link)
        self.assertEqual(response.status_code, 302)
        form_url = response.url
        self.assertContains(self.client.post(form_url, {"new_password1": "123", "new_password2": "123"}), "too short")
        response = self.client.post(form_url, {"new_password1": "Replacement-secret-78!", "new_password2": "Replacement-secret-78!"})
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Replacement-secret-78!"))
        self.assertFalse(default_token_generator.check_token(self.user, token))
        self.assertEqual(AuditEvent.objects.filter(description="Password reset completed.").count(), 1)
        self.assertContains(self.client.get(link), "invalid or expired")

    def test_reset_expired_and_https_email(self):
        link, _ = self.reset_link()
        with override_settings(PASSWORD_RESET_TIMEOUT=-1):
            self.assertContains(self.client.get(link), "invalid or expired")
        self.client.post(reverse("password_reset"), {"email": self.user.email}, secure=True)
        self.assertIn("https://testserver/", mail.outbox[0].body)

    def test_password_change_requires_old_password_preserves_session(self):
        self.client.force_login(self.user)
        data = dict(old_password="wrong", new_password1="Replacement-secret-78!", new_password2="Replacement-secret-78!")
        self.assertContains(self.client.post(reverse("password_change"), data), "incorrectly")
        data["old_password"] = "Initial-secret-42!"
        self.assertRedirects(self.client.post(reverse("password_change"), data), reverse("profile"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        event = AuditEvent.objects.get(description="Password changed.")
        self.assertEqual(event.metadata, {})
        self.assertEqual(event.business_id, self.business.pk)

    def test_login_throttle_open_redirect_and_logout_methods(self):
        with override_settings(ACCOUNT_RATE_LIMIT_ATTEMPTS=2):
            for _ in range(2):
                self.client.post(reverse("login"), {"email": "owner@example.com", "password": "wrong"})
            self.assertEqual(self.client.post(reverse("login"), {}).status_code, 429)
        cache.clear()
        response = self.client.post(reverse("login") + "?next=https://evil.example", {"email": "owner@example.com", "password": "Initial-secret-42!"})
        self.assertEqual(response.url, reverse("dashboard"))
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))

    def test_csrf_and_protected_account_routes(self):
        client = Client(enforce_csrf_checks=True)
        for name in ("register", "login", "password_reset", "resend_verification"):
            self.assertEqual(client.post(reverse(name), {}).status_code, 403)
        for name in ("profile", "password_change", "dashboard"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_profile_pan_validation_ownership_and_audit(self):
        other = get_user_model().objects.create_user("other", password="secret")
        other_profile = BusinessProfile.objects.create(user=other, business_name="Other business")
        self.client.force_login(self.user)
        data = {"business_name": "Updated", "business_type": "individual", "pan": "abcde1234f"}
        self.assertRedirects(self.client.post(reverse("profile"), data), reverse("profile"))
        self.business.refresh_from_db()
        other_profile.refresh_from_db()
        self.assertEqual(self.business.pan, "ABCDE1234F")
        self.assertEqual(other_profile.business_name, "Other business")
        event = AuditEvent.objects.get(description="Business profile updated.")
        self.assertEqual(event.before["business_name"], "Original")
        self.assertEqual(event.after["business_name"], "Updated")

    def test_request_logs_redact_recovery_credentials(self):
        import logging
        from .logging import RedactAccountTokens
        record = logging.LogRecord("django.server", 20, "", 0, 'GET %s HTTP/1.1', ("/accounts/reset/abc/private-token/",), None)
        RedactAccountTokens().filter(record)
        self.assertNotIn("private-token", record.getMessage())
        self.assertIn("[redacted]", record.getMessage())
