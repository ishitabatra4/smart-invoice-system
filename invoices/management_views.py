from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from accounts.models import BusinessProfile
from .models import Invoice
from .extraction_services import latest_attempt, assert_original
from .classification import classification_assessment
from .management_services import eligibility_errors, state_token, target_duplicates, manage_provisional, activity
from .management_forms import ProvisionalManagementForm


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def provisional_action(request, invoice_id, operation):
    business = get_object_or_404(BusinessProfile, user=request.user)
    invoice = get_object_or_404(Invoice, pk=invoice_id, user=request.user)
    attempt = latest_attempt(invoice)
    errors = eligibility_errors(invoice,business,operation,attempt)
    if not errors:
        try:
            assert_original(attempt)
        except ValidationError as exc:
            errors.extend(exc.messages)
    initial = {'target_type':invoice.invoice_type, 'token':state_token(invoice,business,operation,attempt) if not errors else ''}
    form = ProvisionalManagementForm(request.POST if request.method == 'POST' else None, operation=operation, initial=initial)
    proposed = invoice.invoice_type
    if request.method == 'POST' and form.is_valid() and not errors:
        proposed = form.cleaned_data.get('target_type',invoice.invoice_type)
        if 'preview' not in request.POST:
            try:
                updated = manage_provisional(request=request,invoice_id=invoice.pk,operation=operation,**form.cleaned_data)
                messages.success(request,'Invoice restored to Needs Review. Review and verify separately.' if operation=='restore' else 'Invoice type changed. Review the updated categorization; no financial totals were recognized.')
                return redirect('invoice_detail',invoice_id=updated.pk)
            except ValidationError as exc:
                form.add_error(None,exc)
            except (IntegrityError,OperationalError):
                form.add_error(None,'The invoice or duplicate state changed concurrently. Nothing was accepted; reload and review before trying again.')
    signals = target_duplicates(request.user,business,invoice,proposed,attempt) if attempt and not errors else []
    return render(request,'invoices/provisional_action.html',{'invoice':invoice,'business':business,'attempt':attempt,'operation':operation,
        'form':form,'eligibility_errors':errors,'proposed_type':dict(Invoice.INVOICE_TYPES).get(proposed), 'duplicate_signals':signals,
        'classification':classification_assessment(business,attempt.result if attempt else {},proposed),
        'cancellations':activity(invoice).filter(action='cancel')[:10]},status=403 if errors else 200)
