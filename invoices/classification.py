"""Current, non-mutating role evidence; never automatic invoice classification."""
from difflib import SequenceMatcher
from .gstin import gstin_evidence, validate_registration_state
from django.core.exceptions import ValidationError


def classification_assessment(business, fields, invoice_type):
    own = gstin_evidence(business.gstin)
    confidence = fields.get('confidence') or {}
    messages = []
    reliable = {}
    for role in ('supplier', 'buyer'):
        gst = gstin_evidence(fields.get(role + '_gstin') or '')
        level = confidence.get(role + '_gstin', {}).get('level')
        reliable[role] = bool(gst['checksum_valid'] and level not in {'missing', 'low', 'conflicting'})
        reliable[role + '_match'] = reliable[role] and gst['value'] == own['value']
    if not own['checksum_valid'] or not all(reliable[r] for r in ('supplier', 'buyer')):
        status, strength = 'insufficient', 'weak'
        messages.append('Insufficient reliable GSTIN evidence for invoice classification. Missing, invalid or conflicting identities need human review; no type was inferred.')
    elif reliable['supplier_match'] and reliable['buyer_match']:
        status, strength = 'ambiguous', 'strong'
        messages.append('Ambiguous classification: your business appears in both supplier and buyer roles. Review the original; neither role establishes a safe automatic type.')
    elif reliable['supplier_match'] or reliable['buyer_match']:
        as_seller = reliable['supplier_match']
        consistent = (invoice_type == 'sales') == as_seller
        status, strength = ('consistent' if consistent else 'wrong_type'), 'strong'
        if consistent:
            messages.append('GSTIN role evidence is consistent with the selected type; it does not confirm financial values or registration.')
        else:
            messages.append('Possible incorrect invoice type: your business appears as the ' + ('supplier' if as_seller else 'buyer') + ', but the current selection is ' + dict(sales='Sales Invoice', purchase='Purchase Invoice', expense='Expense Invoice').get(invoice_type, 'unsupported') + '. Review the selected invoice type before confirmation.')
        if not as_seller:
            messages.append('Buyer identity cannot distinguish a purchase from an expense. Choose that classification explicitly.')
    else:
        status, strength = 'identity_mismatch', 'strong'
        messages.append('Business identity mismatch: neither reliable party GSTIN matches your business. Review the source and workspace; do not substitute identities to force confirmation.')
    own_name = ' '.join((business.business_name or '').casefold().split())
    names = {role: ' '.join(str(fields.get(role + '_name') or '').casefold().split()) for role in ('supplier', 'buyer')}
    suggestions = [r for r, name in names.items() if own_name and name and SequenceMatcher(None, own_name, name).ratio() >= .85]
    if suggestions:
        messages.append('Name-only suggestion (not proof): business name resembles the ' + ' and '.join(suggestions) + '. Reliable GSTINs take precedence; a name match never authorizes acceptance.')
    if strength == 'strong' and ((reliable['supplier_match'] and 'buyer' in suggestions and 'supplier' not in suggestions) or (reliable['buyer_match'] and 'supplier' in suggestions and 'buyer' not in suggestions)):
        messages.append('Name and GSTIN role evidence conflict. Compare the labelled supplier/buyer blocks manually; GSTIN evidence has not been replaced by the name suggestion.')
    for gst, state, label in ((business.gstin, business.state, 'Business profile'), (fields.get('supplier_gstin'), fields.get('supplier_state_code'), 'Supplier'), (fields.get('buyer_gstin'), fields.get('buyer_state_code'), 'Buyer')):
        try:
            validate_registration_state(gst, state, label)
        except ValidationError:
            messages.append(label + ' GSTIN/state evidence is invalid or inconsistent; existing financial identity validation still applies.')
    if fields.get('detected_document_kind') in {'credit note', 'debit note', 'quotation', 'proforma invoice'}:
        messages.append('The printed document label requires its dedicated document workflow; changing invoice type cannot make this an accepted accounting invoice.')
    return {'status': status, 'strength': strength, 'messages': messages,
            'selected_type': invoice_type, 'supplier_match': reliable['supplier_match'], 'buyer_match': reliable['buyer_match']}
