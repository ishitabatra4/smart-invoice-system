from decimal import Decimal
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.forms import formset_factory
from django.utils import timezone
from accounts.models import BusinessParty
from .manual_calculations import line_basis
from django.conf import settings


def creation_key_from_token(token, user_id, invoice_type):
    try:
        value = signing.loads(token, salt="invoices.create", max_age=86400)
        if value["user"] != user_id or value["type"] != invoice_type:
            raise signing.BadSignature
        import uuid
        return uuid.UUID(value["key"])
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        raise forms.ValidationError("This form expired. Reload the page and try again.")


class ManualInvoiceForm(forms.Form):
    creation_token = forms.CharField(widget=forms.HiddenInput, required=False)
    invoice_number = forms.CharField(max_length=100, required=False)
    party = forms.ModelChoiceField(queryset=BusinessParty.objects.none(), required=False, label="Customer / supplier")
    add_new_party = forms.BooleanField(required=False, label="Add a new customer / supplier")
    invoice_date = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))
    due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    supplier_state_code = forms.CharField(max_length=2, label="Supplier state code", help_text="Two-digit state code. For sales, use your business's state.")
    place_of_supply = forms.CharField(max_length=2, label="Place of supply state code")
    adjustment = forms.DecimalField(max_digits=3, decimal_places=2, min_value=Decimal("-1.00"), max_value=Decimal("1.00"), initial=0, label="Round-off adjustment (₹)")
    pricing_mode = forms.ChoiceField(choices=[("exclusive", "GST exclusive"), ("inclusive", "GST inclusive")], initial="exclusive")
    supply_category = forms.ChoiceField(choices=[("standard", "Standard domestic supply"), ("export", "Export — manual review required"), ("sez", "SEZ — manual review required"), ("reverse_charge", "Reverse charge — manual review required"), ("special", "Other special supply — manual review required")], initial="standard")
    invoice_discount_type = forms.ChoiceField(choices=[("fixed", "Fixed amount (₹)"), ("percent", "Percentage (%)")], initial="fixed")
    invoice_discount_value = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, initial=0, help_text="Allocated proportionally to taxable-classified lines (including 0%). Exempt, nil-rated and non-GST lines are excluded.")
    payment_terms = forms.CharField(max_length=255, required=False)
    notes = forms.CharField(required=False, max_length=5000, widget=forms.Textarea(attrs={"rows": 3}))
    terms_conditions = forms.CharField(label="Terms and conditions", required=False, max_length=5000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, business, invoice_type, invoice=None, **kwargs):
        self.business, self.invoice_type, self.invoice = business, invoice_type, invoice
        if args and args[0] is not None:
            data = args[0].copy()
            for name, default in (("pricing_mode", "exclusive"), ("supply_category", "standard"), ("invoice_discount_type", "fixed"), ("invoice_discount_value", "0")):
                data.setdefault(name, getattr(invoice, name, None) or default)
            args = (data, *args[1:])
        super().__init__(*args, **kwargs)
        kinds = ("customer", "both") if invoice_type == "sales" else ("vendor", "both")
        self.fields["party"].queryset = BusinessParty.objects.filter(business=business, party_type__in=kinds, is_active=True)
        self.fields["party"].label = "Customer" if invoice_type == "sales" else "Supplier"
        if invoice_type == "sales":
            self.fields["invoice_number"].disabled = True
            self.fields["invoice_number"].help_text = "Assigned automatically when the draft is saved; never reused."
        else:
            self.fields["invoice_number"].required = True
            self.fields["invoice_number"].label = "Supplier invoice number"
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field, forms.BooleanField) else "form-select" if isinstance(field, forms.ModelChoiceField) else "form-control"
        if invoice:
            self.initial.update({name: getattr(invoice, name) for name in ("invoice_number", "invoice_date", "due_date", "supplier_state_code", "place_of_supply", "payment_terms", "notes", "terms_conditions")})
            for name, default in (("pricing_mode", "exclusive"), ("supply_category", "standard"), ("invoice_discount_type", "fixed"), ("invoice_discount_value", "0")):
                self.initial[name] = getattr(invoice, name, None) or default
            self.initial.update(party=invoice.buyer_party_id if invoice_type == "sales" else invoice.supplier_party_id, adjustment=invoice.other_charges)
        elif invoice_type == "sales":
            self.initial["supplier_state_code"] = (business.gstin or "")[:2]

    def clean_invoice_date(self):
        value = self.cleaned_data["invoice_date"]
        if not 1900 <= value.year <= 9998:
            raise forms.ValidationError("Choose an invoice date between 1900 and 9998.")
        return value

    def clean_creation_token(self):
        token = self.cleaned_data.get("creation_token")
        if self.invoice:
            return token
        self.creation_key = creation_key_from_token(token, self.business.user_id, self.invoice_type)
        return token

    def clean(self):
        data = super().clean()
        if not data.get("party") and not data.get("add_new_party"):
            self.add_error("party", "Select an existing party or choose Add a new customer / supplier.")
        if data.get("invoice_date") and data.get("due_date") and data["due_date"] < data["invoice_date"]:
            self.add_error("due_date", "Due date cannot be earlier than invoice date.")
        for name in ("supplier_state_code", "place_of_supply"):
            code = data.get(name, "")
            if code not in {f"{number:02}" for number in (*range(1, 39), 97)}:
                self.add_error(name, "Enter a valid two-digit state code.")
        seller_gstin = self.business.gstin if self.invoice_type == "sales" else getattr(data.get("party"), "gstin", "")
        if seller_gstin and data.get("supplier_state_code") != seller_gstin[:2]:
            self.add_error("supplier_state_code", "Supplier state code must match the supplier GSTIN.")
        return data


