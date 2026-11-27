"""Authenticated, owner-scoped generated copies; original files have a separate route."""
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.utils.http import content_disposition_header
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.models import BusinessProfile
from audit.services import record_event
from .models import Invoice
from .pdf_generator import generate_invoice_pdf, invoice_pdf_filename

logger = logging.getLogger(__name__)


@login_required
@require_GET
@never_cache
def invoice_pdf_view(request, invoice_id):
    business = BusinessProfile.objects.filter(user=request.user).first()
    invoice = Invoice.objects.filter(pk=invoice_id, user=request.user).prefetch_related("payments").first() if business else None
    if invoice is None:
        # Never inspect or log another owner's invoice or disclose whether it exists.
        record_event(request=request, business=business, action="security", module="invoices", description="Generated invoice PDF access denied.", success=False)
        raise Http404
    from .adjustment_services import adjustments_available
    if adjustments_available():
        from django.db.models import prefetch_related_objects
        prefetch_related_objects([invoice], "adjustment_notes")
    else:
        invoice._prefetched_objects_cache["adjustment_notes"] = []
    preview = request.GET.get("preview") == "1"
    try:
        content = generate_invoice_pdf(invoice)
    except Exception:
        # No exception text, paths, financial data or malformed content in responses/logs.
        logger.warning("Invoice PDF rendering failed (invoice_id=%s).", invoice.pk)
        record_event(request=request, business=business, action="export", module="invoices", description="Invoice PDF rendering failed.", obj=invoice, success=False, metadata={"format": "pdf"})
        return HttpResponse("This invoice PDF could not be generated. Please review the invoice or try again later.", status=422, content_type="text/plain")
    record_event(request=request, business=business, action="export", module="invoices", description="Invoice PDF preview generated." if preview else "Invoice PDF downloaded.", obj=invoice, metadata={"format": "pdf", "mode": "preview" if preview else "download", "renderer_version": "1"})
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(not preview, invoice_pdf_filename(invoice))
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Length"] = str(len(content))
    return response
