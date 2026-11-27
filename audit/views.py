from config.export_security import spreadsheet_row, sanitize_workbook
import csv
import json
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Min, Q
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET
from openpyxl import Workbook

from accounts.models import BusinessParty, BusinessProfile
from invoices.models import Invoice, InvoicePayment
from .services import record_event, safe_data
from accounts.logging import redact_account_tokens
from .models import AuditEvent


def _business(request):
    return BusinessProfile.objects.filter(user=request.user).first()


def _filtered_events(request, business):
    qs = AuditEvent.objects.filter(business=business).select_related("user", "business").order_by("-created_at", "-pk")
    params = request.GET
    start_text, end_text = params.get("start_date", ""), params.get("end_date", "")
    date_error = ""
    try:
        start = date.fromisoformat(start_text) if start_text else None
        end = date.fromisoformat(end_text) if end_text else None
        if start and end and start > end:
            raise ValueError
    except ValueError:
        start = end = None
        start_text = end_text = ""
        date_error = "Enter a valid date range."
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    report_type = params.get("report_type", "")
    if report_type == "daily" and not start and not end:
        qs = qs.filter(created_at__date=timezone.localdate())
    elif report_type == "monthly" and not start and not end:
        qs = qs.filter(created_at__date__gte=timezone.localdate().replace(day=1))
    elif report_type == "financial_changes":
        qs = qs.filter(module__in=("invoices", "expenses", "payments"), action__in=("update", "payment", "payment_reverse"))
    elif report_type == "invoice_modifications":
        qs = qs.filter(module="invoices", action__in=("create", "update", "verify", "cancel", "delete"))
    elif report_type == "expense_modifications":
        qs = qs.filter(module="expenses", action__in=("create", "update", "cancel"))
    elif report_type == "payment_activity":
        qs = qs.filter(module="payments", action__in=("payment", "payment_reverse", "update"))
    elif report_type == "security":
        qs = qs.filter(Q(success=False) | Q(action__in=("failed_login", "security", "delete_attempt")))
    elif report_type == "complete":
        report_type = ""
    elif report_type not in {"daily", "monthly", "financial_changes", "invoice_modifications", "expense_modifications", "payment_activity", "security", "user_wise", "module_wise"}:
        report_type = ""
    module = params.get("module", "")
    available_modules = AuditEvent.objects.filter(business=business).values_list("module", flat=True).distinct()
    if module and module in available_modules:
        qs = qs.filter(module=module)
    else:
        module = ""
    action = params.get("action", "")
    if action and action in dict(AuditEvent.ACTION_CHOICES):
        qs = qs.filter(action=action)
    else:
        action = ""
    result = params.get("result", "")
    if result in {"success", "failure"}:
        qs = qs.filter(success=(result == "success"))
    else:
        result = ""
    user_id = params.get("user", "")
    if user_id.isdigit():
        qs = qs.filter(user_id=int(user_id))
    else:
        user_id = ""
    query = params.get("q", "").strip()[:100]
    if query:
        qs = qs.filter(description__icontains=query) | qs.filter(object_id__icontains=query)
    sort = params.get("sort", "newest")
    sort_fields = {"newest": ("-created_at", "-pk"), "oldest": ("created_at", "pk"), "module": ("module", "-created_at"), "action": ("action", "-created_at"), "user": ("actor_label", "-created_at")}
    if report_type == "module_wise" and not params.get("sort"):
        sort = "module"
    elif report_type == "user_wise" and not params.get("sort"):
        sort = "user"
    if sort not in sort_fields:
        sort = "newest"
    qs = qs.order_by(*sort_fields[sort])
    return qs, {"start_date": start_text, "end_date": end_text, "module": module,
                "action": action, "result": result, "user_id": user_id, "q": query,
                "date_error": date_error, "report_type": report_type, "sort": sort}


