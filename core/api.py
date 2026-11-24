import base64
from datetime import datetime, timedelta

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from vision import plates as P
from . import services as S
from .models import AuditLog, Booking, Camera, Flag, GateEvent, Notification, ParkingLot, Vehicle
from .serializers import (AdminUserSerializer, AuditSerializer, BookingSerializer, CameraSerializer, FlagSerializer,
                          GateEventSerializer, LotSerializer, NotificationSerializer, VehicleSerializer)

PARSERS = [MultiPartParser, FormParser, JSONParser]


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_admin)


class IsStaffRole(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff_role)


def err(e: S.ServiceError):
    return Response({"detail": e.message}, status=e.code)


def get_image_bytes(request):
    ct = (request.content_type or "").lower()
    if ct.startswith("image/") or ct == "application/octet-stream":
        return request._request.body
    f = request.FILES.get("image")
    if f:
        return f.read()
    b64 = request.data.get("image_base64")
    if b64:
        if "," in b64[:80]:
            b64 = b64.split(",", 1)[1]
        return base64.b64decode(b64)
    return None


def _dt(v):
    if not v:
        return None
    d = parse_datetime(v)
    if d and timezone.is_naive(d):
        d = timezone.make_aware(d)
    return d


# ------------------------------------------------------------------ misc
@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"ok": True, "app": "Thapar Smart Parking", "time": timezone.now().isoformat()})


@api_view(["GET"])
@permission_classes([IsStaffRole])
def dashboard_stats(request):
    S.refresh_booking_states()
    now = timezone.now()
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    ev = GateEvent.objects.filter(created_at__gte=today)
    by = dict(ev.values_list("decision").annotate(c=Count("id")))
    lots = ParkingLot.objects.filter(is_active=True)
    total = sum(l.total_slots for l in lots)
    avail = sum(l.available() for l in lots)
    return Response({
        "entries_today": ev.filter(direction="in").count(),
        "exits_today": ev.filter(direction="out").count(),
        "decisions_today": {k: by.get(k, 0) for k in ("authorized", "unauthorized", "expired", "needs_review")},
        "pending_review": GateEvent.objects.filter(decision="needs_review", final_decision="",
                                                   created_at__gte=now - timedelta(hours=12)).count(),
        "vehicles_total": Vehicle.objects.count(),
        "vehicles_pending": Vehicle.objects.filter(status="pending").count(),
        "bookings_pending": Booking.objects.filter(status="pending").count(),
        "passes_live": Booking.objects.filter(status__in=["approved", "checked_in"], start__lte=now, end__gte=now).count(),
        "flags_open": Flag.objects.filter(status__in=["open", "notified", "acknowledged"]).count(),
        "faculty_pending": User.objects.filter(role="faculty", role_verified=False, is_email_verified=True).count(),
        "slots_total": total, "slots_available": avail,
        "cameras_online": sum(1 for c in Camera.objects.filter(is_active=True) if c.online),
        "cameras_total": Camera.objects.filter(is_active=True).count(),
        "lots": LotSerializer(lots, many=True, context={"request": request}).data,
    })


# ------------------------------------------------------------------ users (admin)
@api_view(["GET", "POST"])
@permission_classes([IsAdminRole])
def users(request):
    if request.method == "POST":
        d = request.data
        email = (d.get("email") or "").lower().strip()
        if User.objects.filter(email=email).exists():
            return Response({"detail": "Email already exists"}, status=400)
        u = User.objects.create_user(email=email, password=d.get("password") or "Thapar@123",
                                     full_name=d.get("full_name", ""), role=d.get("role", "guard"),
                                     phone=d.get("phone", ""), gate=d.get("gate", ""), is_email_verified=True)
        S.audit(request.user, "user.create", u, f"{u.email} as {u.role}")
        return Response(AdminUserSerializer(u).data, status=201)
    qs = User.objects.all().order_by("-date_joined")
    if r := request.GET.get("role"):
        qs = qs.filter(role=r)
    if request.GET.get("pending"):
        qs = qs.filter(role="faculty", role_verified=False)
    if q := request.GET.get("q"):
        qs = qs.filter(Q(email__icontains=q) | Q(full_name__icontains=q) | Q(phone__icontains=q))
    return Response(AdminUserSerializer(qs[:300], many=True).data)


