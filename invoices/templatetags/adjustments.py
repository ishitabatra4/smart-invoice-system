from django import template
from invoices.adjustment_services import adjustments_available
from invoices.adjustment_reporting import adjusted_invoice_amount,overpayment
register=template.Library()

@register.inclusion_tag('invoices/adjustment_panel.html')
def adjustment_panel(invoice):
    ready=adjustments_available()
    return {'invoice':invoice,'ready':ready,'notes':invoice.adjustment_notes.all() if ready else [],
        'net':adjusted_invoice_amount(invoice),'overpayment':overpayment(invoice)}
