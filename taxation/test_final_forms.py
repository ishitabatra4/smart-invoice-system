"""Income controls are separated from GST declarations at render and clean time."""
from datetime import date
from django.test import TestCase
from django.contrib.auth import get_user_model
from accounts.models import BusinessProfile
from .models import TaxPeriod
from .preparation_forms import PreparationForm

class IncomePreparationFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user=get_user_model().objects.create_user('final-income-ui',email='income@example.test',password='synthetic-only')
        business=BusinessProfile.objects.create(user=user,business_name='Fictional Income Test')
        cls.period=TaxPeriod.objects.create(business=business,created_by=user,kind='financial_year',start_date=date(2025,4,1),end_date=date(2026,3,31))

    def test_income_summary_needs_no_gst_or_income_tax_fields(self):
        form=PreparationForm({},period=self.period,kind='income_summary');self.assertTrue(form.is_valid(),form.errors)
        for name in ('ordinary_domestic','aato_band','import_runs','taxable_income','taxpayer'):self.assertNotIn(name,form.fields)
        self.assertEqual(form.inputs(),{'import_runs':[],'registration':{}})

    def test_income_tax_keeps_income_inputs_and_ignores_irrelevant_gst_payload(self):
        payload={'taxpayer':'resident_individual','regime':'new','assessment_year':'2026-27','taxable_income':'1600000','deductions':'0','resident_confirmed':'on','ordinary_only':'on','aato_band':'bogus','import_runs':['999999'],'ordinary_domestic':'on'}
        form=PreparationForm(payload,period=self.period,kind='income_tax');self.assertTrue(form.is_valid(),form.errors)
        values=form.inputs();self.assertEqual(values['import_runs'],[]);self.assertEqual(values['taxpayer'],'resident_individual');self.assertNotIn('aato_band',values);self.assertNotIn('ordinary_domestic',values)
        self.assertIn('taxable_income',form.fields);self.assertIn('confirmed',form.fields)
        bad=PreparationForm({**payload,'taxable_income':'NaN'},period=self.period,kind='income_tax');self.assertFalse(bad.is_valid())

    def test_gst_forms_retain_required_band_and_scoped_comparison_fields(self):
        for kind in ('gstr1','gstr3b','itc','liability'):
            form=PreparationForm({},period=self.period,kind=kind)
            for name in ('ordinary_domestic','aato_band','import_runs'):self.assertIn(name,form.fields)
            self.assertFalse(form.is_valid());self.assertIn('aato_band',form.errors)
            self.assertNotIn('taxable_income',form.fields)

    def test_previous_gst_inputs_not_reintroduced_into_income_form(self):
        form=PreparationForm(period=self.period,kind='income_summary',previous_inputs={'aato_band':'above_5cr','ordinary_domestic':True,'import_runs':[999]})
        self.assertNotIn('aato_band',form.initial);self.assertNotIn('import_runs',form.initial)
