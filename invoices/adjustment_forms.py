import uuid
from django import forms
from django.core import signing
from django.utils import timezone
from .models import Invoice, InvoiceLineItem, InvoiceAdjustmentNote
from .email_forms import InvoiceEmailForm

CREATE_SALT='adjustments.create'
EMAIL_SALT='adjustments.email'


class AdjustmentForm(forms.ModelForm):
    creation_token=forms.CharField(widget=forms.HiddenInput)
    original_invoice=forms.ModelChoiceField(queryset=Invoice.objects.none())
    class Meta:
        model=InvoiceAdjustmentNote
        fields=['original_invoice','kind','document_date','reason','tax_effect','pricing_mode','supplier_reference','invoice_discount_type','invoice_discount_value','eligibility_reviewed','annual_return_date','notes']
        widgets={'document_date':forms.DateInput(attrs={'type':'date'}),'annual_return_date':forms.DateInput(attrs={'type':'date'}),'notes':forms.Textarea(attrs={'rows':2})}
    def __init__(self,*args,business,note=None,**kwargs):
        self.business,self.note=business,note
        super().__init__(*args,instance=note,**kwargs)
        self.fields['original_invoice'].queryset=Invoice.objects.filter(user=business.user,entry_mode='manual',document_state='finalized',status__in=['verified','paid'],invoice_type__in=['sales','purchase'],calculation_version=1)
        self.fields['document_date'].initial=timezone.localdate
        self.fields['eligibility_reviewed'].label='I reviewed GST eligibility, annual-return timing and recipient ITC implications. This is not a filing guarantee.'
        self.fields['annual_return_date'].help_text='Date the original supply FY annual return was furnished, if applicable.'
        self.fields['supplier_reference'].help_text='Required only for received supplier notes; preserve the supplier-issued reference.'
        self.fields['invoice_discount_type'].widget=forms.Select(choices=[('fixed','Fixed'),('percent','Percent')])
        self.fields['pricing_mode'].initial='exclusive'
        for name,field in self.fields.items():
            if name!='creation_token':field.widget.attrs['class']='form-check-input' if isinstance(field.widget,forms.CheckboxInput) else 'form-control'
    def clean_creation_token(self):
        try:
            payload=signing.loads(self.cleaned_data['creation_token'],salt=CREATE_SALT,max_age=86400)
            if payload['user']!=self.business.user_id or payload['note']!=(self.note.pk if self.note else None):raise ValueError
            self.creation_key=uuid.UUID(payload['key']);return self.cleaned_data['creation_token']
        except (signing.BadSignature,ValueError,TypeError,KeyError):raise forms.ValidationError('Reload this expired/invalid form.') from None
    def _post_clean(self):
        # The service populates owned references, snapshots, numbers and computed amounts.
        # Model validation runs there after all server-calculated fields are present.
        pass


class AdjustmentLineForm(forms.Form):
    original_line=forms.ModelChoiceField(queryset=InvoiceLineItem.objects.none())
    mode=forms.ChoiceField(choices=[('quantity','Quantity adjustment'),('value','Value only (no quantity change)')],initial='value')
    quantity=forms.DecimalField(max_digits=12,decimal_places=3,min_value=0,required=False,initial=0)
    amount=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0,required=False,initial=0,label='Value / debit unit rate')
    discount=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0,required=False,initial=0)
    discount_type=forms.ChoiceField(choices=[('fixed','Fixed'),('percent','Percent')],initial='fixed')
    def __init__(self,*args,invoice=None,**kwargs):
        super().__init__(*args,**kwargs)
        if invoice:self.fields['original_line'].queryset=invoice.line_items.all()
        for field in self.fields.values():field.widget.attrs['class']='form-control'

AdjustmentLineSet=forms.formset_factory(AdjustmentLineForm,extra=1,can_delete=True,max_num=100,validate_max=True,min_num=1,validate_min=True,absolute_max=150)


class AdjustmentEmailForm(InvoiceEmailForm):
    def clean_send_token(self):
        try:
            payload=signing.loads(self.cleaned_data['send_token'],salt=EMAIL_SALT,max_age=3600)
            if (payload['user'],payload['note'],payload['retry'])!=(self.user.pk,self.invoice.pk,self.retry.pk if self.retry else None):raise ValueError
            return payload['key']
        except (signing.BadSignature,ValueError,TypeError,KeyError):raise forms.ValidationError('Reload this expired/invalid email confirmation.') from None
