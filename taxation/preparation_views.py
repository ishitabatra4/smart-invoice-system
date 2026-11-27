import uuid
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from accounts.models import BusinessProfile
from audit.services import record_event
from .models import TaxPeriod, TaxWorkingPaper
from .preparation_forms import PeriodForm, PreparationForm
from . import preparation as service
from .preparation_exports import export_paper
from .readiness import review_panel
SALT = 'tax-preparation-reviewed-preview-v1'


def owned_period(user, pk):
    return get_object_or_404(TaxPeriod.objects.select_related('business__user'), pk=pk, business__user=user)


def owned_paper(user, pk):
    return get_object_or_404(TaxWorkingPaper.objects.select_related('period__business__user'), pk=pk, period__business__user=user)


@login_required
@never_cache
def dashboard(request):
    business = get_object_or_404(BusinessProfile, user=request.user)
    available = service.ready()
    form = PeriodForm(request.POST or None)
    if request.method == 'POST' and available and form.is_valid():
        period = service.create_period(request, **form.cleaned_data)
        return redirect('tax_period', pk=period.pk)
    periods = Paginator(TaxPeriod.objects.filter(business=business), 24).get_page(request.GET.get('page')) if available else []
    return render(request, 'taxation/preparation_dashboard.html', {'available': available, 'form': form, 'periods': periods, 'panel': review_panel(request.user, request.GET) if available else None})


@login_required
@never_cache
def period_detail(request, pk):
    if not service.ready():
        return redirect('tax_preparation')
    period = owned_period(request.user, pk)
    papers = Paginator(period.papers.all(), 24).get_page(request.GET.get('page'))
    return render(request, 'taxation/preparation_period.html', {'period': period, 'papers': papers, 'kinds': TaxWorkingPaper.KINDS, 'panel': review_panel(request.user, request.GET, period)})


@login_required
@never_cache
def preview(request, pk, kind):
    if not service.ready():
        return redirect('tax_preparation')
    if kind not in dict(TaxWorkingPaper.KINDS):
        raise Http404
    period = owned_period(request.user, pk)
    previous = None
    if request.GET.get('revise'):
        try:
            previous_id = int(request.GET['revise'])
        except (ValueError, TypeError):
            raise Http404 from None
        previous = get_object_or_404(TaxWorkingPaper, pk=previous_id, period=period, kind=kind)
        if period.papers.filter(kind=kind).order_by('-revision').first() != previous:
            return HttpResponseBadRequest('Only the latest working paper can be explicitly revised.')
    form = PreparationForm(request.POST or None, period=period, kind=kind, previous_inputs=previous.inputs if previous else None)
    data = token = None
    if request.method == 'POST' and form.is_valid():
        try:
            inputs = form.inputs()
            if request.POST.get('action') == 'prepare':
                payload = signing.loads(request.POST.get('preview_token', ''), salt=SALT, max_age=1800)
                if payload['user'] != request.user.pk or payload['period'] != period.pk or payload['kind'] != kind or payload['previous'] != (previous.pk if previous else None):
                    raise ValidationError('Preview does not belong to this user, period, type and revision.')
                paper = service.prepare(request, period, kind, inputs, uuid.UUID(payload['key']), payload['digest'], payload['previous'], form.cleaned_data['confirmed'])
                messages.success(request, 'Saved immutable working paper; no filing performed.')
                return redirect('tax_paper', pk=paper.pk)
            data, _, _ = service.preview(period, kind, inputs)
            token = signing.dumps({'user': request.user.pk, 'period': period.pk, 'kind': kind, 'previous': previous.pk if previous else None, 'key': str(uuid.uuid4()), 'digest': service.digest(data)}, salt=SALT, compress=True)
            record_event(request=request, action='update', module='taxation', description='Tax preparation preview validated and comparison performed; no snapshot or claim created.', obj=period, metadata={'return_type': kind, 'import_runs': inputs['import_runs']})
        except (ValidationError, signing.BadSignature, KeyError, ValueError, IntegrityError) as exc:
            form.add_error(None, exc if isinstance(exc, ValidationError) else 'Preview is invalid, expired or superseded. Review a fresh preview.')
    return render(request, 'taxation/preparation_preview.html', {'period': period, 'kind': kind, 'form': form, 'data': data, 'preview_token': token, 'previous': previous})


@login_required
@never_cache
def paper_detail(request, pk):
    if not service.ready():
        return redirect('tax_preparation')
    paper = owned_paper(request.user, pk)
    try:
        is_stale = service.stale(paper)
    except (ValidationError, OSError):
        is_stale = True
    return render(request, 'taxation/preparation_paper.html', {'paper': paper, 'data': paper.data, 'is_stale': is_stale})


@login_required
@require_POST
def lifecycle(request, pk):
    if not service.ready():
        return redirect('tax_preparation')
    paper = owned_paper(request.user, pk)
    try:
        service.transition(request, paper, request.POST.get('status'), request.POST.get('reason', ''))
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    return redirect('tax_paper', pk=pk)


@login_required
@never_cache
def export(request, pk, fmt):
    if not service.ready():
        return redirect('tax_preparation')
    paper = owned_paper(request.user, pk)
    try:
        response = export_paper(paper, fmt)
    except ValidationError as exc:
        return render(request, 'taxation/preparation_export_error.html', {'paper': paper, 'error': '; '.join(exc.messages)}, status=400)
    record_event(request=request, action='export', module='taxation', description='Saved tax working paper exported; not a filed return.', obj=paper, metadata={'format': fmt, 'revision': paper.revision})
    return response
