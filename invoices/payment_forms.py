from django import forms

from .models import InvoicePayment


class InvoicePaymentForm(forms.ModelForm):
    class Meta:
        model = InvoicePayment
        fields = ["invoice", "payment_date", "amount", "payment_mode", "reference_id", "notes"]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, invoices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invoice"].queryset = invoices
        self.fields["invoice"].empty_label = "Select an outstanding invoice"
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["invoice"].widget.attrs["class"] = "form-select"
        self.fields["payment_mode"].widget.attrs["class"] = "form-select"

    def clean_reference_id(self):
        reference = self.cleaned_data["reference_id"].strip()
        if not reference:
            raise forms.ValidationError("Enter a transaction or reference ID to prevent duplicate payments.")
        return reference
