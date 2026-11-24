from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

admin.site.site_header = "Thapar Smart Parking - Admin"
admin.site.site_title = "Thapar Smart Parking"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    path("", include("web.urls")),
    # serve uploaded/annotated images on the LAN (local deployment)
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
