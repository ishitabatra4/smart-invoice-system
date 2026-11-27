from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import ExpenseReportFilterForm
from .reporting import export_report, report_rows
from .views import _business
from audit.services import record_event


@login_required
def expense_reports_view(request):
    business = _business(request)
    if business is None:
        return redirect("profile")
    form, rows, totals = report_rows(request.user, business, request.GET)
    return render(request, "expenses/reports.html", {
        "form": form, "rows": rows, "totals": totals,
        "is_payment_report": form.is_valid() and form.cleaned_data.get("report_type") == "payments",
    })


@login_required
def expense_report_export_view(request, export_format):
    business = _business(request)
    if business is None:
        return redirect("profile")
    if export_format not in {"xlsx", "csv", "pdf"}:
        messages.error(request, "Choose XLSX, CSV or PDF for the export.")
        return redirect("expense_reports")
    form, rows, totals = report_rows(request.user, business, request.GET)
    if not form.is_valid():
        messages.error(request, "Correct the report filters before exporting.")
        return redirect("expense_reports")
    report_type = form.cleaned_data.get("report_type") or "complete"
    response = export_report(rows, report_type, totals, export_format)
    record_event(request=request, user=request.user, business=business, action="export", module="reports", description=f"Expense {report_type.replace('_', ' ')} report exported.", after={"format": export_format, "record_count": totals.get("count", 0), "total": totals.get("total", 0), "filters": request.GET.dict()})
    return response
