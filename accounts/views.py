import hashlib

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import PasswordChangeView, PasswordResetConfirmView, PasswordResetView
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST, require_http_methods

from audit.services import record_event
from .backends import email_candidates, normalize_email
from .forms import BusinessProfileForm, EmailLoginForm, RegisterForm, ResendVerificationForm
from .models import AccountVerification, BusinessProfile
from .emailing import expiration_description, send_account_email


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST" and limited(request, "register"):
        return render(request, "accounts/register.html", {"form": RegisterForm(), "throttled": True}, status=429)
    form = RegisterForm(request.POST if request.method == "POST" else None)

    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save()
                record_event(request=request, user=user, action="create", module="accounts", description="User registered.", obj=user)
        except IntegrityError:
            form.add_error(None, "These account details are already registered. Please log in or recover your account.")
            return render(request, "accounts/register.html", {"form": form})
        send_verification(request, user)
        return redirect("registration_done")

    return render(request, "accounts/register.html", {"form": form})


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST" and limited(request, "login"):
        return render(request, "accounts/login.html", {"form": EmailLoginForm(request), "throttled": True}, status=429)

    form = EmailLoginForm(request, data=request.POST if request.method == "POST" else None)
    next_url = safe_next(request)

    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        login(request, user)  # Django rotates the session key here (fixation prevention).
        messages.success(request, "Welcome back!")
        return redirect(next_url or "dashboard")

    return render(request, "accounts/login.html", {"form": form, "next": next_url, "expired": request.GET.get("expired") in ("idle", "absolute")})


def safe_next(request):
    """Return a same-origin relative path from ?next/POST next, else None."""
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if (candidate.startswith("/") and not candidate.startswith(("//", "/\\"))
            and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure())
            and not candidate.startswith(reverse("logout"))):
        return candidate
    return None


@require_http_methods(["GET", "POST"])
@login_required
def session_keepalive(request):
    """GET: passive remaining-time check (sent with X-Session-Activity: passive, never extends).
    POST: the user's explicit "Stay signed in" choice, which records activity."""
    from .session_policy import ACTIVITY_KEY, seconds_remaining
    import time
    if request.method == "POST":
        request.session[ACTIVITY_KEY] = int(time.time())
    return JsonResponse({"remaining": seconds_remaining(request.session)})


@login_required
def dashboard_view(request):
    return redirect("analytics_dashboard")


@require_POST
@login_required
def logout_view(request):
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("login")


@login_required
def business_settings_view(request):
    profile = BusinessProfile.objects.filter(user=request.user).first()

    if profile is None:
        profile = BusinessProfile.objects.create(
            user=request.user,
            business_name=request.user.username,
            email=request.user.email or "",
        )

    if request.method == "POST":
        before = {field: getattr(profile, field) for field in BusinessProfileForm._meta.fields}
        form = BusinessProfileForm(request.POST, instance=profile)

        if form.is_valid():
            with transaction.atomic():
                form.save()
                after = {field: getattr(profile, field) for field in before}
                if before != after:
                    record_event(request=request, user=request.user, business=profile, action="update", module="accounts", description="Business profile updated.", obj=profile, before=before, after=after)
            messages.success(request, "Business profile updated successfully.")
            return redirect("profile")
    else:
        form = BusinessProfileForm(instance=profile)

    return render(
        request,
        "accounts/profile.html",
        {"form": form, "profile": profile}
    )



def limited(request, purpose):
    # Never trust forwarded IP headers. Configure a shared cache with multiple workers.
    identity = request.META.get("REMOTE_ADDR", "unknown")
    key = "account-limit:" + hashlib.sha256((purpose + identity).encode()).hexdigest()
    if cache.add(key, 1, settings.ACCOUNT_RATE_LIMIT_WINDOW):
        return False
    try:
        blocked=cache.incr(key) > settings.ACCOUNT_RATE_LIMIT_ATTEMPTS
        if blocked:
            from audit.security import privacy_key
            record_event(request=request,action='security',module='accounts',description='Existing authentication request throttle reached; no account lock was applied.',metadata={'purpose':purpose},success=False,event_type='AUTH_REQUEST_THROTTLED',source='authentication',principal=privacy_key('network:'+identity))
        return blocked
    except ValueError:  # A cache entry may expire between add() and incr().
        cache.add(key, 1, settings.ACCOUNT_RATE_LIMIT_WINDOW)
        return False


def send_verification(request, user):
    token = signing.dumps({"uid": user.pk, "email": user.email}, salt="accounts.verify")
    link = request.build_absolute_uri(reverse("verify_email", args=[token]))
    return send_account_email(
        "Verify your Smart Invoice email address",
        "accounts/verification_email.txt", "accounts/verification_email.html",
        {"verification_url": link, "expiration": expiration_description(settings.EMAIL_VERIFICATION_TIMEOUT)},
        user.email,
    )


