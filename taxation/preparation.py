"""Read-only gathering of saved records and explicit immutable preparation revisions."""
import calendar
import hashlib
import json
from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from accounts.models import BusinessProfile
from audit.services import record_event
from invoices.models import Invoice
from invoices.adjustment_reporting import note_events
from invoices.manual_calculations import decimal, validate_stored_totals, validate_supply
from expenses.reporting import saved_classifications
from expenses.services import itc_state
from analytics.services import build_analytics_context
from .models import TaxPeriod, TaxWorkingPaper, TaxWorkingPaperEvent, ReconciliationRun
from .tax_rules import rule_bundle, CALCULATION_VERSION, b2c_bucket, estimate_income
ZERO = Decimal('0.00')
COMPONENTS = ('taxable', 'cgst', 'sgst', 'igst', 'total')
GST_KINDS = {'gstr1', 'gstr3b', 'itc', 'liability'}

def serial(value):
    return json.loads(json.dumps(value, default=lambda x: format(x.copy_abs() if x == 0 else x, 'f') if isinstance(x, Decimal) else x.isoformat() if isinstance(x, date) else str(x)))

def digest(value):
    return hashlib.sha256(json.dumps(serial(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def ready():
    return {TaxPeriod._meta.db_table, TaxWorkingPaper._meta.db_table, TaxWorkingPaperEvent._meta.db_table} <= set(connection.introspection.table_names())

def empty():
    return {key: ZERO for key in COMPONENTS}

def amounts(invoice):
    return {key: getattr(invoice, 'taxable_amount' if key == 'taxable' else 'total_amount' if key == 'total' else key + '_amount') for key in COMPONENTS}

def add(bucket, values, sign=1):
    for key in COMPONENTS:
        bucket[key] += sign * decimal(values[key])

def validate_period(kind, start, end):
    if kind not in dict(TaxPeriod.KINDS) or start > end or (end - start).days > 365 or (start.year < 1900) or (end.year >= 9999):
        raise ValidationError('Choose an ordered period of at most 366 days, within supported calendar bounds.')
    if kind == 'monthly' and (start.day != 1 or end != date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])):
        raise ValidationError('A monthly period must cover the complete calendar month.')
    if kind == 'quarterly':
        if start.month not in (1, 4, 7, 10) or start.day != 1 or end != date(start.year, start.month + 2, calendar.monthrange(start.year, start.month + 2)[1]):
            raise ValidationError('Select a complete calendar/GST quarter (Apr–Jun, Jul–Sep, Oct–Dec or Jan–Mar).')
    if kind == 'financial_year' and (start != date(start.year, 4, 1) or end != date(start.year + 1, 3, 31)):
        raise ValidationError('Indian financial years run April 1 through March 31.')

@transaction.atomic
def create_period(request, kind, start_date, end_date):
    validate_period(kind, start_date, end_date)
    business = BusinessProfile.objects.select_for_update().get(user=request.user)
    (period, created) = TaxPeriod.objects.get_or_create(business=business, start_date=start_date, end_date=end_date, defaults={'kind': kind, 'created_by': request.user})
    if created:
        record_event(request=request, action='create', module='taxation', description='Tax preparation started; period selected, no filing.', obj=period, after={'start': start_date, 'end': end_date, 'kind': kind})
    return period

def gather(period, run_ids=()):
    period.business = BusinessProfile.objects.select_related('user').get(pk=period.business_id)
    user = period.business.user
    from invoices.date_evidence import with_invoice_dates
    qs = with_invoice_dates(Invoice.objects.filter(user=user)).filter(evidence_invoice_date__range=(period.start_date, period.end_date)).select_related('supplier_party', 'buyer_party').prefetch_related('line_items', 'payments')
    if qs.count() > 2000:
        raise ValidationError('Narrow the period: working papers support at most 2,000 source invoices.')
    invoices = list(qs)
    lines = sum((len(i.line_items.all()) for i in invoices))
    if lines > 10000:
        raise ValidationError('Working papers support at most 10,000 saved lines.')
    events = note_events(user, period.start_date, period.end_date)
    if len(events) > 2000:
        raise ValidationError('Narrow the period: at most 2,000 adjustment events.')
    normalized_ids = []
    for value in run_ids:
        try:
            normalized_ids.append(int(value))
        except (TypeError, ValueError):
            raise ValidationError('Choose owned import runs.') from None
    runs = list(ReconciliationRun.objects.filter(user=user, pk__in=normalized_ids).prefetch_related('records'))
    if len(runs) != len(set(normalized_ids)):
        raise ValidationError('Import runs must belong to this business.')
    if sum((len(r.records.all()) for r in runs)) > 10000:
        raise ValidationError('At most 10,000 imported comparison rows are supported.')
    from .reconciliation import parse_uploaded_file, ReconciliationFileError
    import_evidence = []
    for run in runs:
        file_hash = 'unavailable'
        file_error = ''
        try:
            with run.source_file.open('rb') as source:
                h = hashlib.sha256()
                size = 0
                for chunk in source.chunks():
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise OSError
                    h.update(chunk)
                file_hash = h.hexdigest()
                source.seek(0)
                parsed = parse_uploaded_file(source, reject_duplicates=True)
                fields = ('supplier_gstin', 'invoice_number', 'invoice_date', 'taxable_value', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_tax')
                saved = list(run.records.values(*fields))
                if sorted(digest(row) for row in parsed) != sorted(digest(row) for row in saved):
                    file_error = 'source_mismatch'
                if any(row['total_tax'] != row['cgst_amount'] + row['sgst_amount'] + row['igst_amount'] for row in parsed):
                    file_error = 'amount_mismatch'
        except ReconciliationFileError as exc:
            file_error = 'duplicate' if 'Duplicate' in str(exc) else 'unsupported_format'
        except (OSError, ValueError):
            file_error = 'missing_evidence'
        import_evidence.append({
            'id': run.pk,
            'return_period': run.return_period,
            'file_sha256': file_hash,
            'validation': file_error or 'supported_user_extract',
            'provenance': 'user_uploaded_not_independently_authenticated',
            'records': list(run.records.values('pk', 'supplier_gstin', 'invoice_number', 'invoice_date', 'taxable_value', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_tax', 'status', 'matched_invoice_id', 'manually_matched_at')),
        })
    source = []
    for invoice in invoices:
        state = itc_state(invoice) if invoice.invoice_type in ('purchase', 'expense') else None
        source.append({
            'id': invoice.pk,
            'type': invoice.invoice_type,
            'entry_mode': invoice.entry_mode,
            'document_state': invoice.document_state,
            'status': invoice.status,
            'number': invoice.invoice_number,
            'date': invoice.evidence_invoice_date,
            'due_date': invoice.due_date,
            'supplier_name': invoice.supplier_name,
            'supplier_gstin': invoice.supplier_gstin,
            'buyer_name': invoice.buyer_name,
            'buyer_gstin': invoice.buyer_gstin,
            'pricing_mode': invoice.pricing_mode,
            'supplier_state': invoice.supplier_state_code,
            'place_of_supply': invoice.place_of_supply,
            'supply_category': invoice.supply_category,
            'calculation_version': invoice.calculation_version,
            'values': amounts(invoice),
            'other_charges': invoice.other_charges,
            'classification': saved_classifications(invoice),
            'lines': list(invoice.line_items.values('pk', 'description', 'hsn_sac_code', 'quantity', 'unit', 'rate', 'tax_rate', 'tax_treatment', 'taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'tax_amount', 'total_amount')),
            'payments': list(invoice.payments.filter(user=user).values('pk', 'amount', 'payment_date', 'reference_id')),
            'itc': {'status': state['status'], 'amount': state['amount'], 'reason': state['reason'], 'review_id': state['review'].pk if state.get('review') else None} if state else None,
        })
    event_rows = []
    for event in events:
        n = event['note']
        event_rows.append({
            'id': n.pk,
            'date': event['date'],
            'original_invoice_id': n.original_invoice_id,
            'original_number': n.original_invoice.invoice_number,
            'type': event['invoice_type'],
            'kind': n.kind,
            'direction': n.direction,
            'number': n.number,
            'reversal': event['reversal'],
            'tax_effect': n.tax_effect,
            'values': {
                k: event['taxable_amount' if k == 'taxable' else 'total_amount' if k == 'total' else k + '_amount']
                for k in COMPONENTS
            },
            'lines': list(n.line_items.values('pk', 'hsn_sac_code', 'quantity', 'unit', 'taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'total_amount')),
        })
    profile = {
        'id': period.business_id,
        'name': period.business.business_name,
        'gstin': period.business.gstin,
        'business_type': period.business.business_type,
    }
    analytics = build_analytics_context(user, period.start_date, period.end_date, 'custom')
    financial_summary = {k: analytics[k] for k in ('totals', 'gross_original', 'net_cash_flow')}
    evidence = {
        'business': profile,
        'invoices': source,
        'adjustments': event_rows,
        'imports': import_evidence,
        'financial_summary': financial_summary,
    }
    return (invoices, source, event_rows, import_evidence, digest(evidence), evidence)

def compare_imports(sources, imports):
    from .reconciliation import normalize_invoice_number
    records = [{**r, 'run_id': run['id'], 'file_validation': run['validation']} for run in imports for r in run['records']]
    output = []
    used = set()
    for source in sources:
        if source['type'] not in ('purchase', 'expense') or source['status'] not in ('verified', 'paid'):
            continue
        matches = [r for r in records if normalize_invoice_number(r['invoice_number']) == normalize_invoice_number(source['number']) and source['number']]
        exact = [r for r in matches if r['supplier_gstin'].strip().upper() == source['supplier_gstin'].strip().upper()]
        candidates = exact or matches
        flags = []
        from invoices.gstin import gstin_evidence
        if not gstin_evidence(source['supplier_gstin'])['checksum_valid']:
            flags.append('missing_evidence')
        if not imports or not candidates:
            flags.append('missing_evidence' if not imports else 'unmatched')
        elif len(candidates) > 1:
            flags.append('duplicate')
        else:
            r = candidates[0]
            used.add(r['pk'])
            if r['file_validation'] != 'supported_user_extract':
                flags.append(r['file_validation'])
            if r['supplier_gstin'].strip().upper() != source['supplier_gstin'].strip().upper():
                flags.append('gstin_mismatch')
            if r['invoice_number'] != source['number']:
                flags.append('pending_review')
            if r['invoice_date'] != source['date']:
                flags.append('date_mismatch')
            if r['total_tax'] != sum((source['values'][c] for c in ('cgst', 'sgst', 'igst')), ZERO):
                flags.append('amount_mismatch')
            if any((r[field] != source['values'][key] for (key, field) in [('taxable', 'taxable_value'), ('cgst', 'cgst_amount'), ('sgst', 'sgst_amount'), ('igst', 'igst_amount')])):
                flags.append('amount_mismatch')
        output.append({
            'invoice_id': source['id'],
            'number': source['number'],
            'status': flags[0] if flags else 'matched',
            'flags': flags,
            'candidate_import_ids': [r['pk'] for r in candidates],
            'itc_status': source['itc']['status'],
            'approval': 'Comparison never approves ITC; uploaded extract is not authenticated official evidence.',
        })
    return {'book_records': output, 'unmatched_imports': [{'record_id': r['pk'], 'run_id': r['run_id'], 'number': r['invoice_number']} for r in records if r['pk'] not in used], 'provenance': 'User-supplied normalized XLSX/JSON/B2B extract; no claim of official portal reconciliation.'}

def preview(period, kind, inputs):
    if kind not in dict(TaxWorkingPaper.KINDS):
        raise ValidationError('Choose a supported working-paper type.')
    validate_period(period.kind, period.start_date, period.end_date)
    (invoices, sources, events, imports, source_hash, evidence) = gather(period, inputs.get('import_runs', []))
    warnings = []

    def warn(code, message, severity='review', source_id=None):
        warnings.append({'code': code, 'severity': severity, 'source_id': source_id, 'message': message})
    if kind in GST_KINDS:
        from invoices.gstin import gstin_evidence
        if not gstin_evidence(period.business.gstin or '')['checksum_valid']:
            warn('business_gstin', 'Business GSTIN is missing or fails local checksum; registration is not verified.', 'critical')
        if not inputs.get('ordinary_domestic'):
            warn('scope', 'Confirm standard domestic scope; special supplies cannot be mapped automatically.', 'critical')
        if period.start_date < date(2017, 7, 1):
            warn('gst_period', 'No GST working rules before July 2017.', 'critical')
    if period.end_date > timezone.localdate():
        warn('open_period', 'Period is not complete; this is an estimate using available records.', 'info')
    if ready() and TaxPeriod.objects.filter(business=period.business, start_date__lte=period.end_date, end_date__gte=period.start_date).exclude(pk=period.pk).exists():
        warn('overlap', 'Overlapping periods exist; working papers are not additive.', 'info')
    g1 = {k: empty() for k in ['B2B', 'B2CL', 'B2CS', 'nil_rated', 'exempt', 'non_gst', 'unresolved']}
    g3 = {k: empty() for k in ['3.1(a)_candidate', '3.1(c)_candidate', '3.1(e)_candidate', 'unresolved']}
    out = empty()
    inp = empty()
    input_buckets = {k: empty() for k in ['confirmed', 'potential', 'pending', 'ineligible', 'reversed']}
    included = []
    excluded = []
    hsn = {}
    identities = set()
    registration = inputs.get('registration', {})
    if not isinstance(registration, dict) or set(registration) - {str(x.pk) for x in invoices if x.invoice_type == 'sales'}:
        raise ValidationError('Registration evidence must reference owned sales in this period.')
    for (inv, source) in zip(invoices, sources):
        values = source['values']
        sid = source['id']
        eligible = inv.status in ('verified', 'paid') and (inv.entry_mode != 'manual' or inv.document_state == 'finalized')
        if not eligible:
            excluded.append({
                'id': sid,
                'number': inv.invoice_number,
                'status': inv.status,
                'reason': 'Not a recognized/finalized source',
                'values': values,
            })
            if inv.status == 'cancelled':
                warn('cancelled_source', 'Cancelled original excluded; amendment/cancellation timing requires independent review.', source_id=sid)
            continue
        included.append(source)
        if kind in GST_KINDS:
            if inv.invoice_type == 'sales':
                if not inv.supplier_gstin:
                    warn('issuer_gstin_missing', 'Saved issuer GSTIN is missing; business identity requires independent review.', source_id=sid)
                elif inv.supplier_gstin.strip().upper() != (period.business.gstin or '').strip().upper():
                    warn('issuer_gstin_mismatch', 'Saved sales issuer GSTIN differs from the selected business registration; resolve scope before GST preparation.', 'critical', sid)
            elif inv.buyer_gstin and inv.buyer_gstin.strip().upper() != (period.business.gstin or '').strip().upper():
                warn('input_buyer_gstin_mismatch', 'Saved input document names another buyer GSTIN; ownership/eligibility requires independent review.', source_id=sid)
        issues = validate_stored_totals(inv.taxable_amount, inv.cgst_amount, inv.sgst_amount, inv.igst_amount, inv.other_charges, inv.total_amount)
        if issues:
            warn('amount_mismatch', '; '.join(issues), 'critical', sid)
        if any((value < 0 for value in values.values())):
            warn('negative_source', 'Negative original financial value requires review.', 'critical', sid)
        if not inv.invoice_number:
            warn('missing_number', 'Invoice reference is missing.', 'critical', sid)
        key = (inv.invoice_type, inv.supplier_gstin if inv.invoice_type != 'sales' else inv.buyer_gstin, inv.invoice_number.strip().lower())
        if key in identities:
            warn('duplicate_invoice', 'Duplicate business document identity; resolve before preparing.', 'critical', sid)
        identities.add(key)
        if source['lines'] and inv.calculation_version == 1:
            if sum((line['taxable_amount'] for line in source['lines']), ZERO) != inv.taxable_amount or any((sum((line[c + '_amount'] or ZERO for line in source['lines']), ZERO) != values[c] for c in ('cgst', 'sgst', 'igst'))):
                warn('line_header_mismatch', 'Saved lines and header do not reconcile; no historical recalculation performed.', 'critical', sid)
        if not source['lines']:
            warn('missing_lines', 'No reliable saved lines; HSN/document evidence is incomplete.', source_id=sid)
        if inv.invoice_type != 'sales':
            add(inp, values)
            status = source['itc']['status']
            bucket = status if status in input_buckets else 'pending'
            add(input_buckets[bucket], values)
            if bucket != 'confirmed':
                warn('itc_' + bucket, 'Input GST is not confirmed eligible; no automatic credit claim.', source_id=sid)
            continue
        add(out, values)
        classes = source['classification']
        mapped = True
        try:
            interstate = not validate_supply(inv.supplier_state_code, inv.place_of_supply, inv.supply_category)
        except ValidationError:
            mapped = False
            interstate = None
            warn('supply_classification', 'Missing/special supply information; retained in unresolved bucket.', source_id=sid)
        if mapped:
            if (interstate and (values['cgst'] or values['sgst'])) or (not interstate and (values['igst'] or abs(values['cgst']-values['sgst']) > Decimal('0.01'))):
                warn('component_supply_mismatch', 'Saved GST components conflict with explicit supply states; source correction requires its normal workflow.', 'critical', sid)
        if inv.other_charges != 0:
            warn('rounding_or_charges', 'Header rounding/charges retained; return-specific allocation needs review.', source_id=sid)
        reg = registration.get(str(sid), {})
        recipient = reg.get('status', 'unknown')
        from invoices.gstin import gstin_evidence
        if recipient not in {'registered', 'unregistered', 'unknown'}:
            raise ValidationError('Invalid recipient classification.')
        if recipient != 'unknown' and (not (reg.get('reason') or '').strip()):
            warn('registration_evidence', 'Recipient registration requires explicit evidence/reason.', source_id=sid)
            recipient = 'unknown'
        if recipient == 'registered' and (not gstin_evidence(inv.buyer_gstin)['checksum_valid']):
            warn('recipient_gstin', 'Registered recipient GSTIN missing/invalid; not classified as B2C.', source_id=sid)
            recipient = 'unknown'
        if recipient == 'unregistered' and inv.buyer_gstin:
            warn('conflicting_registration', 'Saved GSTIN conflicts with unregistered declaration; no silent reclassification.', source_id=sid)
            recipient = 'unknown'
        if recipient == 'unknown':
            warn('registration_unknown', 'Registration not established; invalid/missing GSTIN alone never implies B2C.', source_id=sid)
        if classes['unclassified'] or not mapped:
            add(g1['unresolved'], values)
            add(g3['unresolved'], values)
            warn('tax_treatment', 'Saved tax classification/supply is unresolved; amounts were not discarded.', source_id=sid)
        else:
            if classes['taxable'] and (not sum((values[c] for c in ('cgst', 'sgst', 'igst')), ZERO)):
                warn('zero_taxable', 'Zero-GST taxable treatment needs independent review; not inferred as nil-rated.', source_id=sid)
            if not classes['taxable'] and sum((values[c] for c in ('cgst', 'sgst', 'igst')), ZERO):
                warn('classification_mismatch', 'Saved non-taxable classification conflicts with GST components.', 'critical', sid)
            taxable_values = {
                **values,
                'taxable': classes['taxable'],
                'total': classes['taxable'] + values['cgst'] + values['sgst'] + values['igst'],
            }
            bucket = 'B2B' if recipient == 'registered' else b2c_bucket(inv.invoice_date, inv.total_amount, interstate) if recipient == 'unregistered' else 'unresolved'
            add(g1[bucket], taxable_values)
            add(g3['3.1(a)_candidate'], taxable_values)
            for classification in ('nil_rated', 'exempt', 'non_gst'):
                v = {**empty(), 'taxable': classes[classification], 'total': classes[classification]}
                add(g1[classification], v)
                add(g3['3.1(e)_candidate' if classification == 'non_gst' else '3.1(c)_candidate'], v)
        for line in source['lines']:
            code = line['hsn_sac_code']
            min_digits = 6 if inputs.get('aato_band') == 'above_5cr' else 4
            if not code.isdigit() or len(code) < min_digits:
                warn('hsn_missing', 'HSN/SAC evidence missing/too short for selected turnover band.', source_id=sid)
            hkey = (recipient, code, line['unit'], str(line['tax_rate']), line['tax_treatment'])
            if hkey not in hsn:
                hsn[hkey] = {
                    'recipient': recipient,
                    'code': code,
                    'unit': line['unit'],
                    'rate': line['tax_rate'],
                    'treatment': line['tax_treatment'],
                    'quantity': ZERO,
                    **empty(),
                }
            hsn[hkey]['quantity'] += line['quantity']
            add(hsn[hkey], {k: line['taxable_amount' if k == 'taxable' else 'total_amount' if k == 'total' else k + '_amount'] or ZERO for k in COMPONENTS})
    if not inputs.get('aato_band') or inputs.get('aato_band') == 'unknown':
        warn('hsn_aato', 'Prior-year turnover band, HSN master description and portal UQC need review.', 'review')
    adjustments = {'sales': empty(), 'purchase': empty(), 'expense': empty()}
    output_adjustment = empty()
    input_adjustment = empty()
    for event in events:
        side = event['type']
        add(adjustments[side], event['values'])
        if event['tax_effect'] == 'gst':
            add(output_adjustment if side == 'sales' else input_adjustment, event['values'])
        warn('note_mapping', 'Note/cancellation event retained by document date; statutory amendment/deadline/table mapping needs independent review.', source_id=event['id'])
    reviewed = {c: input_buckets['confirmed'][c] for c in ('cgst', 'sgst', 'igst')}
    net_output = {c: out[c] + output_adjustment[c] for c in ('cgst', 'sgst', 'igst')}
    difference = {c: net_output[c] - reviewed[c] for c in reviewed}
    if any((v < 0 for v in net_output.values())):
        warn('negative_output', 'Negative net output retained; carry-forward/return handling unresolved.')
    comparison = compare_imports(included, imports)
    for row in comparison['book_records']:
        if row['status'] != 'matched':
            warn('import_' + row['status'], 'Imported comparison requires review; a match never approves ITC.', source_id=row['invoice_id'])
    ctx = evidence['financial_summary']
    data = {
        'title': dict(TaxWorkingPaper.KINDS)[kind] + ' — working paper, not filed',
        'period': {'id': period.pk, 'kind': period.kind, 'start': period.start_date, 'end': period.end_date},
        'calculation_version': CALCULATION_VERSION,
        'rule_bundle': rule_bundle(),
        'source_evidence': evidence,
        'included': included,
        'excluded': excluded,
        'gstr1': {
            'buckets': g1,
            'original_outward': out,
            'net_gst_adjusted_outward': {k: out[k] + output_adjustment[k] for k in COMPONENTS},
            'adjustments': events,
            'hsn_invoice_evidence': list(hsn.values()),
            'documents': {
                'included_sales': sum((x['type'] == 'sales' for x in included)),
                'excluded_sources': excluded,
                'limitation': 'Saved references/counts only; no portal document-series completeness certification.',
            },
            'limitations': [
                'Standard domestic only; no portal-ready JSON.',
                'Notes/amendments and HSN adjustments remain separately disclosed evidence, not complete statutory Table 9/12 mapping.',
            ],
        },
        'gstr3b': {
            'original_buckets': g3,
            'output_adjustments': output_adjustment,
            'net_output_components': net_output,
            'input_original': inp,
            'input_adjustments': input_adjustment,
            'itc_buckets': input_buckets,
            'owner_reviewed_input_components': reviewed,
            'limitations': [
                'Document-date working allocation, not ITC availment-period determination.',
                'Owner review does not establish government matching/claimability.',
                'RCM, ISD, import tax, cess, interest, late fees and utilization ordering unresolved.',
            ],
        },
        'liability': {
            'net_output': net_output,
            'reviewed_input': reviewed,
            'component_difference': difference,
            'total_difference': sum(difference.values(), ZERO),
            'limitation': 'Simplified signed component differences only, not statutory offsets, refund or government payable.',
        },
        'reconciliation': comparison,
        'adjustment_events': events,
        'income_summary': {
            'saved_analytics': {k: ctx[k] for k in ('totals', 'gross_original', 'net_cash_flow')},
            'direct_expense_before_gst': sum((x['values']['taxable'] for x in included if x['type'] == 'expense'), ZERO),
            'accounting_profit': None,
            'taxable_income': None,
            'limitation': 'Purchases are not automatically deductible/COGS. Inventory, depreciation, private use, other income and tax adjustments need explicit independent assessment.',
        },
        'income_tax': estimate_income(period, inputs, period.business.business_type) if kind == 'income_tax' else None,
        'warnings': warnings,
    }
    metric_labels = {
        'sales': 'Recognized sales including GST and dated financial adjustments',
        'purchases': 'Recognized purchases including GST and dated financial adjustments',
        'expenses': 'Recognized direct expenses including GST',
        'taxable_sales': 'Sales value before GST (all saved classifications)',
        'taxable_purchases': 'Purchase value before GST (all saved classifications)',
        'payments_received': 'Cash received during period', 'payments_made': 'Cash paid during period',
        'receivable': 'Outstanding on period-source receivables at period end',
        'payable': 'Outstanding on period-source payables at period end',
        'output_gst': 'Saved output GST with dated adjustments',
        'input_gst': 'Saved purchase GST with dated adjustments; eligibility unresolved',
    }
    data['income_summary']['aggregates'] = {label: ctx['totals'][key] for key, label in metric_labels.items()}
    if data['income_tax'] and data['income_tax']['status'] != 'estimated':
        for reason in data['income_tax']['reasons']:
            warn('income_manual', reason)
    for run in imports:
        try:
            (y, m) = map(int, run['return_period'].split('-'))
            run_start = date(y, m, 1)
            run_end = date(y, m, calendar.monthrange(y, m)[1])
            if run_start < period.start_date or run_end > period.end_date:
                warn('import_period', 'Selected import month is not contained in this preparation period.')
        except (ValueError, TypeError):
            warn('import_period', 'Selected import period is invalid.')
    data['warnings'] = warnings
    if len(json.dumps(serial(data)).encode()) > 8 * 1024 * 1024:
        raise ValidationError('Working paper exceeds 8 MB; narrow the period.')
    return (serial(data), source_hash, digest(rule_bundle()))

@transaction.atomic
def prepare(request, period, kind, inputs, request_key, expected_preview, previous_id, confirmed):
    if not confirmed:
        raise ValidationError('Explicit review of sources, warnings and limitations is required.')
    if not ready():
        raise ValidationError('Tax working-paper migration approval is required.')
    BusinessProfile.objects.filter(pk=period.business_id, user=request.user).update(updated_at=F('updated_at'))
    period = TaxPeriod.objects.select_for_update().get(pk=period.pk, business__user=request.user)
    existing = TaxWorkingPaper.objects.filter(request_key=request_key, period=period, kind=kind, prepared_by=request.user).first()
    if existing:
        if existing.inputs != serial(inputs):
            raise ValidationError('This request key was used for different inputs.')
        return existing
    latest = TaxWorkingPaper.objects.filter(period=period, kind=kind).order_by('-revision').first()
    if (latest.pk if latest else None) != previous_id:
        raise ValidationError('A working paper already exists or a newer revision was prepared. Explicitly revise the latest snapshot.')
    if latest and latest.status not in {'reviewed', 'prepared'}:
        raise ValidationError('Only a current active working paper can be revised.')
    (data, source_hash, rule_hash) = preview(period, kind, inputs)
    if digest(data) != expected_preview:
        raise ValidationError('Sources, rules or inputs changed since preview; review again.')
    if any((w['severity'] == 'critical' for w in data['warnings'])):
        raise ValidationError('Resolve critical validation requirements before preparing a snapshot.')
    paper = TaxWorkingPaper.objects.create(period=period, kind=kind, revision=latest.revision + 1 if latest else 1, previous=latest, prepared_by=request.user, request_key=request_key, calculation_version=CALCULATION_VERSION, inputs=serial(inputs), data=data, source_fingerprint=source_hash, rule_fingerprint=rule_hash)
    TaxWorkingPaperEvent.objects.create(paper=paper, actor=request.user, status='prepared', reason='Explicit owner review of preview and limitations.')
    if latest:
        TaxWorkingPaperEvent.objects.create(paper=latest, actor=request.user, status='superseded', reason=f'Explicit revision {paper.revision}.')
        record_event(request=request, action='update', module='taxation', description='Tax snapshot superseded by explicit revision.', obj=latest, metadata={'replacement_id': paper.pk})
    record_event(request=request, action='create', module='taxation', description='Tax working snapshot prepared after owner review; not filed.', obj=paper, metadata={'period_id': period.pk, 'revision': paper.revision, 'source_count': len(data['included']), 'version': CALCULATION_VERSION})
    return paper

def stale(paper):
    (_, _, _, _, current, _) = gather(paper.period, paper.inputs.get('import_runs', []))
    return current != paper.source_fingerprint or digest(rule_bundle()) != paper.rule_fingerprint

@transaction.atomic
def transition(request, paper, status, reason):
    BusinessProfile.objects.filter(pk=paper.period.business_id, user=request.user).update(updated_at=F('updated_at'))
    paper = TaxWorkingPaper.objects.select_for_update().get(pk=paper.pk, period__business__user=request.user)
    if status not in {'reviewed', 'archived'} or not reason.strip():
        raise ValidationError('Choose review/archive and supply a reason; filed/paid statuses are unsupported.')
    if paper.status in {'archived', 'superseded'}:
        raise ValidationError('Archived/superseded working papers cannot be reopened.')
    if status == 'reviewed' and stale(paper):
        raise ValidationError('Source/rule changes require an explicit new revision before review.')
    if paper.status == status:
        return paper
    TaxWorkingPaperEvent.objects.create(paper=paper, actor=request.user, status=status, reason=reason[:500])
    record_event(request=request, action='verify' if status == 'reviewed' else 'update', module='taxation', description='Tax working-paper review completed.' if status == 'reviewed' else 'Tax working paper archived; immutable body retained.', obj=paper, after={'working_status': status})
    return paper
