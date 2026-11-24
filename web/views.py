import io
import socket
from functools import wraps

import qrcode
import qrcode.image.svg
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.utils.safestring import mark_safe


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def server_url(request):
    host = request.get_host()
    port = host.split(":")[1] if ":" in host else "8000"
    return f"http://{lan_ip()}:{port}"


def qr_svg(data):
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=1)
    buf = io.BytesIO()
    img.save(buf)
    return mark_safe(buf.getvalue().decode().split("?>", 1)[-1])


def staff_required(admin_only=False):
    def deco(view):
        @wraps(view)
        def wrapper(request, *a, **kw):
            u = request.user
            if not u.is_authenticated:
                return redirect(f"/portal/login/?next={request.path}")
            if not u.is_staff_role or (admin_only and not u.is_admin):
                return render(request, "web/denied.html", status=403)
            return view(request, *a, **kw)
        return wrapper
    return deco


NAV = [
    ("overview", "Overview", "/portal/", "grid", False),
    ("gate", "Main Gate Live", "/portal/gate/", "gate", False),
    ("occupancy", "Parking & Slots", "/portal/occupancy/", "parking", False),
    ("vehicles", "Vehicle Registry", "/portal/vehicles/", "car", False),
    ("passes", "Passes & Bookings", "/portal/passes/", "ticket", False),
    ("flags", "Flags & Alerts", "/portal/flags/", "flag", False),
    ("map", "Campus Map", "/portal/map/", "map", False),
    ("cameras", "Cameras", "/portal/cameras/", "camera", True),
    ("users", "Users", "/portal/users/", "users", True),
    ("audit", "Audit Log", "/portal/audit/", "shield", True),
]


def page(name, template, admin_only=False):
    @staff_required(admin_only)
    def view(request):
        nav = [n for n in NAV if not n[4] or request.user.is_admin]
        url = server_url(request)
        return render(request, template, {"active": name, "nav": nav, "server_url": url,
                                          "server_qr": qr_svg(url) if name == "overview" else ""})
    return view


def login_view(request):
    error = ""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        user = authenticate(request, email=email, password=request.POST.get("password", ""))
        if user and user.is_staff_role and user.is_active:
            login(request, user)
            return redirect(request.GET.get("next") or ("/portal/gate/" if user.is_guard else "/portal/"))
        error = "Invalid credentials, or this account is not an admin/guard account."
    return render(request, "web/login.html", {"error": error})


def logout_view(request):
    logout(request)
    return redirect("/portal/login/")


def root(request):
    return redirect("/portal/")