@require_http_methods(["GET", "POST"])
def resend_verification(request):
    form = ResendVerificationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        if limited(request, "verification"):
            return redirect("verification_sent")
        if form.is_valid():
            users = list(email_candidates(form.cleaned_data["email"])[:2])
            if len(users) == 1:
                account = AccountVerification.objects.select_related("user").filter(
                    user=users[0], email__iexact=form.cleaned_data["email"], verified_at__isnull=True
                ).first()
                if account:
                    send_verification(request, account.user)
            return redirect("verification_sent")
    return render(request, "accounts/resend_verification.html", {"form": form})


def verify_email(request, token):
    if request.method == "GET":
        return render(request, "accounts/verify_email.html")
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        data = signing.loads(token, salt="accounts.verify", max_age=settings.EMAIL_VERIFICATION_TIMEOUT)
        with transaction.atomic():
            account = AccountVerification.objects.select_for_update().select_related("user").get(user_id=data["uid"], email=data["email"], verified_at__isnull=True)
            if normalize_email(account.user.email) != normalize_email(data["email"]):
                raise signing.BadSignature("Verification email changed.")
            account.verified_at = timezone.now()
            account.save(update_fields=["verified_at"])
            account.user.is_active = True
            account.user.save(update_fields=["is_active"])
            record_event(request=request, user=account.user, action="security", module="accounts", description="Account email verified.",event_type="AUTH_EMAIL_VERIFIED",source="authentication")
    except (signing.BadSignature, AccountVerification.DoesNotExist, KeyError):
        messages.error(request, "This verification link is invalid or expired. Request another email.")
    else:
        messages.success(request, "Email verified. You can now log in.")
    return redirect("login")


class SafePasswordResetForm(PasswordResetForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].label = "Email address"
        self.fields["email"].widget.attrs.update({"autocomplete": "email", "placeholder": "you@company.com", "autofocus": True})

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])

    def get_users(self, email):
        # Match Django recovery semantics: the mailbox can recover each eligible
        # account separately. Never silently discard legacy duplicate-email accounts.
        return (user for user in email_candidates(email).filter(is_active=True)
                if user.has_usable_password())

    def send_mail(self, subject_template_name, email_template_name, context,
                  from_email, to_email, html_email_template_name=None):
        context = {**context, "expiration": expiration_description(settings.PASSWORD_RESET_TIMEOUT)}
        user = context["user"]
        profile = BusinessProfile.objects.filter(user=user).first()
        context["workspace_name"] = profile.business_name if profile else "Smart Invoice workspace"
        context["account_reference"] = user.pk
        return send_account_email(
            render_to_string(subject_template_name, context), email_template_name,
            html_email_template_name, context, to_email, from_email,
        )


@method_decorator(sensitive_post_parameters(), name="dispatch")
class AccountPasswordResetView(PasswordResetView):
    form_class = SafePasswordResetForm
    template_name = "accounts/security_form.html"
    email_template_name = "accounts/password_reset_email.txt"
    html_email_template_name = "accounts/password_reset_email.html"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")
    extra_context = {"heading": "Forgot your password?", "button": "Send reset link", "subtitle": "Enter your registered email address and we’ll send you a secure link to reset your password."}

    def form_valid(self,form):
        from audit.security import privacy_key
        record_event(request=self.request,action='security',module='accounts',description='Password recovery requested; account existence is not disclosed.',event_type='AUTH_PASSWORD_RESET_REQUEST',source='authentication',principal=privacy_key('identity:'+form.cleaned_data['email']))
        return super().form_valid(form)

    def post(self, request, *args, **kwargs):
        if limited(request, "reset"):
            return redirect(self.success_url)
        return super().post(request, *args, **kwargs)


class AccountPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "accounts/security_form.html"
    success_url = reverse_lazy("password_reset_complete")
    extra_context = {"heading": "Create a new password", "button": "Update password", "reset_confirm": True, "subtitle": "Choose a strong password to keep your workspace secure."}

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["new_password2"].label = "Confirm new password"
        return form

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            from .session_policy import end_session_observations
            # Django's session auth hash already rejects every earlier session; mark observations ended too.
            ended = end_session_observations(form.user)
            record_event(request=self.request, user=form.user, action="security", module="accounts", description="Password reset completed.",metadata={"earlier_logins_ended": ended},event_type="AUTH_PASSWORD_RESET_COMPLETED",source="authentication")
        return response


class AccountPasswordChangeView(PasswordChangeView):
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("profile")

    def form_valid(self, form):
        previous_key = self.request.session.session_key
        with transaction.atomic():
            response = super().form_valid(form)  # update_session_auth_hash keeps only this session valid (key rotates).
            from .session_policy import end_session_observations
            ended = end_session_observations(self.request.user, exclude_keys=(previous_key, self.request.session.session_key))
            # This browser's pre-rotation key is no longer valid either; it is not counted as another login.
            end_session_observations(self.request.user, exclude_keys=(self.request.session.session_key,))
            record_event(request=self.request, action="security", module="accounts", description="Password changed.",metadata={"other_logins_ended": ended} if ended else None,event_type="AUTH_PASSWORD_CHANGED",source="authentication")
        messages.success(self.request, "Your password was changed successfully.")
        return response
