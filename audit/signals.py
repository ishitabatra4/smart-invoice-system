from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from django.db import transaction

from accounts.models import BusinessProfile
from accounts.backends import email_candidates
from .services import record_event
from .security import privacy_key


@receiver(user_logged_in, dispatch_uid="audit.successful_login")
@transaction.atomic
def log_login(sender, request, user, **kwargs):
    record_event(request=request, user=user, action="login", module="accounts", description="User logged in.",event_type='AUTH_LOGIN_SUCCESS',source='authentication')
    from .security import ready,observe_session
    if ready(request):observe_session(request,user)


@receiver(user_logged_out, dispatch_uid="audit.successful_logout")
@transaction.atomic
def log_logout(sender, request, user, **kwargs):
    if user and user.is_authenticated:
        record_event(request=request, user=user, action="logout", module="accounts", description="User logged out.",event_type='AUTH_LOGOUT',source='authentication')
        from .security import ready,session_digest
        from .models import SessionSecurityRecord
        from django.utils import timezone
        if ready(request) and getattr(request,'session',None) and request.session.session_key:
            SessionSecurityRecord.objects.filter(user=user,token_digest=session_digest(request.session.session_key)).update(ended_at=timezone.now())


@receiver(user_login_failed, dispatch_uid="audit.failed_login")
def log_failed_login(sender, credentials, request, **kwargs):
    username_field = get_user_model().USERNAME_FIELD
    attempted = (credentials or {}).get(username_field) or (credentials or {}).get("username") or ""
    target = get_user_model().objects.filter(**{f"{username_field}__iexact": attempted}).first() if attempted else None
    if (credentials or {}).get("email"):
        candidates = list(email_candidates(credentials["email"])[:2])
        target = candidates[0] if len(candidates) == 1 else None
    business = BusinessProfile.objects.filter(user=target).first() if target else None
    record_event(
        request=request, business=business, action="failed_login", module="security",
        description="Failed sign-in attempt.", actor_label="Unsuccessful sign-in",
        metadata={"attempted_username":""}, success=False,
        event_type='AUTH_LOGIN_FAILURE',source='authentication',principal=privacy_key('identity:'+str((credentials or {}).get('email') or attempted)),
    )
