from django import forms
from .models import Invoice


class ProvisionalManagementForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    target_type = forms.ChoiceField(choices=[('sales','Sales Invoice'),('purchase','Purchase Invoice'),('expense','Expense Invoice')], label='Proposed invoice type')
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={'rows':3}), label='Reason for correction or restoration')
    acknowledge_duplicates = forms.BooleanField(required=False, label='I reviewed the potential duplicate warnings and this is a distinct invoice. This cannot override exact/strong conflicts.')
    confirm = forms.BooleanField(label='I explicitly approve this provisional action. It does not confirm, verify or recognize financial values.')

    def __init__(self,*args,operation,**kwargs):
        super().__init__(*args,**kwargs)
        if operation == 'restore':
            self.fields.pop('target_type')
        if self.is_bound and 'preview' in self.data:
            self.fields['confirm'].required = False
            self.fields['reason'].required = False
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-check-input' if isinstance(field,forms.BooleanField) else 'form-control'
