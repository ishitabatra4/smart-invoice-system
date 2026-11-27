from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from .models import TaxPeriod, TaxWorkingPaper, ReconciliationRun
from .preparation import validate_period
from invoices.models import Invoice


class PeriodForm(forms.Form):
    kind = forms.ChoiceField(choices=TaxPeriod.KINDS)
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if all(k in data for k in ('kind', 'start_date', 'end_date')):
            validate_period(data['kind'], data['start_date'], data['end_date'])
        return data


class PreparationForm(forms.Form):
    ordinary_domestic = forms.BooleanField(required=False, label='I independently reviewed that this GST scope contains only standard domestic supplies (special supplies remain unsupported)')
    aato_band = forms.ChoiceField(choices=[('unknown', 'Prior-year turnover not established'), ('up_to_5cr', 'Prior-year aggregate turnover up to ₹5 crore'), ('above_5cr', 'Prior-year aggregate turnover above ₹5 crore')])
    import_runs = forms.ModelMultipleChoiceField(queryset=ReconciliationRun.objects.none(), required=False, label='Optional user-uploaded comparison extracts (not authenticated portal data)')
    taxpayer = forms.ChoiceField(choices=[('', 'Select taxpayer'), ('resident_individual', 'Resident individual'), ('other', 'Other — manual review')], required=False)
    regime = forms.ChoiceField(choices=[('', 'Select regime'), ('new', 'New regime'), ('old', 'Old regime — manual review')], required=False)
    assessment_year = forms.ChoiceField(choices=[('', 'Select assessment/tax year'), ('2026-27', 'AY 2026-27 (FY 2025-26)'), ('not_applicable', 'Tax year 2026-27 (2025 Act; no assessment year)')], required=False)
    taxable_income = forms.DecimalField(max_digits=16, decimal_places=2, required=False, label='Independently assessed taxable income (never inferred from accounting profit)')
    deductions = forms.DecimalField(max_digits=16, decimal_places=2, required=False, initial=0, label='Additional deductions (only zero supported)')
    resident_confirmed = forms.BooleanField(required=False, label='I independently confirm Indian tax residency')
    ordinary_only = forms.BooleanField(required=False, label='Only ordinary income; no special rates, losses, or unassessed deduction adjustments')
    confirmed = forms.BooleanField(required=False, label='I reviewed all included/excluded records, warnings, source evidence and limitations before preparing this working paper')

    def __init__(self, *args, period, kind, previous_inputs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        if kind in ('income_summary', 'income_tax'):
            for key in ('ordinary_domestic', 'aato_band', 'import_runs'):
                self.fields.pop(key)
        else:
            self.fields['import_runs'].queryset = ReconciliationRun.objects.filter(user=period.business.user)
        sources = Invoice.objects.filter(user=period.business.user, invoice_date__range=(period.start_date, period.end_date))
        self.capacity_error = 'Narrow the period: preparation supports at most 2,000 source invoices.' if sources.count() > 2000 else ''
        sales = sources.filter(invoice_type='sales', status__in=('verified', 'paid')).filter(~Q(entry_mode='manual') | Q(document_state='finalized'))
        if kind not in ('gstr1', 'gstr3b', 'itc', 'liability') or self.capacity_error:
            sales = sales.none()
        for invoice in sales.order_by('pk')[:2000]:
            name = f'registration_{invoice.pk}'
            self.fields[name] = forms.ChoiceField(choices=[('unknown', 'Registration not established'), ('registered', 'Registered — evidence reviewed'), ('unregistered', 'Unregistered — evidence reviewed')], label=f'{invoice.invoice_number or invoice.pk}: recipient registration', initial='unknown')
            self.fields[f'reason_{invoice.pk}'] = forms.CharField(max_length=300, required=False, label=f'{invoice.invoice_number or invoice.pk}: registration evidence/reason')
        if kind != 'income_tax':
            for key in ('taxpayer', 'regime', 'assessment_year', 'taxable_income', 'deductions', 'resident_confirmed', 'ordinary_only'):
                self.fields.pop(key)
        if previous_inputs:
            self.initial.update({k: v for k, v in previous_inputs.items() if k in self.fields})
            for pk, evidence in previous_inputs.get('registration', {}).items():
                if f'registration_{pk}' in self.fields:
                    self.initial[f'registration_{pk}'] = evidence['status']
                    self.initial[f'reason_{pk}'] = evidence['reason']

    def clean(self):
        data = super().clean()
        if self.capacity_error:
            raise ValidationError(self.capacity_error)
        if hasattr(self.data, 'getlist'):
            if any(len(self.data.getlist(key)) > 1 for key in self.data if key != 'import_runs'):
                raise ValidationError('Repeated controls are invalid; submit one reviewed value per field.')
            if len(self.data.getlist('import_runs')) > 10000:
                raise ValidationError('Select at most 10,000 import runs.')
        if any(key.startswith(('registration_', 'reason_')) and key not in self.fields for key in self.data):
            raise ValidationError('Registration controls must reference eligible owned sales in this period.')
        return data

    def inputs(self):
        data = {k: v for k, v in self.cleaned_data.items() if not k.startswith(('registration_', 'reason_')) and k not in ('confirmed', 'import_runs')}
        runs = self.cleaned_data.get('import_runs')
        data['import_runs'] = sorted(runs.values_list('pk', flat=True)) if runs is not None else []
        data['registration'] = {k.removeprefix('registration_'): {'status': v, 'reason': self.cleaned_data.get('reason_' + k.removeprefix('registration_'), '')} for k, v in self.cleaned_data.items() if k.startswith('registration_')}
        return data
