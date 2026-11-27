from datetime import date,timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase,override_settings
from django.urls import reverse
from accounts.models import BusinessProfile
from .security_views import Filters


class SecurityDateRangeTests(TestCase):
    def setUp(self):
        user=get_user_model().objects.create_user('date-filter-owner');BusinessProfile.objects.create(user=user,business_name='Synthetic date checks');self.client.force_login(user)
    def test_year_one_and_unsupported_bounds_are_normal_errors(self):
        for values in [{'end_date':'0001-01-01'},{'start_date':'0001-01-01','end_date':'0001-01-02'},{'end_date':'9999-12-31'},{'start_date':'1899-12-31','end_date':'1900-01-01'},{'start_date':'wrong'}]:
            self.assertFalse(Filters(values).is_valid())
            self.assertEqual(self.client.get(reverse('security_monitor'),values).status_code,200)
            for fmt in ['csv','xlsx','pdf']:self.assertEqual(self.client.get(reverse('security_export',args=[fmt]),values).status_code,400)
    def test_minimum_clamps_default_start_and_maximum_converts_safely(self):
        minimum=Filters({'end_date':'1900-01-01'});self.assertTrue(minimum.is_valid());self.assertEqual(minimum.cleaned_data['start_date'],date(1900,1,1))
        for values in [{'start_date':'1900-01-01','end_date':'1900-01-02'},{'start_date':'9998-12-30','end_date':'9998-12-31'}]:
            self.assertEqual(self.client.get(reverse('security_monitor'),values).status_code,200)
            self.assertEqual(self.client.get(reverse('security_export',args=['csv']),values).status_code,200)
    def test_leap_year_interval_and_reverse_boundaries(self):
        self.assertTrue(Filters({'start_date':'2024-02-29','end_date':'2024-03-01'}).is_valid())
        self.assertFalse(Filters({'start_date':'2025-02-29','end_date':'2025-03-01'}).is_valid())
        start=date(2024,1,1)
        self.assertTrue(Filters({'start_date':start,'end_date':start+timedelta(days=365)}).is_valid())
        self.assertFalse(Filters({'start_date':start,'end_date':start+timedelta(days=366)}).is_valid())
        self.assertFalse(Filters({'start_date':'2026-11-02','end_date':'2026-11-01'}).is_valid())
    @override_settings(TIME_ZONE='Pacific/Kiritimati')
    def test_positive_timezone_extreme_is_safe(self):
        self.assertEqual(self.client.get(reverse('security_monitor'),{'start_date':'1900-01-01','end_date':'1900-01-02'}).status_code,200)
    @override_settings(TIME_ZONE='America/Los_Angeles')
    def test_negative_timezone_extreme_is_safe(self):
        self.assertEqual(self.client.get(reverse('security_monitor'),{'start_date':'9998-12-30','end_date':'9998-12-31'}).status_code,200)