@api_view(["PATCH"])
@permission_classes([IsAdminRole])
def user_detail(request, pk):
    u = get_object_or_404(User, pk=pk)
    before = {"role": u.role, "role_verified": u.role_verified, "is_active": u.is_active}
    s = AdminUserSerializer(u, data=request.data, partial=True)
    s.is_valid(raise_exception=True)
    s.save()
    S.audit(request.user, "user.update", u, f"{u.email}", before=before, after=request.data)
    if not before["role_verified"] and u.role_verified:
        S.notify(u, "Faculty account verified", "You can now pre-book parking slots.", "account")
    return Response(s.data)


# ------------------------------------------------------------------ vehicles
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def vehicles(request):
    u = request.user
    if request.method == "POST":
        data = request.data.copy()
        plate, valid = P.normalize_plate(data.get("plate", ""))
        if len(plate) < 6:
            return Response({"detail": "Enter a valid vehicle number, e.g. PB11AB1234."}, status=400)
        if Vehicle.objects.filter(plate=plate).exists():
            return Response({"detail": f"{P.pretty_plate(plate)} is already registered."}, status=400)
        data["plate"] = plate
        s = VehicleSerializer(data=data, context={"request": request})
        s.is_valid(raise_exception=True)
        if u.is_admin:
            owner_id = request.data.get("owner")
            owner = User.objects.filter(pk=owner_id).first() if owner_id else None
            if not owner and request.data.get("owner_email"):
                owner = User.objects.filter(email__iexact=request.data["owner_email"].strip()).first()
                if not owner:
                    return Response({"detail": "No app account with that email. Leave it blank and fill owner name/phone."},
                                    status=400)
            v = s.save(owner=owner, status=request.data.get("status") or Vehicle.Status.ACTIVE,
                       approved_by=u, approved_at=timezone.now())
        else:
            cat = u.role if u.role in ("faculty", "student", "vendor", "visitor") else "staff"
            v = s.save(owner=u, category=cat, status=Vehicle.Status.PENDING)
            S.notify_role(["admin"], "Vehicle registration request", f"{u.display_name} registered "
                          f"{P.pretty_plate(plate)}", "approval", {"vehicle": v.id})
        S.audit(u, "vehicle.create", v, f"{v.plate} ({v.status})")
        return Response(VehicleSerializer(v, context={"request": request}).data, status=201)

    qs = Vehicle.objects.select_related("owner", "home_lot", "sponsor")
    if not u.is_staff_role:
        qs = qs.filter(Q(owner=u) | Q(sponsor=u))
    if st := request.GET.get("status"):
        qs = qs.filter(status=st)
    if cat := request.GET.get("category"):
        qs = qs.filter(category=cat)
    if q := request.GET.get("q"):
        cq = P.clean(q)
        qs = qs.filter(Q(plate__icontains=cq or q) | Q(owner__full_name__icontains=q) | Q(owner__email__icontains=q)
                       | Q(owner_name__icontains=q) | Q(make__icontains=q) | Q(model_name__icontains=q))
    return Response(VehicleSerializer(qs[:500], many=True, context={"request": request}).data)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def vehicle_detail(request, pk):
    v = get_object_or_404(Vehicle, pk=pk)
    u = request.user
    if not (u.is_staff_role or v.owner_id == u.id):
        return Response(status=404)
    if request.method == "DELETE":
        if not (u.is_admin or (v.owner_id == u.id and v.status == "pending")):
            return Response({"detail": "Only pending vehicles can be removed by owners."}, status=403)
        S.audit(u, "vehicle.delete", v, v.plate)
        v.delete()
        return Response(status=204)
    if request.method == "PATCH":
        if not (u.is_admin or v.owner_id == u.id):
            return Response(status=403)
        data = request.data.copy()
        if "plate" in data:
            if not u.is_admin:
                data.pop("plate")
            else:
                data["plate"] = P.normalize_plate(data["plate"])[0]
        s = VehicleSerializer(v, data=data, partial=True, context={"request": request})
        s.is_valid(raise_exception=True)
        s.save()
        S.audit(u, "vehicle.update", v, v.plate, changes=dict(request.data))
        return Response(s.data)
    data = VehicleSerializer(v, context={"request": request}).data
    if u.is_staff_role:
        data["recent_events"] = GateEventSerializer(v.events.all()[:10], many=True, context={"request": request}).data
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAdminRole])
def vehicle_action(request, pk):
    v = get_object_or_404(Vehicle, pk=pk)
    action = request.data.get("action")
    mapping = {"authorize": Vehicle.Status.ACTIVE, "suspend": Vehicle.Status.SUSPENDED,
               "revoke": Vehicle.Status.REVOKED, "pending": Vehicle.Status.PENDING}
    if action not in mapping:
        return Response({"detail": "Unknown action"}, status=400)
    old = v.status
    v.status = mapping[action]
    if action == "authorize":
        v.approved_by, v.approved_at = request.user, timezone.now()
        if request.data.get("valid_until"):
            v.valid_until = request.data["valid_until"]
    v.save()
    reason = request.data.get("reason", "")
    S.audit(request.user, f"vehicle.{action}", v, f"{v.plate}: {old} → {v.status}", reason=reason)
    if v.notify_user:
        S.notify(v.notify_user, f"Vehicle {P.pretty_plate(v.plate)} {v.get_status_display().lower()}",
                 reason or f"Your vehicle access status is now: {v.get_status_display()}.", "vehicle", {"vehicle": v.id},
                 email=True)
    return Response(VehicleSerializer(v, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsStaffRole])