@login_required
@require_GET
def audit_dashboard_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    today = timezone.localdate()
    events = AuditEvent.objects.filter(business=business)
    start_text, end_text = request.GET.get("start_date", ""), request.GET.get("end_date", "")
    try:
        start = date.fromisoformat(start_text) if start_text else today.replace(day=1)
        end = date.fromisoformat(end_text) if end_text else today
        if start > end:
            raise ValueError
    except ValueError:
        start, end = today.replace(day=1), today
        start_text = end_text = ""
    period_events = events.filter(created_at__date__gte=start, created_at__date__lte=end)
    day_start = today
    week_start = today.fromordinal(today.toordinal() - today.weekday())
    month_start = today.replace(day=1)
    module_counts = list(period_events.values("module").annotate(total=Count("id")).order_by("module"))
    action_counts = list(period_events.values("action").annotate(total=Count("id")).order_by("action"))
    user_counts = list(period_events.values("actor_label").annotate(total=Count("id")).order_by("-total")[:8])
    daily = list(period_events.values("created_at__date").annotate(total=Count("id")).order_by("created_at__date"))
    security_daily = list(period_events.filter(Q(success=False) | Q(action__in=("failed_login", "security"))).values("created_at__date").annotate(total=Count("id")).order_by("created_at__date"))
    return render(request, "audit/dashboard.html", {
        "total": period_events.count(),
        "today_total": period_events.filter(created_at__date=day_start).count(),
        "week_total": period_events.filter(created_at__date__gte=week_start, created_at__date__lte=today).count(),
        "month_total": period_events.filter(created_at__date__gte=month_start, created_at__date__lte=today).count(),
        "login_total": period_events.filter(action__in=("login", "logout", "failed_login")).count(),
        "create_total": period_events.filter(action="create").count(),
        "update_total": period_events.filter(action="update").count(),
        "cancel_total": period_events.filter(action__in=("cancel", "delete_attempt")).count(),
        "security_total": period_events.filter(Q(success=False) | Q(action__in=("failed_login", "security"))).count(),
        "kpis": [
            ("Events in range", period_events.count()),
            ("Today", period_events.filter(created_at__date=day_start).count()),
            ("This week", period_events.filter(created_at__date__gte=week_start, created_at__date__lte=today).count()),
            ("This month", period_events.filter(created_at__date__gte=month_start, created_at__date__lte=today).count()),
            ("Logins", period_events.filter(action__in=("login", "logout")).count()),
            ("Creates", period_events.filter(action="create").count()),
            ("Updates", period_events.filter(action="update").count()),
            ("Security events", period_events.filter(Q(success=False) | Q(action__in=("failed_login", "security"))).count()),
        ],
        "recent": period_events.select_related("user")[:12],
        "module_counts": module_counts,
        "action_counts": action_counts,
        "user_counts": user_counts,
        "daily": daily,
        "chart_data": {
            "daily": {"labels": [row["created_at__date"].isoformat() for row in daily], "values": [row["total"] for row in daily]},
            "modules": {"labels": [row["module"] for row in module_counts], "values": [row["total"] for row in module_counts]},
            "actions": {"labels": [row["action"].replace("_", " ").title() for row in action_counts], "values": [row["total"] for row in action_counts]},
            "users": {"labels": [row["actor_label"] or "System" for row in user_counts], "values": [row["total"] for row in user_counts]},
            "security": {"labels": [row["created_at__date"].isoformat() for row in security_daily], "values": [row["total"] for row in security_daily]},
        },
        "start_date": start_text or start.isoformat(), "end_date": end_text or end.isoformat(),
    })


@login_required
@require_GET
def audit_log_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    from django.core.paginator import Paginator
    events, filters = _filtered_events(request, business)
    modules = AuditEvent.objects.filter(business=business).values_list("module", flat=True).distinct().order_by("module")
    user_model = get_user_model()
    users = user_model.objects.filter(business_profile__audit_events__business=business).distinct().order_by("username")
    page_obj = Paginator(events, 50).get_page(request.GET.get("page"))
    return render(request, "audit/audit_log.html", {
        "page_obj": page_obj, "events": page_obj.object_list, "filters": filters,
        "modules": modules, "actions": AuditEvent.ACTION_CHOICES, "users": users,
    })


@login_required
@require_GET
def audit_detail_view(request, event_id):
    business = _business(request)
    if business is None:
        return redirect("profile")
    event = get_object_or_404(AuditEvent.objects.select_related("business", "user"), pk=event_id, business=business)
    if event.object_id:
        related = AuditEvent.objects.filter(business=business).filter(
            Q(object_type=event.object_type, object_id=event.object_id)
            | Q(related_object_type=event.object_type, related_object_id=event.object_id)
        ).exclude(pk=event.pk)[:20]
    else:
        related = []
    related_url = None
    if event.object_type == "invoices.invoice" and event.object_id.isdigit():
        if Invoice.objects.filter(pk=int(event.object_id), user=request.user).exists():
            from django.urls import reverse
            related_url = reverse("expense_detail", args=[event.object_id]) if Invoice.objects.filter(pk=int(event.object_id), invoice_type="expense", user=request.user).exists() else reverse("invoice_detail", args=[event.object_id])
    elif event.object_type == "accounts.businessparty" and event.object_id.isdigit():
        if BusinessParty.objects.filter(pk=int(event.object_id), business=business).exists():
            from django.urls import reverse
            related_url = reverse("party_detail", args=[event.object_id])
    elif event.object_type == "invoices.invoicepayment" and event.object_id.isdigit():
        payment = InvoicePayment.objects.filter(pk=int(event.object_id), user=request.user).select_related("invoice").first()
        if payment:
            from django.urls import reverse
            related_url = reverse("expense_detail", args=[payment.invoice_id]) if payment.invoice.invoice_type == "expense" else reverse("invoice_detail", args=[payment.invoice_id])
    elif event.object_type == "taxation.reconciliationrecord" and event.object_id.isdigit():
        from taxation.models import ReconciliationRecord
        reconciliation_record = ReconciliationRecord.objects.filter(pk=int(event.object_id), run__user=request.user).first()
        if reconciliation_record:
            from django.urls import reverse
            related_url = reverse("reconciliation_run", args=[reconciliation_record.run_id])
    event.before, event.after = safe_data(event.before), safe_data(event.after)
    event.description = redact_account_tokens(event.description)
    keys = event.before.keys() | event.after.keys()
    changes = [{"field": key, "before": event.before.get(key), "after": event.after.get(key)} for key in sorted(keys)]
    return render(request, "audit/event_detail.html", {"event": event, "related": related, "related_url": related_url, "changes": changes})


