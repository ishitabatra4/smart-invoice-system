import uuid
import logging
from django.db import OperationalError
from django.conf import settings
from django.db.models import Q
from .services import record_event


class AuditForbiddenResponseMiddleware:
    """Correlated monitoring supplements existing authorization; never probes foreign IDs."""
    def __init__(self,get_response):self.get_response=get_response

    def __call__(self,request):
        from .security import ready,session_digest,observe_session,privacy_key
        from .models import SessionSecurityRecord
        request.security_correlation_id=uuid.uuid4()
        available=ready(request)
        if available and request.user.is_authenticated and getattr(request,'session',None):
            keys={request.session.session_key,request.COOKIES.get(settings.SESSION_COOKIE_NAME)}-{None,''}
            if keys and SessionSecurityRecord.objects.filter(user=request.user,token_digest__in=[session_digest(k) for k in keys],revoked_at__isnull=False).exists():
                # The revocation itself is already audited; flush without emitting a user logout event.
                from django.contrib.auth.models import AnonymousUser
                request.session.flush();request.user=AnonymousUser()
        response=self.get_response(request)
        response['X-Request-ID']=str(request.security_correlation_id)
        authenticated=getattr(request,'user',None) and request.user.is_authenticated
        protected=request.path.startswith(('/invoices/','/expenses/','/tax/','/audit/','/admin/'))
        if authenticated and not getattr(request,'_audit_denial_recorded',False) and (response.status_code==403 or (available and protected and response.status_code in (400,404))):
            kind='FILE_ACCESS_DENIED' if any(part in request.path for part in ('/source/','/attachment/','/pdf/','/export/','/receipts/history/')) else 'ACCESS_PERMISSION_DENIED' if response.status_code==403 else 'ACCESS_INVALID_REQUEST' if response.status_code==400 else 'ACCESS_RESOURCE_NOT_FOUND'
            record_event(request=request,action='security',module='security',description='A protected request was denied or unavailable; resource existence is not asserted.',metadata={'path':request.path[:200],'method':request.method,'status_code':response.status_code},success=False,event_type=kind,source='middleware')
        if available and authenticated:
            try:observe_session(request)
            except OperationalError as exc:
                if 'locked' not in str(exc).lower():raise
                # A best-effort heartbeat must not turn a committed financial response into failure.
                # Revocation enforcement above and mandatory transactional audit writes never skip.
                response['X-Session-Observation']='deferred-busy'
                logging.getLogger(__name__).warning('Session observation deferred: database busy.')
        return response
