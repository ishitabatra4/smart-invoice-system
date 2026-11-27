from django.urls import path

from . import views

urlpatterns = [
    path("reconciliation/<int:run_id>/source/", views.reconciliation_source_view, name="reconciliation_source"),
    path("reconciliation/", views.reconciliation_list_view, name="reconciliation_list"),
    path("reconciliation/upload/", views.reconciliation_upload_view, name="reconciliation_upload"),
    path("reconciliation/<int:run_id>/", views.reconciliation_run_view, name="reconciliation_run"),
    path("reconciliation/<int:run_id>/export/", views.reconciliation_export_view, name="reconciliation_export"),
    path("reconciliation/<int:run_id>/records/<int:record_id>/match/", views.reconciliation_match_view, name="reconciliation_match"),
    path("reconciliation/<int:run_id>/records/<int:record_id>/unmatch/", views.reconciliation_unmatch_view, name="reconciliation_unmatch"),
]

from . import preparation_views as preparation
urlpatterns += [
    path('prepare/', preparation.dashboard, name='tax_preparation'),
    path('prepare/period/<int:pk>/', preparation.period_detail, name='tax_period'),
    path('prepare/period/<int:pk>/<str:kind>/', preparation.preview, name='tax_preview'),
    path('prepare/paper/<int:pk>/', preparation.paper_detail, name='tax_paper'),
    path('prepare/paper/<int:pk>/status/', preparation.lifecycle, name='tax_paper_status'),
    path('prepare/paper/<int:pk>/export/<str:fmt>/', preparation.export, name='tax_paper_export'),
]