def vehicle_lookup(request):
    plate, valid = P.normalize_plate(request.GET.get("plate", ""))
    v, dist = S.find_vehicle(plate)
    bk, state = S.active_pass_for(plate)
    return Response({
        "plate": plate, "pretty": P.pretty_plate(plate), "valid_format": valid,
        "match": "exact" if dist == 0 else ("near" if v else "none"),
        "vehicle": S.vehicle_card(v, request.user) if v else None,
        "pass": (S.booking_brief(bk) | {"live": state == "live"}) if bk else None,
        "decision": _decision_preview(plate, valid),
    })


def _decision_preview(plate, valid):
    d = S.decide(plate, valid, 1.0)
    return {"decision": d["decision"], "reason": d["reason"]}


@api_view(["POST"])
@permission_classes([IsStaffRole])
def reveal_contact(request, pk):
    v = get_object_or_404(Vehicle, pk=pk)
    S.audit(request.user, "contact.reveal", v, f"Phone of {v.plate} revealed to {request.user.email}",
            reason=request.data.get("reason", ""))
    return Response({"phone": v.contact_phone, "name": v.contact_name})


# ------------------------------------------------------------------ vision
@api_view(["POST"])
@permission_classes([IsStaffRole])
@parser_classes(PARSERS)
def vision_plate(request):
    """Read plates from a photo and return registry matches (no gate event). Used by guard lookup/flag."""
    data = get_image_bytes(request)
    if not data:
        return Response({"detail": "image required"}, status=400)
    try:
        img = P.decode_image(data)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    reads = P.read_plates(img)
    out = []
    for r in reads:
        v, dist = S.find_vehicle(r.plate)
        out.append(r.to_dict() | {"match": "exact" if dist == 0 else ("near" if v else "none"),
                                  "vehicle": S.vehicle_card(v, request.user) if v else None})
    return Response({"reads": out})


