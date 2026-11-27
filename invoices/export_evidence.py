"""Read-only financial evidence for unconfirmed uploaded exports."""
from decimal import Decimal, InvalidOperation


def financial_export_evidence(invoice, lines):
    if invoice.entry_mode == 'manual' or invoice.status in {'verified', 'paid'}:
        return None
    from .extraction_services import invoice_snapshot
    attempt = invoice.extraction_attempts.first()
    if attempt:
        review = invoice.extraction_reviews.filter(attempt=attempt).first()
        if review and review.snapshot.get('invoice') == invoice_snapshot(invoice, lines=lines):
            return None
    result = attempt.result if attempt and isinstance(attempt.result, dict) else {}
    confidence = result.get('confidence') or {}
    from .extraction import financial_reconciliation, line_financial_reconciliation
    conflicts = set(line_financial_reconciliation(result)['conflicting_fields'])

    def field(source, key, metadata=None, row_conflict=False):
        raw = source.get(key)
        level = (metadata or {}).get('level', '')
        state = 'conflicting' if row_conflict or level == 'conflicting' else 'missing' if key not in source else 'unknown' if raw in (None, '') else 'extracted'
        value = None
        if state == 'extracted':
            try:
                value = Decimal(str(raw))
                if not value.is_finite():
                    raise InvalidOperation
                precision = Decimal('.001') if key == 'quantity' else Decimal('.01')
                if value != value.quantize(precision):
                    raise InvalidOperation
            except (InvalidOperation, ValueError, TypeError):
                state, value = 'conflicting', None
        return {'value': value, 'state': state, 'raw': str(raw) if raw is not None else '',
                'reason': (metadata or {}).get('reason', '')}

    header = {key: field(result, key, confidence.get(key)) for key in (
        'taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'other_charges',
        'rounding_adjustment', 'total_amount', 'invoice_discount_amount', 'total_discount_amount')}
    if financial_reconciliation(result)['status'] == 'mismatch':
        header['total_amount'].update(value=None, state='conflicting')
    extracted = result.get('line_items') or []
    rows = []
    for index, line in enumerate(lines):
        source = extracted[index] if index < len(extracted) and isinstance(extracted[index], dict) else {}
        mismatch = bool(source and str(source.get('description', '')) != line.description)
        evidence = {key: field(source, key, (source.get('confidence') or {}).get(key),
                               mismatch or 'line_items' in conflicts or key in conflicts)
                    for key in ('quantity', 'rate', 'discount', 'tax_rate', 'taxable_amount', 'tax_amount', 'total_amount')}
        # A printed percentage is evidence of a percentage, not a saved monetary reduction.
        if source.get('discount_type') == 'percent' and evidence['discount']['value'] is not None:
            evidence['discount'].update(value=None, state='unknown', reason='Printed percentage discount; monetary reduction requires review.')
        rows.append(evidence)
    return {'header': header, 'lines': rows, 'attempt_id': attempt.pk if attempt else None}


def evidence_note(key, field):
    raw = ('; extracted candidate: ' + field['raw']) if field['raw'] else ''
    reason = ('; ' + field['reason']) if field['reason'] else ''
    return key.replace('_', ' ').title() + ': ' + field['state'] + ' — unconfirmed' + raw + reason
