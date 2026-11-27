from django.urls import path

from . import advanced_views
from . import views
from . import report_views

urlpatterns = [
    path("receipts/upload/",advanced_views.receipt_upload,name="expense_receipt_upload"),
    path("purchases/link/",advanced_views.purchase_link,name="expense_purchase_link"),
    path("receipts/history/<int:revision_id>/",advanced_views.receipt_revision,name="expense_receipt_revision"),
    path("<int:expense_id>/itc/",advanced_views.itc_review,name="expense_itc_review"),
    path("calculate/", views.expense_calculation_preview, name="expense_calculate"),
    path("", views.expense_dashboard_view, name="expense_dashboard"),
    path("list/", views.expense_list_view, name="expense_list"),
    path("add/", views.expense_create_view, name="expense_add"),
    path("reports/", report_views.expense_reports_view, name="expense_reports"),
    path("reports/export/<str:export_format>/", report_views.expense_report_export_view, name="expense_report_export"),
    path("recurring/", views.recurring_expense_list_view, name="recurring_expense_list"),
    path("recurring/add/", views.recurring_expense_create_view, name="recurring_expense_add"),
    path("recurring/generate-due/", views.recurring_generate_view, name="recurring_expense_generate"),
    path("recurring/<int:recurring_id>/edit/", views.recurring_expense_edit_view, name="recurring_expense_edit"),
    path("recurring/<int:recurring_id>/<str:action>/", views.recurring_expense_action_view, name="recurring_expense_action"),
    path("categories/", views.expense_categories_view, name="expense_categories"),
    path("categories/<int:category_id>/edit/", views.expense_category_edit_view, name="expense_category_edit"),
    path("categories/<int:category_id>/deactivate/", views.expense_category_deactivate_view, name="expense_category_deactivate"),
    path("<int:expense_id>/", views.expense_detail_view, name="expense_detail"),
    path("<int:expense_id>/attachment/", views.expense_attachment_view, name="expense_attachment"),
    path("<int:expense_id>/edit/", views.expense_edit_view, name="expense_edit"),
    path("<int:expense_id>/payment/", views.expense_payment_view, name="expense_payment"),
    path("<int:expense_id>/cancel/", views.expense_cancel_view, name="expense_cancel"),
    path("<int:expense_id>/delete/", views.expense_cancel_view, name="expense_delete"),
]