class ManualLineForm(forms.Form):
    description = forms.CharField(max_length=500, label="Product / service")
    hsn_sac_code = forms.RegexField(r"^\d{4,8}$", required=False, max_length=8, label="HSN/SAC", error_messages={"invalid": "Use a 4–8 digit HSN/SAC code."})
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"), initial=1)
    unit = forms.CharField(max_length=30, required=False, initial="Nos")
    rate = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, label="Rate (₹, excluding GST)")
    discount_type = forms.ChoiceField(choices=[("fixed", "₹"), ("percent", "%")], initial="fixed", required=False)
    discount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, initial=0, label="Line discount")
    tax_treatment = forms.ChoiceField(choices=[("taxable", "Taxable (including 0%)"), ("exempt", "Exempt"), ("nil_rated", "Nil-rated"), ("non_gst", "Non-GST")], initial="taxable", required=False)
    tax_rate = forms.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100, initial=0, label="GST rate (%)")

    def __init__(self, *args, pricing_mode="exclusive", **kwargs):
        self.pricing_mode = pricing_mode
        if args and args[0] is not None:
            data=args[0].copy()
            prefix=kwargs.get("prefix", "")
            for name,default in (("discount_type","fixed"),("tax_treatment","taxable")):
                data.setdefault(f"{prefix}-{name}" if prefix else name,default)
            args=(data,*args[1:])
        super().__init__(*args, **kwargs)
        self.fields["tax_rate"] = forms.TypedChoiceField(choices=[("", "Select a reviewed GST rate")]+[(value, value+"%") for value in settings.GST_ALLOWED_RATES], coerce=Decimal, label="GST rate (%)", initial="0")
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        data = super().clean()
        data["discount_type"]=data.get("discount_type") or "fixed"
        data["tax_treatment"]=data.get("tax_treatment") or "taxable"
        if all(name in data for name in ("quantity", "rate", "discount", "tax_rate")):
            try:
                line_basis(data["quantity"], data["rate"], data["discount"], data["tax_rate"], pricing_mode=self.pricing_mode, discount_type=data.get("discount_type", "fixed"), tax_treatment=data.get("tax_treatment", "taxable"), allowed_rates=settings.GST_ALLOWED_RATES)
            except ValidationError as error:
                self.add_error(None, error)
        return data


ManualLineFormSet = formset_factory(ManualLineForm, extra=0, min_num=1, validate_min=True, max_num=100, validate_max=True, absolute_max=100, can_delete=True)
