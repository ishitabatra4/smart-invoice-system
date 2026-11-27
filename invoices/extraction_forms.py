import uuid
from django import forms
from django.core import signing
from accounts.models import BusinessParty
from .manual_forms import ManualLineForm

REVIEW_SALT='invoices.extraction.review.v9'


class ExtractionReviewForm(forms.Form):
    token=forms.CharField(widget=forms.HiddenInput)
    invoice_number=forms.CharField(max_length=100)
    invoice_date=forms.DateField(input_formats=['%Y-%m-%d'],widget=forms.DateInput(attrs={'type':'date'}))
    due_date=forms.DateField(required=False,input_formats=['%Y-%m-%d'],widget=forms.DateInput(attrs={'type':'date'}))
    clear_due_date_confirmed=forms.BooleanField(required=False,label='I intentionally leave the known extracted due date blank after reviewing the original.')
    supplier_name=forms.CharField(max_length=255)
    supplier_gstin=forms.CharField(max_length=15,required=False)
    supplier_address=forms.CharField(max_length=1000,required=False)
    buyer_name=forms.CharField(max_length=255,required=False)
    buyer_gstin=forms.CharField(max_length=15,required=False)
    buyer_address=forms.CharField(max_length=1000,required=False)
    party=forms.ModelChoiceField(queryset=BusinessParty.objects.none(),required=False)
    supplier_state_code=forms.CharField(max_length=2)
    place_of_supply=forms.CharField(max_length=2)
    pricing_mode=forms.ChoiceField(choices=[('','Review pricing basis'),('exclusive','GST exclusive'),('inclusive','GST inclusive')])
    supply_category=forms.ChoiceField(choices=[('','Review supply category'),('standard','Standard; other supply categories require manual review')])
    invoice_discount_type=forms.ChoiceField(choices=[('fixed','Fixed'),('percent','Percentage')])
    invoice_discount_value=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0)
    taxable_amount=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0)
    cgst_amount=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0)
    sgst_amount=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0)
    igst_amount=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0)
    other_charges=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0,label='Unclassified header charges (move them into reviewed lines)')
    rounding_adjustment=forms.DecimalField(max_digits=14,decimal_places=2,min_value=-1,max_value=1)
    total_amount=forms.DecimalField(max_digits=14,decimal_places=2,min_value=0)
    acknowledge_duplicates=forms.BooleanField(required=False,label='I reviewed the owned duplicate signals; this is a distinct invoice.')
    confirm=forms.BooleanField(label='I compared all fields/lines and any empty GSTINs with the original and confirm this reviewed draft. Saving does not verify or finalize it.')
    def __init__(self,*args,user,attempt,**kwargs):
        self.user,self.attempt=user,attempt
        super().__init__(*args,**kwargs)
        kind=attempt.invoice.invoice_type if attempt.invoice_id else attempt.result.get('upload_invoice_type','purchase')
        self.fields['party'].queryset=BusinessParty.objects.filter(business=attempt.business,is_active=True,party_type__in=['customer','both'] if kind=='sales' else ['vendor','both'])
        for field in self.fields.values():field.widget.attrs['class']='form-check-input' if isinstance(field,forms.BooleanField) else 'form-control'
        if self.is_bound and 'preview' in self.data:self.fields['confirm'].required=False
        self.fields['supplier_gstin'].help_text='Local structure/state/checksum only; no official registration lookup.'
        if attempt.result.get('due_date'):
            self.fields['due_date'].help_text=f"Extracted candidate: {attempt.result['due_date']} (unconfirmed). Clearing it requires the explicit acknowledgement below."
        else:self.fields.pop('clear_due_date_confirmed')
    def clean_token(self):
        try:
            data=signing.loads(self.cleaned_data['token'],salt=REVIEW_SALT,max_age=3600)
            if (data['user'],data['attempt'])!=(self.user.pk,self.attempt.pk):raise ValueError
            from .management_services import classification_revision
            if data.get('classification_revision',0)!=(classification_revision(self.attempt.invoice) if self.attempt.invoice_id else 0):raise ValueError
            self.review_key=uuid.UUID(data['key']);self.previous_review=data['previous'];return self.cleaned_data['token']
        except (signing.BadSignature,KeyError,ValueError,TypeError):raise forms.ValidationError('This review expired or belongs to another record. Reload it.') from None
    def clean(self):
        data=super().clean()
        for key in ('supplier_gstin','buyer_gstin'):data[key]=(data.get(key) or '').strip().upper()
        if data.get('due_date') and data.get('invoice_date') and data['due_date']<data['invoice_date']:self.add_error('due_date','Due date cannot precede invoice date.')
        if 'due_date' not in self.errors:
            from .extraction_services import validate_review_due_date
            try:validate_review_due_date(self.attempt,data)
            except forms.ValidationError as exc:self.add_error('due_date',exc)
        return data


class ExtractionLineForm(ManualLineForm):
    def __init__(self,*args,**kwargs):
        if args and args[0] is not None:
            data=args[0].copy();prefix=kwargs.get('prefix','')
            data.setdefault((prefix+'-' if prefix else '')+'tax_treatment','')
            args=(data,*args[1:])
        super().__init__(*args,**kwargs)
        self.fields['tax_treatment'].choices=[('','Review tax treatment'),*self.fields['tax_treatment'].choices]
        for name in ('quantity','rate','discount','tax_rate','tax_treatment'):
            self.fields[name].initial=None;self.fields[name].required=True
        self.fields['discount_type'].initial='fixed'

ExtractionLineSet=forms.formset_factory(ExtractionLineForm,extra=1,can_delete=True,min_num=1,validate_min=True,max_num=100,validate_max=True,absolute_max=150)