# ------------------------------------------------------------------ lots
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def lots(request):
    if request.method == "POST":
        if not request.user.is_admin:
            return Response(status=403)
        s = LotSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        lot = s.save()
        lot.sync_slots()
        S.audit(request.user, "lot.create", lot, lot.name)
        return Response(LotSerializer(lot, context={"request": request}).data, status=201)
    S.refresh_booking_states()
    qs = ParkingLot.objects.all() if request.user.is_admin else ParkingLot.objects.filter(is_active=True)
    return Response(LotSerializer(qs, many=True, context={"request": request}).data)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def lot_detail(request, pk):
    lot = get_object_or_404(ParkingLot, pk=pk)
    if request.method == "PATCH":
        if not request.user.is_admin:
            return Response(status=403)
        before = {"total_slots": lot.total_slots, "visitor_slots": lot.visitor_slots}
        s = LotSerializer(lot, data=request.data, partial=True, context={"request": request})
        s.is_valid(raise_exception=True)
        lot = s.save()
        if lot.visitor_slots > lot.total_slots:
            lot.visitor_slots = lot.total_slots
            lot.save()
        lot.sync_slots()
        S.audit(request.user, "lot.update", lot, lot.name, before=before, after=dict(request.data))
        return Response(LotSerializer(lot, context={"request": request}).data)
    return Response(LotSerializer(lot, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lot_slots(request, pk):
    lot = get_object_or_404(ParkingLot, pk=pk)
    S.refresh_booking_states()
    start = _dt(request.GET.get("start")) or timezone.now()
    end = _dt(request.GET.get("end")) or start + timedelta(hours=2)
    kind = request.GET.get("kind")
    if not kind:
        kind = {"faculty": "faculty", "vendor": "vendor", "visitor": "visitor"}.get(request.user.role)
    slots = S.slot_states(lot, start, end, kind)
    return Response({"lot": LotSerializer(lot, context={"request": request}).data, "start": start, "end": end,
                     "slots": slots, "free": sum(1 for s in slots if s["state"] == "free")})


@api_view(["POST"])
@permission_classes([IsStaffRole])
@parser_classes(PARSERS)
def lot_detect(request, pk):
    lot = get_object_or_404(ParkingLot, pk=pk)
    data = get_image_bytes(request)
    if not data:
        return Response({"detail": "image required"}, status=400)
    try:
        res = S.process_lot_image(lot, data, user=request.user)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    lot.refresh_from_db()
    return Response({"result": res, "lot": LotSerializer(lot, context={"request": request}).data})


@api_view(["POST"])
@permission_classes([IsStaffRole])
def lot_manual(request, pk):
    lot = get_object_or_404(ParkingLot, pk=pk)
    try:
        S.set_manual_occupancy(lot, request.user, int(request.data.get("occupied", 0)), request.data.get("two_wheelers"))
    except (TypeError, ValueError):
        return Response({"detail": "occupied must be a number"}, status=400)
    return Response(LotSerializer(lot, context={"request": request}).data)


# ------------------------------------------------------------------ bookings / passes
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def bookings(request):
    u = request.user
    if request.method == "POST":
        d = request.data
        lot = get_object_or_404(ParkingLot, pk=d.get("lot"), is_active=True)
        try:
            bk = S.create_booking(u, lot=lot, start=_dt(d.get("start")), end=_dt(d.get("end")),
                                  plate=d.get("vehicle_plate", ""), slot_id=d.get("slot"), purpose=d.get("purpose", ""),
                                  host_name=d.get("host_name", ""), vehicle_desc=d.get("vehicle_desc", ""),
                                  gate=d.get("gate") or "Main Gate")
        except S.ServiceError as e:
            return err(e)
        except TypeError:
            return Response({"detail": "start and end are required"}, status=400)
        return Response(BookingSerializer(bk, context={"request": request}).data, status=201)
    S.refresh_booking_states()
    qs = Booking.objects.select_related("lot", "slot", "user")
    if not (u.is_staff_role and request.GET.get("all")):
        qs = qs.filter(user=u)
    if st := request.GET.get("status"):
        qs = qs.filter(status__in=st.split(","))
    if request.GET.get("upcoming"):
        qs = qs.filter(end__gte=timezone.now()).order_by("start")
    if q := request.GET.get("q"):
        qs = qs.filter(Q(code__icontains=q) | Q(vehicle_plate__icontains=P.clean(q) or q) | Q(user__full_name__icontains=q))
    return Response(BookingSerializer(qs[:300], many=True, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_detail(request, pk):
    bk = get_object_or_404(Booking, pk=pk)
    if not (request.user.is_staff_role or bk.user_id == request.user.id):
        return Response(status=404)
    return Response(BookingSerializer(bk, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_action(request, pk):
    bk = get_object_or_404(Booking, pk=pk)
    action = request.data.get("action")
    u = request.user
    if action in ("approve", "reject", "checkout") and not u.is_staff_role:
        return Response(status=403)
    if action in ("approve", "reject") and not u.is_admin:
        return Response({"detail": "Only admins approve passes."}, status=403)
    if action == "cancel" and not (bk.user_id == u.id or u.is_admin):
        return Response(status=403)
    try:
        S.set_booking_status(bk, u, action, request.data.get("reason", ""))
    except S.ServiceError as e:
        return err(e)
    return Response(BookingSerializer(bk, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsStaffRole])
def pass_verify(request):
    token = (request.data.get("token") or "").strip()
    gate = request.data.get("gate") or request.user.gate or "Main Gate"
    check_in = request.data.get("check_in", True) not in (False, "false", "0")
    if not token:
        return Response({"detail": "token or code required"}, status=400)
    if token.upper().startswith("TSP-") and len(token) <= 12:
        return Response(S.verify_pass_code(token, request.user, gate, check_in))
    res = S.verify_pass_token(token, request.user, gate, check_in=check_in)
    return Response(res)


# ------------------------------------------------------------------ gate
@api_view(["POST"])
@permission_classes([IsStaffRole])
@parser_classes(PARSERS)
def gate_scan(request):
    """Image (multipart 'image' / base64) -> ANPR -> decision. Or 'plate' for manual entry."""
    gate = request.data.get("gate") or request.user.gate or "Main Gate"
    direction = request.data.get("direction") or "in"
    src = request.data.get("source") or "guard"
    if src not in GateEvent.Source.values:
        src = "guard"
    data = get_image_bytes(request)
    if data:
        try:
            res = S.process_gate_image(data, gate=gate, direction=direction, source=src, user=request.user,
                                       require_read=str(request.data.get("auto", "")).lower() in ("1", "true"))
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        ev = res.get("event")
        return Response({"reads": res["reads"], "created": res.get("created"),
                         "event": GateEventSerializer(ev, context={"request": request}).data if ev else None})
    plate = request.data.get("plate", "")
    if not plate:
        return Response({"detail": "image or plate required"}, status=400)
    ev, created = S.record_gate_event(plate=plate, raw=plate, confidence=1.0, gate=gate, direction=direction,
                                      source=GateEvent.Source.MANUAL, user=request.user)
    S.audit(request.user, "gate.manual", ev, f"{ev.plate} {direction} at {gate}: {ev.decision}")
    return Response({"reads": [], "created": created, "event": GateEventSerializer(ev, context={"request": request}).data})


@api_view(["GET"])
@permission_classes([IsStaffRole])
def gate_events(request):
    qs = GateEvent.objects.select_related("vehicle", "booking", "booking__user", "reviewed_by", "camera")
    if since := request.GET.get("since"):
        d = _dt(since)
        if d:
            qs = qs.filter(updated_at__gt=d)
    if g := request.GET.get("gate"):
        qs = qs.filter(gate=g)
    if dec := request.GET.get("decision"):
        qs = qs.filter(Q(final_decision=dec) | Q(final_decision="", decision=dec))
    if request.GET.get("unreviewed"):
        qs = qs.filter(decision="needs_review", final_decision="")
    if q := request.GET.get("q"):
        qs = qs.filter(plate__icontains=P.clean(q))
    if day := request.GET.get("date"):
        qs = qs.filter(created_at__date=day)
    limit = min(int(request.GET.get("limit", 60)), 500)
    return Response(GateEventSerializer(qs.order_by("-updated_at")[:limit], many=True, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsStaffRole])
def gate_review(request, pk):
    ev = get_object_or_404(GateEvent, pk=pk)
    final = request.data.get("final_decision", "")
    if final and final not in GateEvent.Decision.values:
        return Response({"detail": "invalid decision"}, status=400)
    S.review_event(ev, request.user, final, request.data.get("plate", ""), request.data.get("note", ""))
    return Response(GateEventSerializer(ev, context={"request": request}).data)


# ------------------------------------------------------------------ flags
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes(PARSERS)
def flags(request):
    u = request.user
    if request.method == "POST":
        if not u.is_staff_role:
            return Response(status=403)
        d = request.data
        lot = ParkingLot.objects.filter(pk=d.get("lot")).first() if d.get("lot") else None
        try:
            flag, sent, existed = S.create_flag(u, plate=d.get("plate", ""), reason=d.get("reason") or "no_parking",
                                                lot=lot, location=d.get("location", ""), note=d.get("note", ""),
                                                image_bytes=get_image_bytes(request),
                                                notify_now=str(d.get("notify", "true")).lower() != "false")
        except S.ServiceError as e:
            return err(e)
        return Response({"flag": FlagSerializer(flag, context={"request": request}).data, "alert": sent,
                         "existing": existed}, status=201)
    if u.is_staff_role and not request.GET.get("mine"):
        qs = Flag.objects.select_related("vehicle", "lot", "created_by")
    else:
        qs = Flag.objects.filter(Q(vehicle__owner=u) | Q(vehicle__sponsor=u))
    if request.GET.get("open"):
        qs = qs.filter(status__in=["open", "notified", "acknowledged"])
    return Response(FlagSerializer(qs[:200], many=True, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def flag_action(request, pk):
    flag = get_object_or_404(Flag, pk=pk)
    u = request.user
    action = request.data.get("action")
    is_owner = flag.vehicle and (flag.vehicle.owner_id == u.id or flag.vehicle.sponsor_id == u.id)
    try:
        if action == "ack" and (is_owner or u.is_staff_role):
            S.acknowledge_flag(flag, u, request.data.get("reply", "Moving it now."))
            res = {"detail": "Thanks! Security has been informed."}
        elif action == "notify" and u.is_staff_role:
            res = S.send_flag_alert(flag, u, force=bool(request.data.get("force")) and u.is_admin)
        elif action in ("resolve", "cancel") and u.is_staff_role:
            S.resolve_flag(flag, u, request.data.get("note", ""), cancel=action == "cancel")
            res = {"detail": "Flag closed."}
        else:
            return Response({"detail": "Not allowed"}, status=403)
    except S.ServiceError as e:
        return err(e)
    return Response(res | {"flag": FlagSerializer(flag, context={"request": request}).data})


# ------------------------------------------------------------------ notifications
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def notifications(request):
    qs = request.user.notifications.all()
    if request.GET.get("unread"):
        qs = qs.filter(read_at__isnull=True)
    if after := request.GET.get("after_id"):
        qs = qs.filter(id__gt=int(after))
    return Response({"unread": request.user.notifications.filter(read_at__isnull=True).count(),
                     "items": NotificationSerializer(qs[:100], many=True).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def notifications_read(request):
    qs = request.user.notifications.filter(read_at__isnull=True)
    if ids := request.data.get("ids"):
        qs = qs.filter(id__in=ids)
    n = qs.update(read_at=timezone.now())
    return Response({"marked": n})


# ------------------------------------------------------------------ cameras
@api_view(["GET", "POST"])
@permission_classes([IsStaffRole])
def cameras(request):
    if request.method == "POST":
        if not request.user.is_admin:
            return Response(status=403)
        s = CameraSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        cam = s.save()
        S.audit(request.user, "camera.create", cam, cam.name)
        return Response(CameraSerializer(cam, context={"request": request}).data, status=201)
    data = CameraSerializer(Camera.objects.select_related("lot"), many=True, context={"request": request}).data
    if not request.user.is_admin:
        for c in data:
            c["key"] = None
    return Response(data)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAdminRole])
def camera_detail(request, pk):
    cam = get_object_or_404(Camera, pk=pk)
    if request.method == "DELETE":
        S.audit(request.user, "camera.delete", cam, cam.name)
        cam.delete()
        return Response(status=204)
    s = CameraSerializer(cam, data=request.data, partial=True, context={"request": request})
    s.is_valid(raise_exception=True)
    s.save()
    return Response(s.data)


@api_view(["POST"])
@permission_classes([AllowAny])
@parser_classes(PARSERS + [])
def camera_ingest(request):
    """
    Frame upload from ESP32-CAM / IP-camera worker / phone node.
    Header X-Camera-Key: <key>   (or ?key=)
    Body: raw image/jpeg, or multipart 'image', or JSON image_base64.
    """
    key = request.headers.get("X-Camera-Key") or request.GET.get("key")
    cam = Camera.objects.filter(key=key, is_active=True).select_related("lot").first() if key else None
    if not cam:
        return Response({"detail": "Unknown or inactive camera key"}, status=401)
    data = get_image_bytes(request)
    if not data:
        return Response({"detail": "no image"}, status=400)
    try:
        if cam.kind == Camera.Kind.GATE:
            res = S.process_gate_image(data, gate=cam.gate or "Main Gate", direction=cam.direction,
                                       source=GateEvent.Source.CAMERA, camera=cam)
            ev = res.get("event")
            return Response({"ok": True, "kind": "gate", "reads": res["reads"],
                             "decision": ev.decision if ev else None, "plate": ev.plate if ev else None})
        if not cam.lot:
            return Response({"detail": "Lot camera has no lot assigned"}, status=400)
        res = S.process_lot_image(cam.lot, data, camera=cam)
        return Response({"ok": True, "kind": "lot", "occupied": res["four_wheelers"], "available": res["available"]})
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)


# ------------------------------------------------------------------ audit
@api_view(["GET"])
@permission_classes([IsAdminRole])
def audit_log(request):
    qs = AuditLog.objects.select_related("actor")
    if a := request.GET.get("action"):
        qs = qs.filter(action__startswith=a)
    if q := request.GET.get("q"):
        qs = qs.filter(Q(summary__icontains=q) | Q(actor__email__icontains=q))
    return Response(AuditSerializer(qs[: int(request.GET.get("limit", 200))], many=True).data)
