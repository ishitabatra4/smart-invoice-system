from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from accounts.models import BusinessProfile
from .services import build_analytics_context, dashboard_date_range
from invoices.models import Invoice


@login_required
def dashboard_view(request):
    start, end, preset, error = dashboard_date_range(request.GET, timezone.localdate())
    context = build_analytics_context(request.user, start, end, preset, error)
    context.update({
        "profile": BusinessProfile.objects.filter(user=request.user).first(),
        "filter_start_date": start.isoformat(),
        "filter_end_date": end.isoformat(),
    })
    # Recent activity is a bounded document list, not another financial calculation.
    from invoices.date_evidence import with_invoice_dates
    recent = with_invoice_dates(Invoice.objects.filter(user=request.user)).order_by('-created_at', '-pk')
    context['recent_invoices'] = recent.filter(invoice_type__in=['sales', 'purchase'])[:6]
    context['recent_expenses'] = recent.filter(invoice_type='expense')[:6]
    from audit.security import ready
    from audit.models import SecurityAlert
    context['workspace_alerts'] = SecurityAlert.objects.filter(business__user=request.user, status__in=['OPEN', 'ACKNOWLEDGED', 'INVESTIGATING'])[:5] if ready(request) else []
    return render(request, "analytics/dashboard.html", context)
