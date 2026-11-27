import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db import IntegrityError, DatabaseError
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.cache import never_cache

from accounts.models import BusinessProfile
from audit.models import AuditEvent
from audit.services import record_event
from .models import CommercialDocument, CommercialEmailDelivery
from .commercial_forms import CommercialDocumentForm, CommercialDecisionForm, ConversionForm, CommercialEmailForm, CREATE_SALT, EMAIL_SALT
from .commercial_services import commercial_available, save_commercial, transition_commercial, convert_commercial, amounts, stored_inputs
from .commercial_pdf import generate_commercial_pdf, commercial_pdf_filename
from .manual_forms import ManualLineFormSet
from .email_delivery import eligible_for_email, default_recipient, send_commercial_email


def _business(request):
    return get_object_or_404(BusinessProfile, user=request.user)


def _document(request, document_id):
    return get_object_or_404(CommercialDocument.objects.select_related("business", "customer"), pk=document_id, user=request.user, business__user=request.user)


def _unavailable():
    return HttpResponse("Quotations and proformas require approval and application of their pending migration.", status=503, content_type="text/plain")


@login_required
@require_GET
def commercial_list(request):
    business = _business(request)
    ready = commercial_available()
    documents = CommercialDocument.objects.none()
    if ready:
        documents = CommercialDocument.objects.filter(business=business, user=request.user)
        kind, status = request.GET.get("kind"), request.GET.get("status")
        if kind in dict(CommercialDocument.KINDS):
            documents = documents.filter(kind=kind)
        if status == "expired":
            documents = documents.filter(status__in=["draft", "sent", "accepted"], valid_until__lt=timezone.localdate())
        elif status in dict(CommercialDocument.STATUSES):
            documents = documents.filter(status=status)
            if status in {"draft", "sent", "accepted"}:
                documents = documents.filter(valid_until__gte=timezone.localdate())
        if request.GET.get("q"):
            query = request.GET["q"][:200]
            documents = documents.filter(Q(number__icontains=query) | Q(party_snapshot__name__icontains=query))
        order = request.GET.get("sort", "-document_date")
        if order not in {"number", "document_date", "-document_date", "total_amount", "-total_amount"}:
            order = "-document_date"
        documents = documents.order_by(order, "-pk")
    page = Paginator(documents, 25).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "invoices/commercial_list.html", {"page": page, "ready": ready, "kinds": CommercialDocument.KINDS,
        "statuses": CommercialDocument.STATUSES + [("expired", "Expired")], "filter_query": query.urlencode()})


def _forms(request, business, kind, document=None):
    data = request.POST if request.method == "POST" else None
    form = CommercialDocumentForm(data, business=business, kind=kind, document=document)
    if not data and not document:
        form.initial.update(creation_token=signing.dumps({"key": str(uuid.uuid4()), "kind": kind, "user": request.user.pk}, salt=CREATE_SALT), due_date=timezone.localdate() + timedelta(days=30))
    initial = stored_inputs(document) if document else None
    if initial:
        for row in initial:
            row["tax_rate"] = format(row["tax_rate"].normalize(), "f")
    rows = ManualLineFormSet(data, initial=initial, prefix="items", form_kwargs={"pricing_mode": request.POST.get("pricing_mode", "exclusive") if data else document.pricing_mode if document else "exclusive"})
    return form, rows


@login_required
@require_http_methods(["GET", "POST"])
def commercial_form(request, kind=None, document_id=None):
    business = _business(request)
    if not commercial_available():
        return _unavailable()
    document = _document(request, document_id) if document_id else None
    kind = document.kind if document else kind
    if kind not in dict(CommercialDocument.KINDS):
        raise Http404
    if document and document.status != "draft":
        return HttpResponse("Issued documents cannot be edited. Create a new draft for revised terms.", status=400)
    form, rows = _forms(request, business, kind, document)
    if request.method == "POST":
        valid, valid_rows = form.is_valid(), rows.is_valid()
        if valid and valid_rows:
            try:
                document = save_commercial(request=request, kind=kind, data=form.cleaned_data,
                    line_data=[row for row in rows.cleaned_data if row and not row.get("DELETE")],
                    creation_key=getattr(form, "creation_key", None), document_id=document.pk if document else None)
            except ValidationError as error:
                form.add_error(None, error)
            except IntegrityError:
                form.add_error(None, "These details conflict with an existing record. Reload and review.")
            except ObjectDoesNotExist:
                raise Http404 from None
            else:
                return redirect("commercial_detail", document_id=document.pk)
    return render(request, "invoices/commercial_form.html", {"form": form, "formset": rows, "document": document, "kind": kind, "label": dict(CommercialDocument.KINDS)[kind]})


