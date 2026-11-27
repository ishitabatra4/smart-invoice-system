from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from .manual_calculations import calculate_invoice, money, validate_stored_totals

D=Decimal


class GSTCalculatorTests(SimpleTestCase):
    def line(self, **updates):
        result={'quantity':D('1'),'rate':D('1000'),'tax_rate':D('18'),'discount':D('0'),'discount_type':'fixed','tax_treatment':'taxable'}
        result.update(updates)
        return result

    def calculate(self, lines=None, supplier='27', place='27', **options):
        return calculate_invoice(lines or [self.line()],supplier,place,**options)

    def test_exclusive_and_inclusive_examples_reconcile(self):
        exclusive=self.calculate()
        inclusive=self.calculate([self.line(rate=D('1180'))],pricing_mode='inclusive')
        for result in (exclusive,inclusive):
            self.assertEqual(result['taxable_amount'],D('1000'))
            self.assertEqual(result['tax_amount'],D('180'))
            self.assertEqual(result['total_amount'],D('1180'))
            self.assertEqual(result['cgst_amount'],D('90'))
            self.assertEqual(result['sgst_amount'],D('90'))
            self.assertEqual(result['igst_amount'],D('0'))

    def test_interstate_uses_place_of_supply_not_buyer_state(self):
        result=self.calculate(place='29')
        self.assertEqual(result['igst_amount'],D('180'))
        self.assertEqual(result['cgst_amount']+result['sgst_amount'],D('0'))

    def test_fixed_and_percentage_line_discount(self):
        fixed=self.calculate([self.line(discount=D('100'))])
        percent=self.calculate([self.line(discount=D('10'),discount_type='percent')])
        self.assertEqual(fixed['total_amount'],D('1062'))
        self.assertEqual(fixed,percent)
        self.assertEqual(percent['total_discount_amount'],D('100'))

    def test_inclusive_discount_applies_to_inclusive_basis(self):
        result=self.calculate([self.line(rate=D('1180'),discount=D('10'),discount_type='percent')],pricing_mode='inclusive')
        self.assertEqual(result['taxable_amount'],D('900'))
        self.assertEqual(result['tax_amount'],D('162'))
        self.assertEqual(result['total_amount'],D('1062'))
        self.assertEqual(result['total_discount_amount'],D('118'))

    def test_invoice_discount_allocated_before_mixed_rate_taxes(self):
        result=self.calculate([self.line(tax_rate=D('5')),self.line(tax_rate=D('18'))],invoice_discount=D('200'))
        self.assertEqual([row['invoice_discount_allocated'] for row in result['lines']],[D('100'),D('100')])
        self.assertEqual(result['taxable_amount'],D('1800'))
        self.assertEqual(result['tax_amount'],D('207'))
        self.assertEqual(result['total_amount'],D('2007'))

    def test_invoice_percentage_discount_after_line_discount(self):
        result=self.calculate([self.line(discount=D('100'))],invoice_discount_type='percent',invoice_discount=D('10'))
        self.assertEqual(result['invoice_discount_amount'],D('90'))
        self.assertEqual(result['total_discount_amount'],D('190'))
        self.assertEqual(result['taxable_amount'],D('810'))
        self.assertEqual(result['total_amount'],D('955.80'))

    def test_inclusive_mixed_rate_allocation_preserves_net_price(self):
        result=self.calculate([self.line(rate=D('1180')),self.line(rate=D('1050'),tax_rate=D('5'))],pricing_mode='inclusive',invoice_discount=D('223'))
        self.assertEqual(result['total_amount'],D('2007'))
        self.assertEqual(sum((row['invoice_discount_allocated'] for row in result['lines']),D('0')),D('223'))
        self.assertEqual(result['taxable_amount']+result['tax_amount'],D('2007'))

    def test_largest_remainder_is_deterministic_and_conserves_discount(self):
        result=self.calculate([self.line(rate=D('1'),tax_rate=D('0')) for _ in range(3)],invoice_discount=D('0.01'))
        self.assertEqual([row['invoice_discount_allocated'] for row in result['lines']],[D('.01'),D('0'),D('0')])
        self.assertEqual(result['total_amount'],D('2.99'))

    def test_tax_treatments_remain_distinct_and_discount_eligibility_is_explicit(self):
        result=self.calculate([self.line(tax_rate=D('0'),tax_treatment=name) for name in ('taxable','exempt','nil_rated','non_gst')],invoice_discount=D('100'))
        self.assertEqual(result['treatment_breakdown'],{'taxable':D('900'),'exempt':D('1000'),'nil_rated':D('1000'),'non_gst':D('1000')})
        self.assertEqual(result['tax_amount'],D('0'))
        self.assertEqual([row['invoice_discount_allocated'] for row in result['lines']],[D('100'),D('0'),D('0'),D('0')])

    def test_fractional_quantity_paise_rounding_and_round_off(self):
        result=self.calculate([self.line(quantity=D('1.125'),rate=D('0.10'),tax_rate=D('5'))],adjustment=D('0.01'))
        self.assertEqual(result['lines'][0]['gross_amount'],D('.11'))
        self.assertEqual(result['total_amount'],D('.13'))
        self.assertEqual(money(D('-.001')),D('0.00'))
        self.assertFalse(money(D('-.001')).is_signed())
        self.assertEqual(money(D('1.005')),D('1.01'))

    def test_large_values_remain_decimal_and_within_model_limits(self):
        result=self.calculate([self.line(rate=D('999999999999.99'),tax_rate=D('0'))])
        self.assertIsInstance(result['total_amount'],Decimal)
        self.assertEqual(result['total_amount'],D('999999999999.99'))
        with self.assertRaises(ValidationError):
            self.calculate([self.line(rate=D('999999999999.99'))])

    def test_negative_nonfinite_float_and_invalid_precision_are_rejected(self):
        for changes in ({'quantity':D('0')},{'quantity':D('-1')},{'rate':D('-1')},{'rate':D('NaN')},{'rate':1.5},{'quantity':D('1.0001')},{'rate':D('1.001')},{'tax_rate':D('101')}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):
                self.calculate([self.line(**changes)])

    def test_excessive_and_invalid_discounts_rejected(self):
        for changes in ({'discount':D('1001')},{'discount':D('101'),'discount_type':'percent'},{'discount':D('-1')},{'discount_type':'unknown'},{'discount':D('1.001'),'discount_type':'percent'}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):
                self.calculate([self.line(**changes)])
        for options in ({'invoice_discount':D('1001')},{'invoice_discount':D('101'),'invoice_discount_type':'percent'},{'adjustment':D('1.01')},{'adjustment':D('.001')}):
            with self.subTest(options=options),self.assertRaises(ValidationError):
                self.calculate(**options)

    def test_non_taxable_treatments_require_zero_rate(self):
        for treatment in ('exempt','nil_rated','non_gst'):
            with self.subTest(treatment=treatment),self.assertRaises(ValidationError):
                self.calculate([self.line(tax_treatment=treatment)])

    def test_unknown_rates_rejected_and_explicit_extension_supported(self):
        with self.assertRaises(ValidationError):
            self.calculate([self.line(tax_rate=D('7'))])
        self.assertEqual(self.calculate([self.line(tax_rate=D('7'))],allowed_rates=(D('0'),D('7')))['tax_amount'],D('70'))

    def test_missing_supply_special_categories_and_utgst_require_review(self):
        for supplier,place,category in (('', '27','standard'),('27','','standard'),('99','27','standard'),('27','27','sez'),('27','27','export'),('27','27','zero_rated'),('27','27','reverse_charge'),('04','04','standard'),('97','27','standard')):
            with self.subTest(supplier=supplier,place=place,category=category),self.assertRaises(ValidationError):
                self.calculate(supplier=supplier,place=place,supply_category=category)

    def test_saved_components_validation_does_not_recalculate_inputs(self):
        self.assertEqual(validate_stored_totals(D('100'),D('9'),D('9'),D('0'),D('0'),D('118')),[])
        self.assertTrue(validate_stored_totals(D('100'),D('9'),D('9'),D('18'),D('0'),D('136')))
        self.assertTrue(validate_stored_totals(D('100'),D('9'),D('9'),D('0'),D('0'),D('119')))

    def test_components_and_headers_reconcile_at_paise_boundaries(self):
        result=self.calculate([self.line(rate=D('.10'),tax_rate=D('5')) for _ in range(3)])
        for row in result['lines']:
            self.assertEqual(row['cgst_amount']+row['sgst_amount']+row['igst_amount'],row['tax_amount'])
        self.assertEqual(result['cgst_amount'],D('.03'))
        self.assertEqual(result['sgst_amount'],D('0'))
        self.assertEqual(result['taxable_amount']+result['tax_amount'],result['total_amount'])

    def test_one_paise_invoice_stays_positive_without_spurious_tax(self):
        for mode in ('exclusive','inclusive'):
            result=self.calculate([self.line(rate=D('.01'),tax_rate=D('18'))],pricing_mode=mode)
            self.assertEqual(result['total_amount'],D('.01'))
            self.assertEqual(result['tax_amount'],D('0.00'))
