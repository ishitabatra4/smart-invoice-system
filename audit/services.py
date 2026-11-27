from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from django.db import transaction
import ipaddress
import math

from accounts.models import BusinessProfile
from accounts.logging import redact_account_tokens
from .models import AuditEvent


SENSITIVE_KEYS = {
    "credential", "smtp", "api_key", "cookie", "password", "password1", "password2", "token", "secret", "authorization", "session",
    "bank", "account_number", "notes", "description", "email", "phone", "address",
}


def safe_data(data):
    budget=[5000]
    def clean(value, depth=0):
        budget[0]-=1
        if budget[0]<0 or depth>8:return '[truncated]'
        if isinstance(value,dict):
            result={}
            for index,(key,item) in enumerate(value.items()):
                if index>=64:result['__omitted_fields__']=len(value)-64;break
                key=str(key)[:100]
                if any(secret in key.lower() for secret in SENSITIVE_KEYS) or key.lower() in {'body','raw','document','extracted_text','raw_text','ocr_text'}:continue
                result[key]=clean(item,depth+1)
            return result
        if isinstance(value,(list,tuple)):
            result=[clean(item,depth+1) for item in value[:100]]
            if len(value)>100:result.append({'__omitted_items__':len(value)-100})
            return result
        if isinstance(value,(date,datetime)):return value.isoformat()
        if isinstance(value,(Decimal,UUID)):return str(value)
        if isinstance(value,str):return redact_account_tokens(value)[:500]
        if isinstance(value,float) and not math.isfinite(value):return '[nonfinite]'
        if isinstance(value,(int,float,bool)) or value is None:return value
        return '[unsupported value]'
    return clean(data or {}) if isinstance(data or {},dict) else {'value':clean(data)}


def changed_fields(before, after):
    """Return safe, field-level deltas between two flat dictionaries."""
    old, new = safe_data(before), safe_data(after)
    changed_before, changed_after = {}, {}
    for key in old.keys() | new.keys():
        if old.get(key) != new.get(key):
            changed_before[key] = old.get(key)
            changed_after[key] = new.get(key)
    return changed_before, changed_after


def _client_ip(request):
    if request is None:
        return None
    # Use REMOTE_ADDR only; forwarded headers are user-controlled unless a trusted proxy is configured.
    try:return str(ipaddress.ip_address(request.META.get("REMOTE_ADDR")))
    except (ValueError,TypeError):return None


@transaction.atomic
def record_event(*, request=None, user=None, business=None, action, module,
                 description, obj=None, before=None, after=None, metadata=None,
                 success=True, actor_label="", related_obj=None, event_type=None, severity=None, principal=None, source="application"):

    """Create one audit event inside the caller's transaction when applicable."""
    if user is None and request is not None and getattr(request, "user", None) and request.user.is_authenticated:
        user = request.user
    if business is None and user is not None:
        business = BusinessProfile.objects.filter(user=user).first()
    if obj is not None:
        object_type = obj._meta.label_lower
        object_id = str(obj.pk)
    else:
        object_type = ""
        object_id = ""
    related_object_type = related_obj._meta.label_lower if related_obj is not None else ""
    related_object_id = str(related_obj.pk) if related_obj is not None else ""
    before_data, after_data = changed_fields(before or {}, after or {})
    if not actor_label and user is not None:
        actor_label = user.get_username()
    agent = request.META.get("HTTP_USER_AGENT", "")[:500] if request else ""
    event = AuditEvent.objects.create(
        business=business,
        user=user,
        actor_label=redact_account_tokens(actor_label)[:150],
        action=action,
        module=module,
        object_type=object_type,
        object_id=object_id,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        description=redact_account_tokens(description)[:500],
        before=before_data,
        after=after_data,
        ip_address=_client_ip(request),
        user_agent=redact_account_tokens(agent),
        success=success,
        metadata=safe_data(metadata),
    )

    from .security import ready,seal_event
    if request is not None and not success and action=='security':request._audit_denial_recorded=True
    if ready(request):seal_event(event,request,event_type,severity,principal,source)
    return event
