from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponseForbidden
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from io import BytesIO
from openpyxl import load_workbook

from accounts.models import BusinessProfile
from .models import AuditEvent
from .middleware import AuditForbiddenResponseMiddleware
from .services import changed_fields, record_event


class AuditManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="audit-owner", email="owner@example.test", password="safe-password")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="Owner Business")
        self.other = User.objects.create_user(username="other-owner", password="safe-password")
        self.other_business = BusinessProfile.objects.create(user=self.other, business_name="Other Business")

    def test_change_comparison_masks_sensitive_fields(self):
        before, after = changed_fields({"total": "100", "password": "old", "notes": "bank details"}, {"total": "125", "password": "new", "notes": "new bank details"})
        self.assertEqual(before, {"total": "100"})
        self.assertEqual(after, {"total": "125"})

    def test_audit_events_are_append_only(self):
        event = record_event(user=self.user, business=self.business, action="update", module="test", description="Changed.", before={"total": "10"}, after={"total": "20"})
        event.description = "Tampered"
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()
        with self.assertRaises(ValueError):
            AuditEvent.objects.filter(pk=event.pk).update(description="Tampered by queryset")
        with self.assertRaises(ValueError):
            AuditEvent.objects.filter(pk=event.pk).delete()

    def test_audit_event_rolls_back_with_failed_business_transaction(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                record_event(user=self.user, action="create", module="test", description="Will roll back.")
                raise RuntimeError("business write failed")
        self.assertFalse(AuditEvent.objects.filter(description="Will roll back.").exists())

    def test_login_logout_and_failed_login_are_recorded_without_password(self):
        failed = self.client.post(reverse("login"), {"email": self.user.email, "password": "wrong-password"})
        self.assertEqual(failed.status_code, 200)
        event = AuditEvent.objects.get(action="failed_login", business=self.business)
        self.assertFalse(event.success)
        self.assertNotIn("password", str(event.metadata).lower())
        self.client.post(reverse("login"), {"email": self.user.email, "password": "safe-password"})
        self.assertTrue(AuditEvent.objects.filter(action="login", user=self.user).exists())
        self.client.post(reverse("logout"))
        self.assertTrue(AuditEvent.objects.filter(action="logout", user=self.user).exists())

    def test_audit_pages_and_exports_are_business_scoped(self):
        own = record_event(user=self.user, action="create", module="invoices", description="My invoice.")
        record_event(user=self.other, action="create", module="invoices", description="Other invoice.")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("audit_log")).status_code, 200)
        page = self.client.get(reverse("audit_events"))
        self.assertContains(page, "My invoice.")
        self.assertNotContains(page, "Other invoice.")
        self.assertEqual(self.client.get(reverse("audit_event_detail", args=[own.pk])).status_code, 200)
        for export_format in ("csv", "xlsx", "pdf"):
            response = self.client.get(reverse("audit_export", args=[export_format]))
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment;", response["Content-Disposition"])
            if export_format == "csv":
                self.assertIn(b"My invoice.", b"".join(response.streaming_content))
            elif export_format == "xlsx":
                workbook = load_workbook(BytesIO(response.content), read_only=True)
                self.assertEqual(workbook.active.title, "Audit events")
                self.assertGreaterEqual(sum(1 for _ in workbook.active.iter_rows()), 3)
            else:
                self.assertTrue(response.content.startswith(b"%PDF-1.4"))
        self.assertEqual(self.client.get(reverse("audit_event_detail", args=[AuditEvent.objects.filter(business=self.other_business).first().pk])).status_code, 404)

    def test_anonymous_user_cannot_access_audit(self):
        self.assertEqual(self.client.get(reverse("audit_log")).status_code, 302)

    def test_authenticated_forbidden_response_creates_security_event(self):
        request = RequestFactory().post("/private/resource/")
        request.user = self.user
        middleware = AuditForbiddenResponseMiddleware(lambda req: HttpResponseForbidden())
        self.assertEqual(middleware(request).status_code, 403)
        event = AuditEvent.objects.get(module="security", action="security", business=self.business)
        self.assertFalse(event.success)
        self.assertEqual(event.metadata["path"], "/private/resource/")
