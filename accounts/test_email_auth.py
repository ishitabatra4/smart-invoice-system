from unittest.mock import patch
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEvent
from .forms import RegisterForm
from .models import AccountVerification, BusinessProfile


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.names = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.names.append(dict(attrs).get("name"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailAuthenticationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user("original-username", "Owner@Example.com", "Original-password-83!")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Original business")

    def sign_in(self, email="owner@example.com", password="Original-password-83!"):
        return self.client.post(reverse("login"), {"email": email, "password": password})

    def test_email_login_matches_existing_account_without_changing_credentials(self):
        original_hash = self.user.password
        response = self.sign_in("  OWNER@EXAMPLE.COM  ")
        self.assertEqual(response.url, reverse("dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "original-username")
        self.assertEqual(self.user.password, original_hash)
        self.assertEqual(self.user.email, "Owner@example.com")
        self.assertTrue(AuditEvent.objects.filter(user=self.user, action="login").exists())

    def test_registration_requires_no_username_and_keeps_business_fields(self):
        form = RegisterForm({"email": " NEW@EXAMPLE.COM ", "business_name": "New business", "gstin": "27ABCDE1234F1Z5",
                             "password1": "New-strong-password-83!", "password2": "New-strong-password-83!"})
        self.assertNotIn("username", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, "new@example.com")
        self.assertTrue(user.username.startswith("account_"))
        self.assertFalse(user.is_active)
        self.assertTrue(user.check_password("New-strong-password-83!"))
        self.assertEqual(user.business_profile.gstin, "27ABCDE1234F1Z5")
        self.assertEqual(user.business_profile.business_name, "New business")
        self.assertEqual(AccountVerification.objects.get(user=user).email, "new@example.com")

    def test_duplicate_legacy_email_with_spaces_is_rejected(self):
        self.user.email = " Owner@Example.com "
        self.user.save()
        form = RegisterForm({"email": "OWNER@example.com"})
        self.assertFalse(form.is_valid())
        self.assertIn("already registered", str(form.errors["email"]))
        self.assertEqual(self.sign_in().status_code, 302)

    def test_invalid_credentials_are_generic_and_failed_attempt_is_audited(self):
        known = self.sign_in(password="wrong")
        unknown = self.sign_in(email="unknown@example.com", password="wrong")
        text = "The email address or password is incorrect. Please try again."
        self.assertContains(known, text)
        self.assertContains(unknown, text)
        self.assertNotContains(known, "Resend verification email")
        event = AuditEvent.objects.get(action="failed_login", business=self.business)
        self.assertEqual(event.metadata, {"attempted_username": ""})
        self.assertNotIn("wrong", str(event.metadata))

    def test_username_is_not_accepted_by_public_login(self):
        response = self.client.post(reverse("login"), {"username": self.user.username, "password": "Original-password-83!"})
        self.assertContains(response, "This field is required")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unverified_hint_requires_correct_password(self):
        self.user.is_active = False
        self.user.save()
        AccountVerification.objects.create(user=self.user, email="owner@example.com")
        self.assertNotContains(self.sign_in(password="wrong"), "Resend verification email")
        response = self.sign_in()
        self.assertContains(response, "Your email address hasn&#x27;t been verified yet.")
        self.assertContains(response, reverse("resend_verification"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(len(mail.outbox), 0)

    def test_same_password_duplicates_refuse_login_but_offer_separate_recovery(self):
        get_user_model().objects.create_user("duplicate", "OWNER@example.com", "Original-password-83!")
        self.assertContains(self.sign_in(), "email address or password is incorrect")
        self.client.post(reverse("password_reset"), {"email": "owner@example.com"})
        self.assertEqual(len(mail.outbox), 2)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_preexisting_session_backend_still_works(self):
        self.client.force_login(self.user, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)
        self.assertEqual(self.client.session["_auth_user_backend"], "django.contrib.auth.backends.ModelBackend")

    def test_login_structure_and_separate_resend_page(self):
        response = self.client.get(reverse("login"))
        html = response.content.decode()
        self.assertEqual([name for name in Inputs(html).names if name != "csrfmiddlewaretoken"], ["email", "password"])
        self.assertContains(response, "Welcome back")
        self.assertContains(response, "Sign in to your workspace to continue.")
        self.assertNotContains(response, reverse("resend_verification"))
        self.assertLess(html.index(">Sign in</button>"), html.index("Forgot password?"))
        self.assertLess(html.index("Forgot password?"), html.index("Create an account"))
        self.assertContains(response, 'data-password-toggle="id_password"')
        self.assertEqual(self.client.get(reverse("resend_verification")).status_code, 200)

    def test_registration_template_omits_username(self):
        response = self.client.get(reverse("register"))
        names = Inputs(response.content.decode()).names
        self.assertNotIn("username", names)
        self.assertTrue({"email", "password1", "password2", "business_name", "gstin"}.issubset(names))
        self.assertContains(response, "auth.css")
        self.assertContains(response, "auth.js")

    def test_recovery_and_resend_validation_and_generic_confirmations(self):
        for name in ("password_reset", "resend_verification"):
            invalid = self.client.post(reverse(name), {"email": "not-an-email"})
            self.assertContains(invalid, "Enter a valid email address")
            known = self.client.post(reverse(name), {"email": "OWNER@example.com"})
            unknown = self.client.post(reverse(name), {"email": "unknown@example.com"})
            self.assertEqual(known.url, unknown.url)
            self.assertContains(self.client.get(known.url), "Check your email")
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_pending_only_and_delivery_failure_is_generic(self):
        self.user.is_active = False
        self.user.save()
        AccountVerification.objects.create(user=self.user, email="owner@example.com")
        response = self.client.post(reverse("resend_verification"), {"email": " OWNER@EXAMPLE.COM "})
        self.assertRedirects(response, reverse("verification_sent"))
        self.assertEqual(len(mail.outbox), 1)
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("secret exception")):
            failed = self.client.post(reverse("resend_verification"), {"email": "owner@example.com"})
        self.assertEqual(failed.url, response.url)
        self.assertNotIn("secret exception", self.client.get(failed.url).content.decode())

    def test_all_auth_pages_render_shared_layout_even_for_signed_in_user(self):
        for name in ("login", "register", "password_reset", "resend_verification", "password_reset_done", "registration_done", "verification_sent", "password_reset_complete"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'class="auth-panel"')
            self.assertContains(response, 'id="auth-heading"')
            self.assertContains(response, 'name="viewport"')
        self.client.force_login(self.user)
        for name in ("password_change", "password_reset", "resend_verification"):
            self.assertContains(self.client.get(reverse(name)), 'class="auth-panel"')

    def test_email_login_throttling_and_redirect_regression(self):
        with override_settings(ACCOUNT_RATE_LIMIT_ATTEMPTS=1):
            self.sign_in(password="wrong")
            self.assertEqual(self.sign_in().status_code, 429)
        cache.clear()
        response = self.client.post(reverse("login") + "?next=//evil.example", {"email": self.user.email, "password": "Original-password-83!"})
        self.assertEqual(response.url, reverse("dashboard"))

    def test_verification_link_cannot_activate_an_account_with_a_changed_email(self):
        from django.core import signing
        self.user.is_active = False
        self.user.save()
        AccountVerification.objects.create(user=self.user, email="owner@example.com")
        token = signing.dumps({"uid": self.user.pk, "email": "owner@example.com"}, salt="accounts.verify")
        self.user.email = "changed@example.com"
        self.user.save()
        response = self.client.post(reverse("verify_email", args=[token]))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertIsNone(AccountVerification.objects.get(user=self.user).verified_at)

    def test_duplicate_email_recovery_resets_only_selected_account_then_logs_in(self):
        import re
        other = get_user_model().objects.create_user('second-legacy-account', self.user.email, 'Original-password-83!')
        old_other_hash = other.password
        self.client.post(reverse('password_reset'), {'email': self.user.email})
        self.assertEqual(len(mail.outbox), 2)
        selected_message = next(message for message in mail.outbox if f'Account reference: {self.user.pk}\n' in message.body)
        link = re.search(r'http://testserver(/accounts/reset/\S+)', selected_message.body)[1]
        response = self.client.get(link)
        response = self.client.post(response.url, {'new_password1': 'Distinct-recovery-password-94!', 'new_password2': 'Distinct-recovery-password-94!'})
        self.assertRedirects(response, reverse('password_reset_complete'))
        other.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(other.password, old_other_hash)
        self.assertTrue(self.user.check_password('Distinct-recovery-password-94!'))
        self.assertEqual(self.sign_in(password='Distinct-recovery-password-94!').url, reverse('dashboard'))
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)

    def test_duplicate_email_login_uses_only_a_unique_matching_password(self):
        other = get_user_model().objects.create_user('separate-password-account', self.user.email, 'Different-secret-password-94!')
        self.assertEqual(self.sign_in(password='Different-secret-password-94!').url, reverse('dashboard'))
        self.assertEqual(int(self.client.session['_auth_user_id']), other.pk)
