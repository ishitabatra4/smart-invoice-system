"""Server-side idle and absolute expiry for Django session authentication.

Timestamps live in the signed server-side session (never in browser storage).
`last_activity` is rewritten at most once per SESSION_ACTIVITY_UPDATE_INTERVAL,
so ordinary navigation does not write the session row on every request.
Requests marked `X-Session-Activity: passive` (background polling) are checked
for expiry but never extend the idle window.
"""
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

STARTED_KEY = "_auth_started_at"
ACTIVITY_KEY = "_auth_last_activity"
PASSIVE_HEADER = "HTTP_X_SESSION_ACTIVITY"


def start_session_clock(request):
    now = int(time.time())
    request.session[STARTED_KEY] = now
    request.session[ACTIVITY_KEY] = now


def expiry_reason(session, now=None):
    now = int(now if now is not None else time.time())
    started = session.get(STARTED_KEY)
    last = session.get(ACTIVITY_KEY)
    if not isinstance(started, int) or not isinstance(last, int):
        return None
    if now - started >= settings.SESSION_ABSOLUTE_TIMEOUT:
        return "absolute"
    if now - last >= settings.SESSION_IDLE_TIMEOUT:
        return "idle"
    return None


def seconds_remaining(session, now=None):
    now = int(now if now is not None else time.time())
    started = session.get(STARTED_KEY, now)
    last = session.get(ACTIVITY_KEY, now)
    return max(0, min(settings.SESSION_IDLE_TIMEOUT - (now - last), settings.SESSION_ABSOLUTE_TIMEOUT - (now - started)))


def end_session_observations(user, *, exclude_keys=()):
    """Mark retained session observations ended (no deletion; raw keys never stored)."""
    from audit.models import SessionSecurityRecord
    from audit.security import session_digest
    qs = SessionSecurityRecord.objects.filter(user=user, ended_at__isnull=True, revoked_at__isnull=True)
    keys = [key for key in exclude_keys if key]
    if keys:
        qs = qs.exclude(token_digest__in=[session_digest(key) for key in keys])
    return qs.update(ended_at=timezone.now())


def wants_json(request):
    return (request.META.get(PASSIVE_HEADER) == "passive"
            or request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", ""))


class SessionExpiryMiddleware:
    """Placed after AuthenticationMiddleware and MessageMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            response = self.enforce(request, user)
            if response is not None:
                return response
        return self.get_response(request)

    def enforce(self, request, user):
        session = request.session
        now = int(time.time())
        if not isinstance(session.get(STARTED_KEY), int):
            # Sessions created before this policy start their clock now (no retroactive logout).
            start_session_clock(request)
            return None
        reason = expiry_reason(session, now)
        if reason is None:
            passive = request.META.get(PASSIVE_HEADER) == "passive"
            if not passive and now - session[ACTIVITY_KEY] >= settings.SESSION_ACTIVITY_UPDATE_INTERVAL:
                session[ACTIVITY_KEY] = now
            return None
        self.expire(request, user, reason)
        login_url = reverse(settings.LOGIN_URL)
        if wants_json(request):
            return JsonResponse({"session_expired": True, "reason": reason, "login_url": login_url}, status=401)
        messages.warning(request, "Your session expired after inactivity. Please sign in again." if reason == "idle"
                         else "Your session reached its maximum duration. Please sign in again.")
        query = {"expired": reason}
        if request.method == "GET":
            query["next"] = request.get_full_path()
        response = redirect(f"{login_url}?{urlencode(query)}")
        response["Cache-Control"] = "private, no-store"
        return response

    def expire(self, request, user, reason):
        from audit.security import ready, session_digest
        from audit.services import record_event
        from audit.models import SessionSecurityRecord
        if ready(request) and request.session.session_key:
            SessionSecurityRecord.objects.filter(user=user, token_digest=session_digest(request.session.session_key),
                                                 ended_at__isnull=True).update(ended_at=timezone.now())
        record_event(request=request, user=user, action="security", module="accounts",
                     description="Authenticated session expired and was invalidated on the server.",
                     metadata={"reason": reason}, event_type="AUTH_SESSION_EXPIRED", source="authentication")
        # Flush without user_logged_out, so an expiry is not also recorded as a user logout.
        request.session.flush()
        request.user = AnonymousUser()


def session_timeout_context(request):
    """Template values for the optional pre-expiry warning (no secrets)."""
    session = getattr(request, "session", None)
    if session is None or not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"session_policy": {
        "idle": settings.SESSION_IDLE_TIMEOUT,
        "remaining": seconds_remaining(session),
        "warning": settings.SESSION_WARNING_SECONDS,
    }}
