import csv
import json
import re
from datetime import datetime, time, timedelta
from io import BytesIO
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST
from accounts.models import BusinessProfile
from accounts.logging import redact_account_tokens
from config.export_security import spreadsheet_row
from .models import AuditEvent, SecurityAlert, SecurityRecord, SessionSecurityRecord, SEVERITIES, ALERT_STATUSES
from .services import record_event, safe_data
from config.report_limits import MIN_REPORT_DATE, MAX_REPORT_DATE, validate_report_range
from .security import ready, administrator, transition_alert, configure_rule, revoke_sessions, session_digest, RULES

class Filters(forms.Form):
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    severity = forms.ChoiceField(choices=[('', 'All severities'), *SEVERITIES], required=False)
    event_type = forms.RegexField(regex='^[A-Z0-9_]*$', max_length=64, required=False)
    outcome = forms.ChoiceField(choices=[('', 'All outcomes'), ('success', 'Success'), ('failure', 'Failure')], required=False)
    actor = forms.IntegerField(min_value=1, max_value=2 ** 63 - 1, required=False)
    business = forms.IntegerField(min_value=1, max_value=2 ** 63 - 1, required=False)
    resource = forms.CharField(max_length=80, required=False)
    correlation = forms.UUIDField(required=False)
    status = forms.ChoiceField(choices=[('', 'All alert statuses'), *ALERT_STATUSES], required=False)

    def clean(self):
        data = super().clean()
        end = data.get('end_date') or timezone.localdate()
        # Validate before subtraction; accepted bounds also make timezone conversion safe.
        if not MIN_REPORT_DATE <= end <= MAX_REPORT_DATE:
            raise ValidationError('Choose dates between 1900-01-01 and 9998-12-31.')
        start = data.get('start_date') or max(MIN_REPORT_DATE, end - timedelta(days=29))
        validate_report_range(start, end, max_days=365)
        data.update(start_date=start, end_date=end)
        return data

def scope(request, model, lookup='business'):
    qs = model.objects.all()
    if administrator(request.user):
        return qs
    business = BusinessProfile.objects.filter(user=request.user).first()
    return qs.filter(**{lookup: business}) if business else qs.none()

def event_filters(request):
    form = Filters(request.GET)
    qs = scope(request, AuditEvent).select_related('user', 'business', 'security_record')
    if not form.is_valid():
        return (form, qs.none())
    data = form.cleaned_data
    if data.get('business'):
        if not administrator(request.user) and (not BusinessProfile.objects.filter(pk=data['business'], user=request.user).exists()):
            form.add_error('business', 'Use your own workspace.')
            return (form, qs.none())
        qs = qs.filter(business_id=data['business'])
    start = timezone.make_aware(datetime.combine(data['start_date'], time.min))
    end = timezone.make_aware(datetime.combine(data['end_date'] + timedelta(days=1), time.min))
    qs = qs.filter(created_at__gte=start, created_at__lt=end)
    if data.get('severity'):
        qs = qs.filter(security_record__severity=data['severity'])
    if data.get('event_type') == 'LEGACY_UNSEALED':
        qs = qs.filter(security_record__isnull=True)
    elif data.get('event_type'):
        qs = qs.filter(security_record__event_type=data['event_type'])
    if data.get('outcome'):
        qs = qs.filter(success=data['outcome'] == 'success')
    if data.get('actor'):
        qs = qs.filter(user_id=data['actor'])
    if data.get('resource'):
        qs = qs.filter(object_type=data['resource'])
    if data.get('correlation'):
        qs = qs.filter(security_record__correlation_id=data['correlation'])
    return (form, qs.order_by('-created_at', '-pk'))

def export_text(value):
    return re.sub('[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]', '[control]', redact_account_tokens(value))

def event_row(event):
    sealed = getattr(event, 'security_record', None)
    return {'id': event.pk, 'at': event.created_at.isoformat(), 'actor': export_text(event.actor_label), 'type': sealed.event_type if sealed else 'LEGACY_UNSEALED', 'severity': sealed.severity if sealed else 'UNCLASSIFIED', 'sequence': sealed.sequence if sealed else '', 'correlation': str(sealed.correlation_id) if sealed and sealed.correlation_id else '', 'resource': event.object_type, 'resource_id': event.object_id, 'outcome': 'success' if event.success else 'failure', 'description': export_text(event.description), 'changed_fields': ', '.join(sorted(set(safe_data(event.before)) | set(safe_data(event.after)))), 'digest': sealed.digest if sealed else ''}

def unavailable(request):
    return render(request, 'audit/monitor_unavailable.html', status=503)

