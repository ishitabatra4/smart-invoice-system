"""Synchronous, owner-authorized delivery with durable attempts and no financial writes."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import logging
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.validators import validate_email
from django.db import connection, DatabaseError, IntegrityError, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.emailing import delivery_error
from accounts.models import BusinessProfile
from audit.services import record_event
from .ledger import payment_total
from .models import Invoice, InvoiceEmailDelivery, CommercialDocument, CommercialEmailDelivery
from .pdf_generator import generate_invoice_pdf, indian_number, invoice_pdf_filename

logger = logging.getLogger(__name__)
COOLDOWN = timedelta(seconds=60)
HOURLY_BUSINESS_LIMIT = 20
HOURLY_INVOICE_LIMIT = 5
MAX_ATTEMPTS = 3
UNCERTAIN_ERRORS = {"timeout", "connection_failed", "smtp_rejected", "delivery_failed"}


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    delivery_id: int = None
    error_category: str = ""
    reused: bool = False


def eligible_for_email(invoice):
    from .models import InvoiceAdjustmentNote
    if isinstance(invoice, InvoiceAdjustmentNote):
        return invoice.status == "finalized" and invoice.finalized_at is not None
    if isinstance(invoice, CommercialDocument):
        return invoice.effective_status in {"sent", "accepted"} and invoice.issued_at is not None
    return (invoice.entry_mode == "manual" and invoice.invoice_type == "sales"
            and invoice.document_state == "finalized" and invoice.status in {"verified", "paid"})


def history_available():
    try:
        return InvoiceEmailDelivery._meta.db_table in connection.introspection.table_names()
    except DatabaseError:
        return False


def recipient_address(value):
    if not isinstance(value, str) or len(value) > 254 or any(char in value for char in "\r\n"):
        raise ValidationError("Enter one valid recipient email address.")
    value = value.strip()
    validate_email(value)
    return value


def default_recipient(invoice):
    snapshot = invoice.party_snapshot if isinstance(invoice.party_snapshot, dict) else {}
    value = snapshot.get("email")
    if not value and invoice.buyer_party_id and invoice.buyer_party.business.user_id == invoice.user_id:
        value = invoice.buyer_party.email
    try:
        return recipient_address(value or "")
    except ValidationError:
        return ""


def email_context(invoice):
    from .models import InvoiceAdjustmentNote
    if isinstance(invoice, InvoiceAdjustmentNote):
        return {"business_name": invoice.business_snapshot.get("name") or "Business", "customer_name": invoice.party_snapshot.get("name") or "Party",
            "invoice_number": invoice.number, "invoice_date": invoice.document_date, "total": "INR " + indian_number(invoice.total_amount),
            "document_label": invoice.get_kind_display(), "adjustment": True, "original_reference": invoice.original_snapshot.get("number"),
            "business_email": invoice.business_snapshot.get("email", ""), "business_phone": invoice.business_snapshot.get("phone", "")}
    business = invoice.business_snapshot if isinstance(invoice.business_snapshot, dict) else {}
    customer = invoice.party_snapshot if isinstance(invoice.party_snapshot, dict) else {}
    if isinstance(invoice, CommercialDocument):
        return {"business_name": business.get("legal_name") or business.get("name") or "Business",
            "customer_name": customer.get("legal_name") or customer.get("name") or "Customer",
            "invoice_number": invoice.number, "invoice_date": invoice.document_date, "due_date": invoice.valid_until,
            "total": "INR " + indian_number(invoice.total_amount), "business_email": business.get("email", ""),
            "business_phone": business.get("phone", ""), "commercial": True, "document_label": invoice.get_kind_display()}
    paid = payment_total(invoice)
    # A legacy "paid" status alone is not evidence of recorded receipts.
    from .ledger import invoice_outstanding
    payment_status = "Paid / no amount outstanding" if invoice_outstanding(invoice)==0 and paid>0 else "Partially paid" if paid > 0 else "No payments recorded"
    return {"business_name": business.get("legal_name") or business.get("name") or invoice.supplier_name,
            "customer_name": customer.get("legal_name") or customer.get("name") or invoice.buyer_name or "Customer",
            "invoice_number": invoice.invoice_number, "invoice_date": invoice.invoice_date,
            "due_date": invoice.due_date, "total": "INR " + indian_number(invoice.total_amount),
            "payment_status": payment_status, "paid": "INR " + indian_number(paid),
            "business_email": business.get("email", ""), "business_phone": business.get("phone", "")}


def _audit(delivery, request, event, success=True, recipient_changed=False):
    from .models import AdjustmentNoteEmailDelivery
    adjustment = isinstance(delivery, AdjustmentNoteEmailDelivery)
    commercial = isinstance(delivery, CommercialEmailDelivery) or adjustment
    description = {"requested": "Invoice email requested.", "retry": "Invoice email retry requested.",
        "accepted": "Invoice email accepted by configured backend; inbox unconfirmed.",
        "failed": "Invoice email failed.", "uncertain": "Invoice email acceptance is uncertain."}[event]
    record_event(request=request, user=delivery.initiated_by, business=delivery.business,
                 action="export", module="adjustments" if adjustment else "commercial" if commercial else "invoices", obj=delivery.document if commercial else delivery.invoice,
                 description=description.replace("Invoice", "Adjustment note" if adjustment else "Commercial document") if commercial else description,
                 success=success, metadata={"event": ("adjustment_email_" if adjustment else "commercial_email_" if commercial else "invoice_email_") + event, "delivery_id": delivery.pk,
                    "recipient_changed": recipient_changed, "error_category": delivery.error_category})


@transaction.atomic
def _reserve(*, user, invoice_id, recipient, request_key, retry_id, confirm_override, confirm_resend, request, commercial=False, adjustment=False):
    from .models import InvoiceAdjustmentNote, AdjustmentNoteEmailDelivery
    Model, History = (InvoiceAdjustmentNote, AdjustmentNoteEmailDelivery) if adjustment else (CommercialDocument, CommercialEmailDelivery) if commercial else (Invoice, InvoiceEmailDelivery)
    field = "document" if commercial or adjustment else "invoice"
    business = BusinessProfile.objects.select_for_update().get(user=user)
    invoice = Model.objects.select_related("party__business" if adjustment else "customer__business" if commercial else "buyer_party__business").get(pk=invoice_id, user=user)
    if (commercial or adjustment) and invoice.business_id != business.pk:
        raise ValidationError("This document does not belong to your business.")
    if not user.is_active or not eligible_for_email(invoice):
        raise ValidationError("Only your unexpired issued commercial documents can be emailed." if commercial else "Only your finalized manual sales invoices can be emailed.")
    scope = {field: invoice, "business": business}
    existing = History.objects.filter(request_key=request_key).first()
    if existing:
        if getattr(existing, field + "_id") != invoice.pk or existing.business_id != business.pk or existing.recipient != recipient:
            raise ValidationError("This confirmation has already been used. Reload the form.")
        return existing, False
    previous = None
    if retry_id:
        previous = History.objects.filter(pk=retry_id, **scope).first()
        if previous is None or previous.status != "failed":
            raise ValidationError("Only a failed attempt for this invoice can be retried.")
        child = History.objects.filter(retry_of=previous).first()
        if child:
            return child, False
        if previous.attempt_number >= MAX_ATTEMPTS:
            raise ValidationError("This retry chain has reached its three-attempt limit.")
    reference = previous.recipient if previous else default_recipient(invoice)
    changed = recipient.casefold() != reference.casefold()
    if changed and not confirm_override:
        raise ValidationError("Explicitly confirm the corrected recipient before sending.")
    history = History.objects.filter(**scope)
    if history.filter(status__in=["pending", "sending"]).exists():
        raise ValidationError("An attempt is already in progress. Do not resend until its outcome is checked.")
    if history.filter(status="uncertain").exists() and not confirm_resend:
        raise ValidationError("Check the uncertain attempt first, then explicitly confirm a deliberate new send; duplicates are possible.")
    now = timezone.now()
    if history.filter(created_at__gt=now - COOLDOWN).exists():
        raise ValidationError("Wait at least 60 seconds between attempts for this invoice.")
    recent_count = InvoiceEmailDelivery.objects.filter(business=business, created_at__gte=now - timedelta(hours=1)).count()
    from .commercial_services import commercial_available
    if commercial_available():
        recent_count += CommercialEmailDelivery.objects.filter(business=business, created_at__gte=now - timedelta(hours=1)).count()
    from .adjustment_services import adjustments_available
    if adjustments_available():
        recent_count += AdjustmentNoteEmailDelivery.objects.filter(business=business, created_at__gte=now - timedelta(hours=1)).count()
    if recent_count >= HOURLY_BUSINESS_LIMIT or history.filter(created_at__gte=now - timedelta(hours=1)).count() >= HOURLY_INVOICE_LIMIT:
        raise ValidationError("The invoice email hourly limit has been reached. Try again later.")
    context = email_context(invoice)
    label = context.get("document_label", "Invoice")
    subject = "".join(f"{label} {invoice.invoice_number} from {context['business_name']}".splitlines())[:255]
    delivery = History.objects.create(**scope, initiated_by=user,
        request_key=request_key, recipient=recipient, subject=subject, backend=settings.EMAIL_BACKEND,
        retry_of=previous, attempt_number=previous.attempt_number + 1 if previous else 1)
    _audit(delivery, request, "retry" if previous else "requested", recipient_changed=changed)
    return delivery, True


def _claim(delivery):
    return type(delivery).objects.filter(pk=delivery.pk, status="pending").update(status="sending", started_at=timezone.now()) == 1


@transaction.atomic
def _finish(delivery, status, error_category, request, digest="", filename=""):
    if type(delivery).objects.filter(pk=delivery.pk, status="sending").update(
            status=status, error_category=error_category, completed_at=timezone.now(),
            attachment_sha256=digest, attachment_name=filename) != 1:
        raise DatabaseError("Delivery result could not be recorded.")
    delivery.error_category = error_category
    _audit(delivery, request, "accepted" if status == "sent" else status, success=status == "sent")


def _send_email(*, user, invoice_id, recipient, request_key, confirm_override=False,
                confirm_resend=False, retry_id=None, request=None, commercial=False, adjustment=False):
    """Reserve and commit before SMTP. Never retry a claimed request automatically."""
    recipient = recipient_address(recipient)
    try:
        request_key = UUID(str(request_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Reload the email confirmation form.") from None
    from .commercial_services import commercial_available
    from .models import InvoiceAdjustmentNote, AdjustmentNoteEmailDelivery
    Model, History = (InvoiceAdjustmentNote, AdjustmentNoteEmailDelivery) if adjustment else (CommercialDocument, CommercialEmailDelivery) if commercial else (Invoice, InvoiceEmailDelivery)
    from .adjustment_services import adjustments_available
    if not (adjustments_available() if adjustment else commercial_available() if commercial else history_available()):
        return DeliveryResult("unavailable", error_category="history_unavailable")
    try:
        delivery, created = _reserve(user=user, invoice_id=invoice_id, recipient=recipient,
            request_key=request_key, retry_id=retry_id, confirm_override=confirm_override,
            confirm_resend=confirm_resend, request=request, commercial=commercial, adjustment=adjustment)
    except (BusinessProfile.DoesNotExist, Model.DoesNotExist):
        raise ValidationError("This invoice is unavailable.") from None
    except IntegrityError:
        existing = History.objects.filter(**{("document_id" if commercial or adjustment else "invoice_id"): invoice_id}, business__user=user).filter(
            Q(request_key=request_key) | Q(retry_of_id=retry_id, retry_of__isnull=False)).first()
        if existing is None:
            return DeliveryResult("unavailable", error_category="history_unavailable")
        return DeliveryResult(existing.status, existing.pk, existing.error_category, reused=True)
    except DatabaseError:
        return DeliveryResult("unavailable", error_category="history_unavailable")
    if not created:
        return DeliveryResult(delivery.status, delivery.pk, delivery.error_category, reused=True)
    try:
        if not _claim(delivery):
            return DeliveryResult("sending", delivery.pk, reused=True)
    except DatabaseError:
        return DeliveryResult("unavailable", delivery.pk, "history_unavailable")
    status, reason, digest, filename = "failed", "pdf_failed", "", ""
    try:
        invoice = Model.objects.get(pk=invoice_id, user=user) if commercial or adjustment else Invoice.objects.prefetch_related("payments").get(pk=invoice_id, user=user)
        if not commercial and not adjustment:
            from django.db.models import prefetch_related_objects
            if adjustments_available(): prefetch_related_objects([invoice], "adjustment_notes")
            else: invoice._prefetched_objects_cache["adjustment_notes"] = []
        if not eligible_for_email(invoice):
            raise ValidationError("Invoice is no longer eligible.")
        if adjustment:
            from .adjustment_pdf import generate_adjustment_pdf, adjustment_pdf_filename
            content, filename = generate_adjustment_pdf(invoice), adjustment_pdf_filename(invoice)
        elif commercial:
            from .commercial_pdf import generate_commercial_pdf, commercial_pdf_filename
            content, filename = generate_commercial_pdf(invoice), commercial_pdf_filename(invoice)
        else:
            content, filename = generate_invoice_pdf(invoice), invoice_pdf_filename(invoice)
        digest = sha256(content).hexdigest()
        context = email_context(invoice)
        message = EmailMultiAlternatives(delivery.subject,
            render_to_string("invoices/email/invoice.txt", context), settings.DEFAULT_FROM_EMAIL, [recipient],
            connection=get_connection(timeout=min(settings.EMAIL_TIMEOUT, 30)))
        message.attach_alternative(render_to_string("invoices/email/invoice.html", context), "text/html")
        message.attach(filename, content, "application/pdf")
        # Cancellation can commit while a PDF is rendering. Recheck before transport.
        fresh = Model.objects.filter(pk=invoice_id, user=user).first()
        if fresh is None or not eligible_for_email(fresh):
            reason = "ineligible"
        else:
            reason = delivery_error(message)
            status = "sent" if not reason else "uncertain" if reason in UNCERTAIN_ERRORS else "failed"
    except DatabaseError:
        reason = "history_unavailable"
    except Exception:
        # No SMTP operation has started: transport exceptions are handled by delivery_error.
        reason = "pdf_failed"
    try:
        _finish(delivery, status, reason, request, digest, filename)
    except DatabaseError:
        logger.warning("Invoice email result could not be recorded (delivery_id=%s).", delivery.pk)
        return DeliveryResult("uncertain", delivery.pk, "recording_failed")
    if reason:
        logger.warning("Invoice email attempt ended with category %s (delivery_id=%s).", reason, delivery.pk)
    return DeliveryResult(status, delivery.pk, reason)


def send_invoice_email(**kwargs):
    return _send_email(**kwargs)


def send_commercial_email(*, document_id, **kwargs):
    return _send_email(invoice_id=document_id, commercial=True, **kwargs)


def send_adjustment_email(*, document_id, **kwargs):
    return _send_email(invoice_id=document_id, adjustment=True, **kwargs)
