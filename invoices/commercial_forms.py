import uuid
from django import forms
from django.core import signing
from django.utils import timezone

from .manual_forms import ManualInvoiceForm

CREATE_SALT = "commercial.create"


class CommercialDocumentForm(ManualInvoiceForm):
    def __init__(self, *args, business, kind, document=None, **kwargs):
        self.kind = kind
        super().__init__(*args, business=business, invoice_type="sales", invoice=document, **kwargs)
        self.fields.pop("add_new_party")
        self.fields["party"].required = True
        self.fields["invoice_number"].label = "Document number"
        self.fields["invoice_date"].label = "Document date"
        self.fields["due_date"].label = "Valid until"
        self.fields["due_date"].required = True
        self.fields["payment_terms"].label = "Commercial terms (not a payment demand)"

    def clean_creation_token(self):
        token = self.cleaned_data.get("creation_token")
        if self.invoice:
            return token
        try:
            value = signing.loads(token, salt=CREATE_SALT, max_age=86400)
            if (value["user"], value["kind"]) != (self.business.user_id, self.kind):
                raise ValueError
            self.creation_key = uuid.UUID(value["key"])
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            raise forms.ValidationError("This form expired. Reload the page.") from None
        return token


class CommercialDecisionForm(forms.Form):
    notes = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}))


class ConversionForm(forms.Form):
    invoice_date = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))

    def clean_invoice_date(self):
        value = self.cleaned_data["invoice_date"]
        if not 1900 <= value.year <= 9998:
            raise forms.ValidationError("Choose a date between 1900 and 9998.")
        return value


from .email_forms import InvoiceEmailForm
EMAIL_SALT = "commercial.email"


class CommercialEmailForm(InvoiceEmailForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["confirm"].label = "I have verified the recipient and want to send this non-accounting document PDF."

    def clean_send_token(self):
        try:
            value = signing.loads(self.cleaned_data["send_token"], salt=EMAIL_SALT, max_age=3600)
            if (value["user"], value["document"], value["retry"]) != (self.user.pk, self.invoice.pk, self.retry.pk if self.retry else None):
                raise ValueError
            return value["key"]
        except (signing.BadSignature, KeyError, ValueError, TypeError):
            raise forms.ValidationError("This confirmation expired or is invalid. Reload the form.") from None