@login_required
@never_cache
@require_GET
def dashboard(request):
    if not ready(request):
        return unavailable(request)
    form, events = event_filters(request)
    page = Paginator(events, 25).get_page(request.GET.get('page'))
    alerts = scope(request, SecurityAlert)
    if form.is_valid():
        data = form.cleaned_data
        alerts = alerts.filter(created_at__date__gte=data['start_date'], created_at__date__lte=data['end_date'])
        if data.get('severity'):
            alerts = alerts.filter(severity=data['severity'])
        if data.get('status'):
            alerts = alerts.filter(status=data['status'])
        if data.get('business') and (not form.errors):
            alerts = alerts.filter(business_id=data['business'])
    else:
        alerts = alerts.none()
    counts = events.aggregate(total=Count('pk'), failures=Count('pk', filter=Q(action='failed_login')), logins=Count('pk', filter=Q(action='login')), denials=Count('pk', filter=Q(security_record__event_type__in=['ACCESS_PERMISSION_DENIED', 'ACCESS_RESOURCE_NOT_FOUND', 'ACCESS_INVALID_REQUEST', 'FILE_ACCESS_DENIED'])), exports=Count('pk', filter=Q(security_record__event_type='EXPORT_GENERATED')), legacy=Count('pk', filter=Q(security_record__isnull=True)))
    clean_query = request.GET.copy()
    clean_query.pop('page', None)
    clean_query.pop('alert_page', None)
    return render(request, 'audit/monitor_dashboard.html', {'form': form, 'page': page, 'rows': [event_row(e) for e in page], 'alerts': Paginator(alerts, 20).get_page(request.GET.get('alert_page')), 'alert_count': alerts.count(), 'open_alerts': alerts.filter(status__in=['OPEN', 'ACKNOWLEDGED', 'INVESTIGATING']).count(), 'high_alerts': alerts.filter(severity__in=['HIGH', 'CRITICAL']).count(), 'counts': counts, 'platform': administrator(request.user), 'filters_query': clean_query.urlencode()})

@login_required
@never_cache
@require_GET
def event_detail(request, event_id):
    if not ready(request):
        return unavailable(request)
    event = get_object_or_404(scope(request, AuditEvent).select_related('security_record', 'business'), pk=event_id)
    related = scope(request, AuditEvent).filter(Q(object_type=event.object_type, object_id=event.object_id) | Q(related_object_type=event.object_type, related_object_id=event.object_id)).exclude(pk=event.pk).select_related('security_record')[:20] if event.object_id else []
    return render(request, 'audit/monitor_event.html', {'row': event_row(event), 'before': safe_data(event.before), 'after': safe_data(event.after), 'related': [event_row(e) for e in related]})

@login_required
@never_cache
@require_GET
def alert_detail(request, alert_id):
    if not ready(request):
        return unavailable(request)
    alert = get_object_or_404(scope(request, SecurityAlert), pk=alert_id)
    events = scope(request, AuditEvent).filter(security_record__pk__in=alert.evidence[:50]).select_related('security_record')
    return render(request, 'audit/monitor_alert.html', {'alert': alert, 'rows': [event_row(e) for e in events], 'history': Paginator(alert.history.select_related('actor').all(), 25).get_page(request.GET.get('page')), 'can_manage': administrator(request.user), 'statuses': ALERT_STATUSES[1:]})

@login_required
@require_POST
def alert_transition(request, alert_id):
    if not ready(request):
        return unavailable(request)
    if not administrator(request.user):
        raise PermissionDenied
    get_object_or_404(SecurityAlert, pk=alert_id)
    try:
        transition_alert(request, alert_id, request.POST.get('status'), request.POST.get('reason', ''))
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    return redirect('security_alert_detail', alert_id=alert_id)

@login_required
@never_cache
def rules(request):
    if request.method not in ('GET', 'POST'):
        return HttpResponse(status=405)
    if not ready(request):
        return unavailable(request)
    if not administrator(request.user):
        raise PermissionDenied
    if request.method == 'POST':
        try:
            configure_rule(request, request.POST.get('rule_id'), int(request.POST.get('threshold', '')), int(request.POST.get('window_seconds', '')), request.POST.get('enabled') == 'on')
        except (ValueError, ValidationError) as exc:
            messages.error(request, 'Choose a supported bounded rule configuration.')
        else:
            messages.success(request, 'Security rule configuration recorded.')
        return redirect('security_rules')
    from .models import SecurityRuleConfiguration
    overrides = {r.rule_id: r for r in SecurityRuleConfiguration.objects.filter(rule_id__in=RULES)}
    rows = [{'id': key, **values, 'override': overrides.get(key)} for key, values in RULES.items()]
    return render(request, 'audit/monitor_rules.html', {'rules': rows})

@login_required
@never_cache
@sensitive_post_parameters('password')
def sessions(request):
    if request.method not in ('GET', 'POST'):
        return HttpResponse(status=405)
    if not ready(request):
        return unavailable(request)
    if request.method == 'POST':
        others = request.POST.get('action') == 'others'
        raw_id = request.POST.get('session_id')
        if not others:
            try:
                pk = int(raw_id)
            except (ValueError, TypeError):
                raise Http404 from None
            get_object_or_404(SessionSecurityRecord, pk=pk, user=request.user)
        else:
            pk = None
        try:
            revoke_sessions(request, request.POST.get('password', ''), pk, others)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, 'Owned session revocation recorded.')
        return redirect('security_sessions')
    qs = SessionSecurityRecord.objects.filter(user=request.user).order_by('-last_seen', '-pk')
    page = Paginator(qs, 20).get_page(request.GET.get('page'))
    current = session_digest(request.session.session_key)
    return render(request, 'audit/monitor_sessions.html', {'page': page, 'rows': [{'record': r, 'current': r.token_digest == current, 'expired': r.expires_at <= timezone.now()} for r in page]})