@login_required
@require_GET
def audit_activity_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    events, filters = _filtered_events(request, business)
    summaries = list(events.values("actor_label").annotate(total=Count("id"), last_activity=Max("created_at")).order_by("-total")[:100])
    return render(request, "audit/activity.html", {"events": events[:250], "filters": filters, "summaries": summaries})


@login_required
@require_GET
def audit_security_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    events, filters = _filtered_events(request, business)
    events = (events.filter(success=False) | events.filter(action__in=("failed_login", "security", "delete_attempt"))).order_by("-created_at", "-pk")
    repeated = list(events.exclude(ip_address__isnull=True).values("ip_address").annotate(attempts=Count("id"), last_seen=Max("created_at")).filter(attempts__gte=3).order_by("-attempts")[:20])
    return render(request, "audit/security.html", {"events": events[:250], "filters": filters, "repeated": repeated})


@login_required
@require_GET
def audit_export_view(request, export_format):
    business = _business(request)
    if business is None:
        return redirect("profile")
    if export_format not in {"csv", "xlsx", "pdf"}:
        raise Http404
    events, filters = _filtered_events(request, business)
    if filters["date_error"]:
        return HttpResponse("Choose a valid date range.",status=400)
    max_event_id = events.order_by("-pk").values_list("pk", flat=True).first() or 0
    events = events.filter(pk__lte=max_event_id)
    export_count = events.count()
    bounds = events.aggregate(first=Min("created_at"), last=Max("created_at"))
    if export_count > 10000 or (bounds["first"] and (bounds["last"]-bounds["first"]).days > 365):
        return HttpResponse("Narrow exports to 10,000 events within 366 days.",status=400)
    if export_format == "pdf" and export_count > 100:
        return HttpResponse("PDF supports 100 event summaries; use CSV/Excel.",status=400)
    headers = ["Date/time", "User", "Module", "Action", "Description", "Record type", "Record ID", "Success", "Before", "After"]
    def display_values(values):
        formatted = {}
        for key, value in safe_data(values).items():
            if any(token in key.lower() for token in ("amount", "total", "tax", "gst", "discount", "rate")):
                try:
                    formatted[key] = f"₹{Decimal(str(value)):,.2f}"
                    continue
                except (TypeError, ValueError, ArithmeticError):
                    pass
            formatted[key] = value
        return json.dumps(formatted, ensure_ascii=False, sort_keys=True)

    def iter_rows():
        for event in events.iterator(chunk_size=500):
            yield [event.created_at.isoformat(), redact_account_tokens(event.actor_label), event.module, event.action, redact_account_tokens(event.description),
                   event.object_type, event.object_id, "Success" if event.success else "Failure",
                   display_values(event.before), display_values(event.after)]
    if export_format == "csv":
        class Echo:
            def write(self, value):
                return value
        writer = csv.writer(Echo())
        def csv_stream():
            yield writer.writerow(headers)
            for row in iter_rows():
                yield writer.writerow(spreadsheet_row(row))
        response = StreamingHttpResponse(csv_stream(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="audit-report.csv"'
        record_event(request=request, business=business, action="export", module="reports", description="Audit activity exported as CSV.", after={"format": "csv", "record_count": export_count})
        return response
    if export_format == "xlsx":
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Audit events")
        sheet.append(headers)
        for row in iter_rows():
            sheet.append(spreadsheet_row(row))
        buffer = BytesIO()
        workbook.save(buffer)
        response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = 'attachment; filename="audit-report.xlsx"'
        record_event(request=request, business=business, action="export", module="reports", description="Audit activity exported as Excel.", after={"format": "xlsx", "record_count": export_count})
        return response
    from invoices.pdf_generator import render_report_pdf, PDFGenerationError
    try:
        content = render_report_pdf("Audit activity report",["Timestamp","Actor/module/action","Record and changes"],[[row[0],row[1]+" / "+row[2]+" / "+row[3],"Record: "+row[5]+" "+row[6]+" | "+row[4]+" | Before: "+row[8]+" | After: "+row[9]] for row in iter_rows()],"Authorized, redacted audit changes. CSV/Excel provide structured field values.")
    except PDFGenerationError:
        return HttpResponse("PDF exceeds safe limits; use CSV/Excel.",status=400)
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="audit-report.pdf"'
    record_event(request=request, business=business, action="export", module="reports", description="Audit activity exported as PDF.", after={"format": "pdf", "record_count": export_count})
    return response
