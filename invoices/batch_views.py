from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from accounts.models import BusinessProfile
from audit.services import record_event

from .exporting import invoice_batch_workbook_response
from .models import Invoice, InvoiceBatch
from .date_evidence import with_invoice_dates


@login_required
def invoice_batch_history_view(request):
    batches = InvoiceBatch.objects.filter(user=request.user).prefetch_related("invoices")
    return render(request, "invoices/batch_history.html", {"batches": batches})


@login_required
def invoice_batch_detail_view(request, batch_id):
    batch = get_object_or_404(InvoiceBatch.objects.prefetch_related("invoices__line_items"), pk=batch_id, user=request.user)
    from .extraction_services import history_ready
    return render(request, "invoices/batch_detail.html", {"attempts":batch.extraction_attempts.all() if history_ready() else [],
        "batch": batch,
        "invoices": with_invoice_dates(Invoice.objects.filter(user=request.user, batch=batch)).prefetch_related("line_items"),
    })


@login_required
def invoice_batch_export_view(request, batch_id):
    batch = get_object_or_404(InvoiceBatch, pk=batch_id, user=request.user)
    response = invoice_batch_workbook_response(batch)
    business = BusinessProfile.objects.filter(user=request.user).first()
    record_event(
        request=request, user=request.user, business=business, action="export",
        module="reports", description=f"Invoice batch {batch.pk} exported to Excel.",
        obj=batch, after={"format": "xlsx", "invoice_count": batch.invoices.count()},
    )
    return response