@login_required
@never_cache
@require_GET
def export(request, fmt):
    if not ready(request):
        return unavailable(request)
    if fmt not in {'csv', 'xlsx', 'pdf'}:
        raise Http404
    form, events = event_filters(request)
    alert_mode = request.GET.get('dataset') == 'alerts'
    if request.GET.get('dataset', 'events') not in ('events', 'alerts'):
        form.add_error(None, 'Unknown dataset.')
    if alert_mode and form.is_valid():
        if any((form.cleaned_data.get(k) for k in ('actor', 'event_type', 'outcome', 'resource', 'correlation'))):
            form.add_error(None, 'Alert exports support date, severity, business and status filters only.')
        events = scope(request, SecurityAlert).filter(created_at__date__gte=form.cleaned_data['start_date'], created_at__date__lte=form.cleaned_data['end_date'])
        for key in ('business', 'severity', 'status'):
            if form.cleaned_data.get(key):
                events = events.filter(**{key: form.cleaned_data[key]})
    if not form.is_valid():
        return render(request, 'audit/monitor_export_error.html', {'error': 'Choose valid filters and a range of at most 366 days.'}, status=400)
    max_id = events.order_by('-pk').values_list('pk', flat=True).first() or 0
    events = events.filter(pk__lte=max_id)
    count = events.count()
    if count > 10000:
        return render(request, 'audit/monitor_export_error.html', {'error': 'Narrow the scope: at most 10,000 events per monitoring export.'}, status=400)
    headers = ['Event ID', 'Timestamp', 'Actor', 'Type', 'Severity', 'Sequence', 'Request correlation', 'Resource type', 'Resource ID', 'Outcome', 'Description', 'Changed fields (full deltas retained in audit detail)', 'Chain digest']
    keys = ['id', 'at', 'actor', 'type', 'severity', 'sequence', 'correlation', 'resource', 'resource_id', 'outcome', 'description', 'changed_fields', 'digest']
    if alert_mode:
        headers = ['Alert ID', 'Timestamp', 'Rule', 'Severity', 'Status', 'Summary', 'Evidence record IDs']

    def rows():
        for event in events.iterator(chunk_size=200):
            if alert_mode:
                yield [event.pk, event.created_at.isoformat(), event.rule_id, event.severity, event.status, export_text(event.summary), json.dumps(event.evidence[:50])]
            else:
                data = event_row(event)
                yield [data[key] for key in keys]
    if fmt == 'csv':

        class Echo:

            def write(self, value):
                return value
        writer = csv.writer(Echo())

        def stream():
            yield writer.writerow(headers)
            for row in rows():
                yield writer.writerow(spreadsheet_row(row))
        response = StreamingHttpResponse(stream(), content_type='text/csv; charset=utf-8')
    elif fmt == 'xlsx':
        from openpyxl import Workbook
        from taxation.preparation_exports import validate_excel_row
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet('Security evidence')
        sheet.append(headers)
        for row in rows():
            validate_excel_row(row)
            sheet.append(spreadsheet_row(row))
        output = BytesIO()
        workbook.save(output)
        response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    else:
        from invoices.pdf_generator import render_report_pdf, PDFGenerationError
        if count > 100:
            return render(request, 'audit/monitor_export_error.html', {'error': 'PDF supports at most 100 evidence summaries; use CSV/Excel for detail.'}, status=400)
        pdf_rows = [[e.pk, e.created_at.isoformat(), e.rule_id if alert_mode else event_row(e)['type'], e.severity if alert_mode else event_row(e)['severity'], export_text(e.summary if alert_mode else e.description)] for e in events]
        try:
            content = render_report_pdf('Security evidence summary', ['Event', 'Timestamp', 'Type', 'Severity', 'Description'], pdf_rows, 'Filtered event references, not a full financial ledger or proof against a privileged database administrator. Full deltas remain in authorized audit detail.')
        except PDFGenerationError:
            return render(request, 'audit/monitor_export_error.html', {'error': 'PDF content exceeds safe limits; use CSV/Excel.'}, status=400)
        response = HttpResponse(content, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="security-evidence.{fmt}"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    record_event(request=request, action='export', module='monitoring', description='Filtered security evidence generated; transport receipt is not asserted.', metadata={'format': fmt, 'count': count, 'max_event_id': max_id, 'dataset': 'alerts' if alert_mode else 'events'}, event_type='EXPORT_GENERATED')
    return response