@login_required
@require_POST
def commercial_calculate(request, kind):
    business = _business(request)
    if not commercial_available():
        return _unavailable()
    if kind not in dict(CommercialDocument.KINDS):
        raise Http404
    document = None
    if request.POST.get("document_id"):
        value = request.POST["document_id"]
        if not value.isdecimal() or len(value) > 18:
            raise Http404
        document = _document(request, int(value))
        if document.kind != kind or document.status != "draft":
            raise Http404
    form, rows = _forms(request, business, kind, document)
    valid, valid_rows = form.is_valid(), rows.is_valid()
    if not valid or not valid_rows:
        return JsonResponse({"errors": {"document": form.errors, "lines": rows.errors, "formset": rows.non_form_errors()}}, status=400)
    try:
        result = amounts(form.cleaned_data, [row for row in rows.cleaned_data if row and not row.get("DELETE")])
    except ValidationError as error:
        return JsonResponse({"errors": error.messages}, status=400)
    return JsonResponse(result)


@login_required
@require_GET
def commercial_detail(request, document_id):
    if not commercial_available():
        return _unavailable()
    document = _document(request, document_id)
    return render(request, "invoices/commercial_detail.html", {"document": document, "email_eligible": eligible_for_email(document),
        "decision_form": CommercialDecisionForm(), "conversion_form": ConversionForm(),
        "deliveries": document.email_deliveries.select_related("initiated_by")[:20],
        "activity": AuditEvent.objects.filter(business=document.business, object_type=document._meta.label_lower, object_id=str(document.pk))[:50]})


@login_required
@require_POST
def commercial_transition(request, document_id, action):
    if not commercial_available():
        return _unavailable()
    document = _document(request, document_id)
    form = CommercialDecisionForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Decision notes are invalid.", status=400)
    try:
        transition_commercial(request=request, document_id=document.pk, action=action, notes=form.cleaned_data["notes"])
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    return redirect("commercial_detail", document_id=document.pk)


@login_required
@require_POST
def commercial_convert(request, document_id):
    if not commercial_available():
        return _unavailable()
    document = _document(request, document_id)
    form = ConversionForm(request.POST)
    if form.is_valid():
        try:
            invoice = convert_commercial(request=request, document_id=document.pk, invoice_date=form.cleaned_data["invoice_date"])
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        except IntegrityError:
            messages.error(request, "Conversion conflicted with another request. Reload and review the linked invoice.")
        else:
            return redirect("invoice_detail", invoice_id=invoice.pk)
    else:
        messages.error(request, "Choose a valid date for the new invoice draft.")
    return redirect("commercial_detail", document_id=document.pk)


@login_required
@never_cache
@require_GET
def commercial_pdf(request, document_id):
    if not commercial_available():
        return _unavailable()
    document = _document(request, document_id)
    try:
        content = generate_commercial_pdf(document)
    except Exception:
        return HttpResponse("This document PDF could not be generated.", status=422, content_type="text/plain")
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(True, commercial_pdf_filename(document))
    response["X-Content-Type-Options"] = "nosniff"
    record_event(request=request, action="export", module="commercial", obj=document, description="Commercial document PDF downloaded.")
    return response


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def commercial_email(request, document_id, delivery_id=None):
    if not commercial_available():
        return _unavailable()
    document = _document(request, document_id)
    if not eligible_for_email(document):
        return HttpResponse("Only unexpired, explicitly issued or accepted documents can be emailed.", status=400)
    retry = get_object_or_404(CommercialEmailDelivery, pk=delivery_id, document=document, business=document.business) if delivery_id else None
    if retry and retry.status != "failed":
        return HttpResponse("Only failed attempts can use Retry.", status=400)
    token = signing.dumps({"key": str(uuid.uuid4()), "user": request.user.pk, "document": document.pk, "retry": retry.pk if retry else None}, salt=EMAIL_SALT)
    form = CommercialEmailForm(request.POST if request.method == "POST" else None, user=request.user, invoice=document, retry=retry,
        initial={"send_token": token, "recipient": retry.recipient if retry else default_recipient(document)})
    if request.method == "POST" and form.is_valid():
        try:
            result = send_commercial_email(user=request.user, document_id=document.pk, recipient=form.cleaned_data["recipient"], request_key=form.cleaned_data["send_token"],
                retry_id=retry.pk if retry else None, confirm_override=form.cleaned_data["confirm_override"], confirm_resend=form.cleaned_data["confirm_resend"], request=request)
        except ValidationError as error:
            form.add_error(None, error)
        except DatabaseError:
            form.add_error(None, "History is unavailable. Check the previous outcome before trying again.")
        else:
            messages.info(request, "No new attempt was sent; this confirmation was already recorded." if result.reused else
                "The configured backend accepted the message; inbox and customer acceptance are unconfirmed." if result.status == "sent" else
                "Email outcome: " + result.status + ". Review history before any controlled retry.")
            return redirect("commercial_detail", document_id=document.pk)
    return render(request, "invoices/email_confirm.html", {"invoice": document, "commercial": True, "form": form, "retry": retry,
        "business_name": document.business_snapshot.get("name", ""), "default_recipient": form.initial["recipient"]})
