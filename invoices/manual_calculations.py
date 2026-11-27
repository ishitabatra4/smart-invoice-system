"""Central GST arithmetic. Pure calculations/validation; no requests, settings or writes.

Money: two decimals, ROUND_HALF_UP. Discounts apply in the selected rate basis.
Invoice discounts allocate across taxable-classified lines, including taxable at 0%.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, localcontext
from django.core.exceptions import ValidationError

ZERO = Decimal('0.00')
CENT = Decimal('0.01')
LIMIT = Decimal('1000000000000')
DEFAULT_RATES = tuple(Decimal(value) for value in ('0','5','12','18','28','40'))
TREATMENTS = ('taxable','exempt','nil_rated','non_gst')
STATES = {f'{number:02}' for number in (*range(1,39),97)}


def decimal(value):
    if isinstance(value, (float, bool)):
        raise ValidationError('Use decimal financial values, not floating point.')
    try:
        result = Decimal(value)
        if not result.is_finite() or abs(result) >= LIMIT:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError('Enter a finite financial value within the supported limit.') from None


def money(value):
    with localcontext() as context:
        context.prec=40
        result = decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
    if abs(result)>=LIMIT:
        raise ValidationError("Rounded amount exceeds the supported limit.")
    return result if result else ZERO


def discount_amount(base, value=ZERO, kind='fixed'):
    value = decimal(value)
    if kind not in ('fixed','percent') or value < 0:
        raise ValidationError('Choose a fixed or percentage discount with a nonnegative value.')
    if kind == 'percent' and value > 100:
        raise ValidationError('Percentage discounts cannot exceed 100%.')
    if value != money(value):
        raise ValidationError('Discount inputs must use at most two decimal places.')
    amount = money(base * value / 100) if kind == 'percent' else money(value)
    if amount > base:
        raise ValidationError('Discount cannot exceed the eligible line amount.')
    return amount


def line_basis(quantity, rate, discount=ZERO, tax_rate=ZERO, *, pricing_mode='exclusive', discount_type='fixed', tax_treatment='taxable', allowed_rates=DEFAULT_RATES):
    quantity, rate, tax_rate = decimal(quantity), decimal(rate), decimal(tax_rate)
    if quantity <= 0 or quantity >= Decimal("1000000000") or quantity != quantity.quantize(Decimal('0.001')) or rate < 0 or rate != money(rate):
        raise ValidationError('Use a positive quantity (up to three decimals) and a nonnegative two-decimal rate.')
    if pricing_mode not in ('inclusive','exclusive') or tax_treatment not in TREATMENTS:
        raise ValidationError('Choose a supported pricing mode and tax treatment.')
    if tax_rate not in tuple(decimal(value) for value in allowed_rates):
        raise ValidationError('Select a configured GST rate. No HSN/rate classification is inferred.')
    if tax_treatment != 'taxable' and tax_rate != 0:
        raise ValidationError('Exempt, nil-rated and non-GST lines must have a zero GST rate.')
    with localcontext() as context:
        context.prec = 40
        gross = money(quantity * rate)
    reduction = discount_amount(gross, discount, discount_type)
    return {'gross_amount':gross, 'discount':reduction, 'net_basis':gross-reduction,
            'tax_rate':tax_rate, 'tax_treatment':tax_treatment}


def base_amounts(net_basis, tax_rate, pricing_mode='exclusive'):
    net_basis, tax_rate = money(net_basis), decimal(tax_rate)
    if net_basis < 0 or not 0 <= tax_rate <= 100 or pricing_mode not in ('exclusive','inclusive'):
        raise ValidationError('Invalid pricing basis or GST percentage.')
    taxable = money(net_basis / (1+tax_rate/100)) if pricing_mode == 'inclusive' else net_basis
    tax = net_basis-taxable if pricing_mode == 'inclusive' else money(taxable*tax_rate/100)
    return {'taxable_amount':taxable, 'tax_amount':tax, 'total_amount':taxable+tax}


def validate_supply(supplier_state, place_of_supply, supply_category='standard'):
    if supply_category != 'standard':
        raise ValidationError('This supply category requires manual GST review; automatic tax calculation is not supported.')
    if supplier_state not in STATES or place_of_supply not in STATES:
        raise ValidationError('Enter explicit supplier state and place of supply; neither can be inferred.')
    if '97' in (supplier_state, place_of_supply):
        raise ValidationError('Other-territory supplies require manual GST review.')
    intrastate = supplier_state == place_of_supply
    if intrastate and supplier_state in {'04','25','26','31','35','38'}:
        raise ValidationError('This intra-territory supply may require UTGST. Manual review is required; it cannot be labelled SGST.')
    return intrastate


def split_tax(tax, supplier_state, place_of_supply, supply_category='standard'):
    tax = money(tax)
    if tax < 0:
        raise ValidationError('Tax cannot be negative.')
    intra = validate_supply(supplier_state, place_of_supply, supply_category)
    cgst = money(tax/2) if intra else ZERO
    return {'cgst_amount':cgst, 'sgst_amount':tax-cgst if intra else ZERO, 'igst_amount':ZERO if intra else tax}


def allocate_discount(bases, reduction):
    """Allocate whole paise by largest remainder; input order breaks ties."""
    total = sum(bases, ZERO)
    if reduction < 0 or reduction > total:
        raise ValidationError('Invoice discount exceeds eligible taxable lines.')
    if not total or not reduction:
        return [ZERO for _ in bases]
    with localcontext() as context:
        context.prec = 50
        exact = [reduction*base/total for base in bases]
        allocated = [value.quantize(CENT, rounding=ROUND_DOWN) for value in exact]
        count = int((reduction-sum(allocated,ZERO))/CENT)
        order = sorted(range(len(bases)), key=lambda index:(-(exact[index]-allocated[index]),index))
        for index in order[:count]:
            allocated[index] += CENT
    return allocated


def calculate_invoice(lines, supplier_state, place_of_supply, *, pricing_mode='exclusive', invoice_discount_type='fixed', invoice_discount=ZERO, adjustment=ZERO, supply_category='standard', allowed_rates=DEFAULT_RATES):
    validate_supply(supplier_state, place_of_supply, supply_category)
    if not 1 <= len(lines) <= 100:
        raise ValidationError('Add between 1 and 100 invoice lines.')
    prepared = [line_basis(line['quantity'],line['rate'],line.get('discount',ZERO),line['tax_rate'],
        pricing_mode=pricing_mode,discount_type=line.get('discount_type','fixed'),
        tax_treatment=line.get('tax_treatment','taxable'),allowed_rates=allowed_rates) for line in lines]
    eligible = [line['net_basis'] if line['tax_treatment']=='taxable' else ZERO for line in prepared]
    reduction = discount_amount(sum(eligible,ZERO),invoice_discount,invoice_discount_type)
    allocated = allocate_discount(eligible,reduction)
    output = []
    breakdown = {name:ZERO for name in TREATMENTS}
    for line, share in zip(prepared,allocated):
        amounts = base_amounts(line['net_basis']-share,line['tax_rate'],pricing_mode)
        components = split_tax(amounts['tax_amount'],supplier_state,place_of_supply,supply_category)
        breakdown[line['tax_treatment']] += amounts['taxable_amount']
        output.append({**line,**amounts,**components,'invoice_discount_allocated':share})
    adjustment = decimal(adjustment)
    if abs(adjustment)>1 or adjustment != money(adjustment):
        raise ValidationError('Round-off must be at most one rupee and use two decimals.')
    result = {name:sum((line[name] for line in output),ZERO) for name in ('taxable_amount','tax_amount','total_amount','cgst_amount','sgst_amount','igst_amount')}
    for name in result:
        result[name]=money(result[name])
    result['total_amount'] = money(result['total_amount']+adjustment)
    if result['total_amount'] <= 0:
        raise ValidationError('Invoice total must be positive.')
    return {**result, 'other_charges':money(adjustment),'lines':output,'treatment_breakdown':breakdown,
        'subtotal':sum((line['gross_amount'] for line in prepared),ZERO), 'invoice_discount_amount':reduction,
        'total_discount_amount':sum((line['discount'] for line in prepared),ZERO)+reduction}


def validate_stored_totals(taxable, cgst, sgst, igst, adjustment, total):
    """Read-only source validation: never infer pricing, rates, discounts or supply."""
    issues=[]
    values=[decimal(value) for value in (taxable,cgst,sgst,igst,adjustment,total)]
    taxable,cgst,sgst,igst,adjustment,total=values
    if any(value < 0 for value in (taxable,cgst,sgst,igst,total)):
        issues.append('Financial values cannot be negative.')
    if igst and (cgst or sgst):
        issues.append('CGST/SGST and IGST cannot both be charged on the same standard supply; review the source.')
    if taxable+cgst+sgst+igst+adjustment != total:
        issues.append('Invoice total does not reconcile with saved components.')
    return issues

