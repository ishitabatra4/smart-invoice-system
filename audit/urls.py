from django.urls import path

from . import views

urlpatterns = [
    path("", views.audit_dashboard_view, name="audit_log"),
    path("events/", views.audit_log_view, name="audit_events"),
    path("events/<int:event_id>/", views.audit_detail_view, name="audit_event_detail"),
    path("activity/", views.audit_activity_view, name="audit_activity"),
    path("security/", views.audit_security_view, name="audit_security"),
    path("export/<str:export_format>/", views.audit_export_view, name="audit_export"),
]

from . import security_views as monitor
urlpatterns += [
    path('monitor/',monitor.dashboard,name='security_monitor'),
    path('monitor/events/<int:event_id>/',monitor.event_detail,name='security_event_detail'),
    path('monitor/alerts/<int:alert_id>/',monitor.alert_detail,name='security_alert_detail'),
    path('monitor/alerts/<int:alert_id>/status/',monitor.alert_transition,name='security_alert_transition'),
    path('monitor/rules/',monitor.rules,name='security_rules'),
    path('monitor/sessions/',monitor.sessions,name='security_sessions'),
    path('monitor/export/<str:fmt>/',monitor.export,name='security_export'),
]
