from django.conf import settings
from django.contrib.auth import authenticate
from django.core.mail import send_mail
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from core.serializers import UserSerializer
from .models import EmailOTP, User

CFG = settings.PARKING


class AuthThrottle(AnonRateThrottle):
    rate = "30/min"


def tokens_for(user):
    r = RefreshToken.for_user(user)
    return {"access": str(r.access_token), "refresh": str(r), "user": UserSerializer(user).data}


def _domain_ok(email):
    return email.split("@")[-1].lower() in CFG["ALLOWED_DOMAINS"]


def _send_otp(user):
    otp = EmailOTP.issue(user)
    send_mail("Your Thapar Parking verification code",
              f"Hi {user.display_name},\n\nYour verification code is {otp.code}. It expires in "
              f"{CFG['OTP_TTL_MIN']} minutes.\n\nThapar Smart Parking", None, [user.email], fail_silently=True)
    print(f"\n========== OTP for {user.email}: {otp.code} ==========\n", flush=True)
    return otp


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
@transaction.atomic
def register(request):
    d = request.data
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    role = d.get("role") or "student"
    if role not in ("faculty", "student", "vendor", "visitor"):
        return Response({"detail": "Invalid role."}, status=400)
    if "@" not in email or len(password) < 6:
        return Response({"detail": "Valid email and a password of at least 6 characters are required."}, status=400)
    if role in ("faculty", "student") and not _domain_ok(email):
        return Response({"detail": f"Faculty and students must sign up with a Thapar email "
                                   f"(@{CFG['ALLOWED_DOMAINS'][0]})."}, status=400)
    if role in ("vendor", "visitor") and not (d.get("phone") or "").strip():
        return Response({"detail": "Phone number is required for visitors and vendors."}, status=400)
    existing = User.objects.filter(email=email).first()
    if existing and existing.is_email_verified:
        return Response({"detail": "An account with this email already exists. Please sign in."}, status=400)
    user = existing or User(email=email)
    user.set_password(password)
    user.full_name = (d.get("full_name") or "").strip()[:120]
    user.phone = (d.get("phone") or "").strip()[:20]
    user.role = role
    user.department = (d.get("department") or "")[:120]
    user.roll_or_emp_no = (d.get("roll_or_emp_no") or "")[:40]
    user.organisation = (d.get("organisation") or "")[:120]
    user.role_verified = role != "faculty"   # faculty need admin confirmation for booking privileges
    user.is_email_verified = False
    user.save()
    otp = _send_otp(user)
    resp = {"detail": f"Verification code sent to {email}.", "email": email}
    if CFG["SHOW_OTP_IN_RESPONSE"]:
        resp["dev_otp"] = otp.code
    return Response(resp, status=201)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def verify_otp(request):
    email = (request.data.get("email") or "").strip().lower()
    code = (request.data.get("code") or "").strip()
    user = User.objects.filter(email=email).first()
    if not user:
        return Response({"detail": "Account not found."}, status=404)
    otp = user.otps.filter(used=False).order_by("-created_at").first()
    if not otp or not otp.is_valid(CFG["OTP_TTL_MIN"]):
        return Response({"detail": "Code expired. Request a new one."}, status=400)
    if otp.code != code:
        otp.attempts += 1
        otp.save()
        return Response({"detail": "Incorrect code."}, status=400)
    otp.used = True
    otp.save()
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])
    if user.role == "faculty" and not user.role_verified:
        from core.services import notify_role
        notify_role(["admin"], "Faculty verification needed", f"{user.display_name} ({user.email}) registered as faculty.",
                    "approval", {"user": user.id})
    return Response(tokens_for(user))


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def resend_otp(request):
    email = (request.data.get("email") or "").strip().lower()
    user = User.objects.filter(email=email, is_email_verified=False).first()
    if not user:
        return Response({"detail": "Nothing to verify for this email."}, status=400)
    otp = _send_otp(user)
    resp = {"detail": "A new code was sent."}
    if CFG["SHOW_OTP_IN_RESPONSE"]:
        resp["dev_otp"] = otp.code
    return Response(resp)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def login(request):
    email = (request.data.get("email") or "").strip().lower()
    password = request.data.get("password") or ""
    user = authenticate(request, email=email, password=password)
    if not user:
        return Response({"detail": "Incorrect email or password."}, status=401)
    if not user.is_active:
        return Response({"detail": "Account disabled. Contact the parking office."}, status=403)
    if user.role in ("admin", "guard", "faculty", "student") and not _domain_ok(email) and not user.is_superuser:
        return Response({"detail": "Use your Thapar email account."}, status=403)
    if not user.is_email_verified:
        otp = _send_otp(user)
        resp = {"detail": "Please verify your email first.", "needs_verification": True, "email": email}
        if CFG["SHOW_OTP_IN_RESPONSE"]:
            resp["dev_otp"] = otp.code
        return Response(resp, status=403)
    return Response(tokens_for(user))


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    if request.method == "PATCH":
        s = UserSerializer(request.user, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        return Response(s.data)
    return Response(UserSerializer(request.user).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    old, new = request.data.get("old_password", ""), request.data.get("new_password", "")
    if not request.user.check_password(old):
        return Response({"detail": "Current password is incorrect."}, status=400)
    if len(new) < 6:
        return Response({"detail": "New password must be at least 6 characters."}, status=400)
    request.user.set_password(new)
    request.user.save()
    return Response({"detail": "Password updated."}, status=status.HTTP_200_OK)
