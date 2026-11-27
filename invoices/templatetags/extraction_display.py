"""Read-only warning presentation. The full immutable message is always retained."""
from django import template
register=template.Library()

@register.inclusion_tag('invoices/_extraction_warnings.html')
def extraction_warnings(notices):
    groups={key:[] for key in ('Financial reconciliation','Identity and classification','Dates','Duplicates','Review requirements')}
    for notice in notices or []:
        message=str(notice);lower=message.lower()
        group=('Financial reconciliation' if any(word in lower for word in ('financial','reconcil','round-off','gst components')) else
               'Identity and classification' if any(word in lower for word in ('gstin','supplier','buyer','classification','pricing')) else
               'Dates' if 'date' in lower else 'Duplicates' if 'duplicate' in lower else 'Review requirements')
        # Keep discrepancies, amounts and identity repairs fully visible, not behind a toggle.
        compact=len(message)>300 and 'incomplete' in lower and 'inr ' not in lower and 'discrepancy' not in lower
        summary='Financial reconciliation incomplete — missing or conflicting evidence; human review is required.' if compact else message
        groups[group].append({'message':message,'summary':summary,'expandable':compact})
    severity={'Financial reconciliation':'critical','Identity and classification':'critical','Duplicates':'critical','Dates':'review','Review requirements':'review'}
    result=[{'title':key,'items':items,'severity':severity[key],'severity_label':'Critical' if severity[key]=='critical' else 'Needs review'} for key,items in groups.items() if items]
    return {'warning_groups':result,'warning_total':sum(len(g['items']) for g in result),
            'critical_total':sum(len(g['items']) for g in result if g['severity']=='critical')}
