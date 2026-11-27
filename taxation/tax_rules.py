"""Versioned, deliberately bounded working-paper rules. No network calls or filing."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from invoices.manual_calculations import decimal, money
CALCULATION_VERSION = 'tax-working-v11.1'
REFERENCES = {
    'gst1': {
        'title': 'GST Portal GSTR-1 user guide',
        'url': 'https://tutorial.gst.gov.in/userguide/returns/Creation_of_Outward_Supplies_Return_in_GSTR-1.htm',
        'verified_on': '2026-11-22',
        'effective': 'B2CL threshold change 2024-08-01',
        'scope': 'Standard domestic supplies, explicit recipient evidence; no portal JSON',
    },
    'hsn': {
        'title': 'GST Portal Table 12/13 advisory, 25 April 2025',
        'url': 'https://tutorial.gst.gov.in/downloads/news/updated_advisory_hsn_table12_25042025.pdf',
        'verified_on': '2026-11-22',
        'effective': '2025-05-01',
        'scope': 'Separate B2B/B2C HSN evidence; AATO, master description/UQC and document series require review',
    },
    'gst3b': {
        'title': 'GST Portal GSTR-3B FAQs',
        'url': 'https://tutorial.gst.gov.in/userguide/returns/GSTR3B.htm',
        'verified_on': '2026-11-22',
        'effective': 'Reference checked 2026-11-22; ordinary domestic working buckets only',
        'scope': '3.1(a/c/e) candidates, owner-reviewed input components; no statutory utilization',
    },
    'it1961': {
        'title': 'Income Tax Department AY 2026-27 individual guidance',
        'url': 'https://www.incometax.gov.in/iec/foportal/help/individual/return-applicable-1',
        'verified_on': '2026-11-22',
        'effective': 'FY 2025-26 / AY 2026-27',
        'scope': 'Resident individual, new regime, ordinary taxable income up to INR 50 lakh; no special income/deductions',
    },
    'it2025': {
        'title': 'Income-tax Act 2025 as amended by Finance Act 2026, sections 156, 202, 516',
        'url': 'https://www.incometaxindia.gov.in/documents/d/guest/income_tax_act_2025_as_amended_by_fa_act_2026-pdf',
        'verified_on': '2026-11-22',
        'effective': '2026-04-01',
        'scope': 'TY 2026-27 resident individual ordinary income; new regime, rebate/marginal relief and rounding',
    },
    'cess': {
        'title': 'Finance Act 2026, sections 2/3 and First Schedule',
        'url': 'https://www.incometaxindia.gov.in/documents/d/guest/finance-act-2026-pdf-1',
        'verified_on': '2026-11-22',
        'effective': 'AY 2026-27 / TY 2026-27',
        'scope': '4% health/education cess; selected individual scope excludes income above surcharge threshold',
    },
    'round1961': {
        'title': 'Parliament comparison of 288A/288B with section 516',
        'url': 'https://sansad.in/getFile/lsscommittee/Select%20Committee%20of%20Lok%20Sabha%20to%20Examine%20the%20Income-Tax%20Bill%202025/18_Select_Committee_of_Lok_Sabha_to_Examine_the_Income-Tax_Bill_2025_1.pdf?source=loksabhadocs',
        'verified_on': '2026-11-22',
        'effective': 'FY 2025-26 historical rounding / 2025 Act comparison',
        'scope': 'Ignore paise then nearest INR 10; no policy change in comparison',
    },
}
INCOME_RULES = {
    year: {
        'id': f'IN-NEW-IND-{year}-v1',
        'tax_type': 'income',
        'financial_year': f'{year}-{str(year + 1)[-2:]}',
        'assessment_year': '2026-27' if year == 2025 else None,
        'tax_year': None if year == 2025 else '2026-27',
        'effective_start': f'{year}-04-01',
        'effective_end': f'{year + 1}-03-31',
        'taxpayer': 'resident_individual',
        'regime': 'new',
        'status': 'verified_narrow_scope',
        'slabs': [
            ['400000', '0'],
            ['800000', '0.05'],
            ['1200000', '0.10'],
            ['1600000', '0.15'],
            ['2000000', '0.20'],
            ['2400000', '0.25'],
            [None, '0.30'],
        ],
        'rebate_threshold': '1200000',
        'rebate_cap': '60000',
        'cess': '0.04',
        'maximum_income': '5000000',
        'sources': ['it1961', 'cess', 'round1961'] if year == 2025 else ['it2025', 'cess'],
    }
    for year in [2025, 2026]
}

def rule_bundle():
    return deepcopy({'calculation_version': CALCULATION_VERSION, 'references': REFERENCES, 'income_rules': INCOME_RULES, 'b2cl': [{'from': '2017-07-01', 'through': '2024-07-31', 'threshold': '250000'}, {'from': '2024-08-01', 'through': None, 'threshold': '100000'}], 'gst_scope': 'Standard domestic document-date working summaries; no filing schema, utilization, RCM, cess or statutory liability determination.'})

def b2c_bucket(invoice_date, total, interstate):
    if invoice_date < date(2017, 7, 1):
        return 'unresolved'
    threshold = Decimal('100000') if invoice_date >= date(2024, 8, 1) else Decimal('250000')
    return 'B2CL' if interstate and total > threshold else 'B2CS'

def round_ten(value):
    whole = decimal(value).to_integral_value(rounding=ROUND_DOWN)
    return whole.quantize(Decimal('1E1'), rounding=ROUND_HALF_UP)

def estimate_income(period, inputs, business_type):
    year = period.start_date.year
    rules = INCOME_RULES.get(year)
    reasons = []
    if period.kind != 'financial_year' or period.start_date != date(year, 4, 1) or period.end_date != date(year + 1, 3, 31):
        reasons.append('Income tax requires an April-to-March financial year.')
    if rules is None:
        reasons.append('No verified income-tax rule bundle for this financial year.')
    if business_type not in {'individual', 'proprietorship'} or inputs.get('taxpayer') != 'resident_individual':
        reasons.append('This business/taxpayer category requires manual review; individual slabs are not applied.')
    if inputs.get('regime') != 'new':
        reasons.append('Only the verified new-regime ordinary-income scope is implemented.')
    if not inputs.get('resident_confirmed') or not inputs.get('ordinary_only'):
        reasons.append('Explicit residency and ordinary-income/scope confirmation are required.')
    raw = inputs.get('taxable_income')
    if raw is None:
        reasons.append('Enter independently assessed taxable income; accounting totals do not establish it.')
    else:
        income = decimal(raw)
        if income < 0:
            reasons.append('Negative income/loss set-off is unsupported.')
        income = round_ten(income)
        if income > Decimal('5000000'):
            reasons.append('Surcharge/high-income computation is outside the supported scope.')
    if decimal(inputs.get('deductions') or 0) != 0:
        reasons.append('Additional deduction computation is unsupported; obtain independently assessed taxable income.')
    if rules and inputs.get('assessment_year') != ('2026-27' if year == 2025 else 'not_applicable'):
        reasons.append('Assessment-year selection does not match this financial/tax year.')
    if reasons:
        return {'status': 'manual_review_required', 'estimated_tax': None, 'reasons': reasons, 'rule': deepcopy(rules)}
    lower = Decimal(0)
    tax = Decimal(0)
    steps = []
    for (ceiling, rate) in rules['slabs']:
        top = income if ceiling is None else min(income, Decimal(ceiling))
        base = max(top - lower, Decimal(0))
        amount = base * Decimal(rate)
        tax += amount
        steps.append({'from': lower, 'through': ceiling, 'rate': rate, 'income_in_slab': base, 'tax': money(amount)})
        if ceiling is None or income <= Decimal(ceiling):
            break
        lower = Decimal(ceiling)
    rebate = min(tax, Decimal('60000')) if income <= Decimal('1200000') else Decimal(0)
    relief = max(tax - (income - Decimal('1200000')), Decimal(0)) if income > Decimal('1200000') else Decimal(0)
    after = tax - rebate - relief
    cess = money(after * Decimal('0.04'))
    unrounded = money(after + cess)
    return {'status': 'estimated', 'taxable_income_input': raw, 'rounded_taxable_income': income, 'deductions_applied': Decimal(0), 'slabs': steps, 'base_tax': money(tax), 'rebate': money(rebate), 'rebate_marginal_relief': money(relief), 'surcharge': Decimal(0), 'cess': cess, 'before_final_rounding': unrounded, 'estimated_tax': round_ten(unrounded), 'rule': deepcopy(rules), 'limitations': ['Independent taxable-income input, not accounting profit.', 'No special income, other deductions, losses, prepaid credits or surcharge above INR 50 lakh.', 'Estimate only; regime-election eligibility and statutory filing require independent review.']}
