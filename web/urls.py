from django.urls import path

from . import views as V

urlpatterns = [
    path("", V.root),
    path("portal/login/", V.login_view),
    path("portal/logout/", V.logout_view),
    path("portal/", V.page("overview", "web/overview.html")),
    path("portal/gate/", V.page("gate", "web/gate.html")),
    path("portal/occupancy/", V.page("occupancy", "web/occupancy.html")),
    path("portal/vehicles/", V.page("vehicles", "web/vehicles.html")),
    path("portal/passes/", V.page("passes", "web/passes.html")),
    path("portal/flags/", V.page("flags", "web/flags.html")),
    path("portal/map/", V.page("map", "web/map.html")),
    path("portal/cameras/", V.page("cameras", "web/cameras.html", admin_only=True)),
    path("portal/users/", V.page("users", "web/users.html", admin_only=True)),
    path("portal/audit/", V.page("audit", "web/audit.html", admin_only=True)),
]
