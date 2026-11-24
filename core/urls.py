from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from accounts import api as A
from . import api as C

urlpatterns = [
    path("health/", C.health),
    # auth
    path("auth/register/", A.register),
    path("auth/verify-otp/", A.verify_otp),
    path("auth/resend-otp/", A.resend_otp),
    path("auth/login/", A.login),
    path("auth/refresh/", TokenRefreshView.as_view()),
    path("auth/me/", A.me),
    path("auth/change-password/", A.change_password),
    # admin / dashboard
    path("dashboard/stats/", C.dashboard_stats),
    path("users/", C.users),
    path("users/<int:pk>/", C.user_detail),
    path("audit/", C.audit_log),
    # vehicles
    path("vehicles/", C.vehicles),
    path("vehicles/lookup/", C.vehicle_lookup),
    path("vehicles/<int:pk>/", C.vehicle_detail),
    path("vehicles/<int:pk>/action/", C.vehicle_action),
    path("vehicles/<int:pk>/reveal-contact/", C.reveal_contact),
    path("vision/plate/", C.vision_plate),
    # lots
    path("lots/", C.lots),
    path("lots/<int:pk>/", C.lot_detail),
    path("lots/<int:pk>/slots/", C.lot_slots),
    path("lots/<int:pk>/detect/", C.lot_detect),
    path("lots/<int:pk>/manual/", C.lot_manual),
    # bookings & passes
    path("bookings/", C.bookings),
    path("bookings/<int:pk>/", C.booking_detail),
    path("bookings/<int:pk>/action/", C.booking_action),
    path("passes/verify/", C.pass_verify),
    # gate
    path("gate/scan/", C.gate_scan),
    path("gate/events/", C.gate_events),
    path("gate/events/<int:pk>/review/", C.gate_review),
    # flags & alerts
    path("flags/", C.flags),
    path("flags/<int:pk>/action/", C.flag_action),
    path("notifications/", C.notifications),
    path("notifications/read/", C.notifications_read),
    # cameras
    path("cameras/", C.cameras),
    path("cameras/<int:pk>/", C.camera_detail),
    path("cameras/ingest/", C.camera_ingest),
]
