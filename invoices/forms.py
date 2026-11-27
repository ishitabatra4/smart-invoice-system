from django import forms
from django.forms import inlineformset_factory

from .models import Invoice, InvoiceLineItem
from accounts.models import BusinessParty


class InvoiceEditForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = [
            "supplier_party", "buyer_party", "invoice_number", "invoice_date", "due_date", "supplier_name",
            "supplier_gstin", "buyer_name", "buyer_gstin", "taxable_amount",
            "cgst_amount", "sgst_amount", "igst_amount", "other_charges",
            "total_amount",
        ]
        widgets = {
            "invoice_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, kinds in (("supplier_party", ("vendor", "both")), ("buyer_party", ("customer", "both"))):
            self.fields[name].queryset = BusinessParty.objects.filter(business__user_id=self.instance.user_id, is_active=True, party_type__in=kinds)
        if self.instance.invoice_type == "sales":
            self.fields.pop("supplier_party")
        else:
            self.fields.pop("buyer_party")
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        data = super().clean()
        for prefix in ("supplier", "buyer"):
            party = data.get(prefix + "_party")
            if party:
                data[prefix + "_name"] = party.name
                data[prefix + "_gstin"] = party.gstin
        return data


class InvoiceLineItemForm(forms.ModelForm):
    class Meta:
        model = InvoiceLineItem
        fields = [
            "description", "hsn_sac_code", "quantity", "unit", "rate",
            "tax_rate", "taxable_amount", "tax_amount", "total_amount",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = (
                "form-check-input" if name == "DELETE" else "form-control"
            )


InvoiceLineItemFormSet = inlineformset_factory(
    Invoice, InvoiceLineItem, form=InvoiceLineItemForm,
    extra=1, can_delete=True,
)


class InvoiceListFilterForm(forms.Form):
    q = forms.CharField(max_length=100, required=False, label='Search', widget=forms.TextInput(attrs={'placeholder': 'Number, customer or supplier'}))
    type = forms.ChoiceField(required=False, choices=[('', 'All document types'), ('sales', 'Sales'), ('purchase', 'Purchase'), ('expense', 'Expense')], label='Type')
    status = forms.ChoiceField(required=False, choices=[('', 'All statuses'), *Invoice.STATUS_CHOICES], label='Status')
    sort = forms.ChoiceField(required=False, choices=[('', 'Newest first'), ('oldest', 'Oldest first'), ('number', 'Invoice number'), ('amount', 'Highest value')], label='Sort')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'
