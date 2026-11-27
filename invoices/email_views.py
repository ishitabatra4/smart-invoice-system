import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts.models import BusinessProfile
from audit.services import record_event
from .email_delivery import default_recipient, eligible_for_email, history_available, send_invoice_email
from .email_forms import InvoiceEmailForm, TOKEN_SALT
from .models import Invoice, InvoiceEmailDelivery


def invoice_email_context(invoice):
    eligible = eligible_for_email(invoice)
    ready = history_available() if eligible else False
    return {"email_eligible": eligible, "email_ready": ready,
            "email_deliveries": InvoiceEmailDelivery.objects.filter(invoice=invoice, business__user_id=invoice.user_id).select_related("initiated_by")[:20] if ready else []}


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def invoice_email_view(request, invoice_id, delivery_id=None):
    business = BusinessProfile.objects.filter(user=request.user).first()
    invoice = Invoice.objects.select_related("buyer_party__business").filter(pk=invoice_id, user=request.user).first() if business else None
    if invoice is None:
        record_event(request=request, business=business, action="security", module="invoices", description="Invoice email access denied.", success=False)
        raise Http404
    if not eligible_for_email(invoice):
        return HttpResponse("Only finalized manual sales invoices can be emailed.", status=400, content_type="text/plain")
    if not history_available():
        return HttpResponse("Invoice email delivery is unavailable until its database migration is approved and applied.", status=503, content_type="text/plain")
    retry = get_object_or_404(InvoiceEmailDelivery, pk=delivery_id, invoice=invoice, business=business) if delivery_id else None
    if retry and retry.status != "failed":
        return HttpResponse("Only a failed attempt can be retried. Successful or uncertain attempts are never retried automatically.", status=400, content_type="text/plain")
    token = signing.dumps({"key": str(uuid.uuid4()), "user": request.user.pk, "invoice": invoice.pk, "retry": retry.pk if retry else None}, salt=TOKEN_SALT)
    initial = {"recipient": retry.recipient if retry else default_recipient(invoice), "send_token": token}
    form = InvoiceEmailForm(request.POST if request.method == "POST" else None, user=request.user, invoice=invoice, retry=retry, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            result = send_invoice_email(user=request.user, invoice_id=invoice.pk,
                recipient=form.cleaned_data["recipient"], request_key=form.cleaned_data["send_token"],
                retry_id=retry.pk if retry else None, confirm_override=form.cleaned_data["confirm_override"],
                confirm_resend=form.cleaned_data["confirm_resend"], request=request)
        except ValidationError as error:
            form.add_error(None, error)
        except DatabaseError:
            form.add_error(None, "Delivery history is temporarily unavailable. No automatic retry was performed; check history before trying again.")
        else:
            if result.reused:
                messages.info(request, "This attempt has already been recorded. No new email was sent; check its history below.")
            elif result.status == "sent":
                messages.success(request, "The configured email backend accepted the message. This does not confirm inbox delivery.")
            elif result.status in {"uncertain", "sending"}:
                messages.warning(request, "The outcome is uncertain. Check the mailbox/provider before deliberately sending again; duplicates are possible.")
            elif result.status == "failed":
                messages.error(request, "The attempt failed. Review the safe category in delivery history and use Retry when ready.")
            else:
                messages.error(request, "Delivery history is unavailable. No automatic retry was performed. Review history before trying again.")
            return redirect("invoice_detail", invoice_id=invoice.pk)
    return render(request, "invoices/email_confirm.html", {"invoice": invoice, "business_name": (invoice.business_snapshot or {}).get("name") or invoice.supplier_name,
        "form": form, "retry": retry, "default_recipient": initial["recipient"]})
