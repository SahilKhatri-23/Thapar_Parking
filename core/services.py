"""
Domain logic shared by the REST API, the web portal and camera ingest.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import cv2
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from vision import plates as P
from .models import (AuditLog, Booking, Camera, Flag, GateEvent, Notification, ParkingLot, Slot, Vehicle)

log = logging.getLogger("core")
CFG = settings.PARKING


class ServiceError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# ------------------------------------------------------------------ audit / notify
def audit(actor, action, target=None, summary="", **details):
    AuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target_type=target.__class__.__name__ if target is not None else "",
        target_id=str(getattr(target, "pk", "") or ""),
        summary=summary[:250],
        details=details or None,
    )


def notify(user, title, body="", kind="system", data=None, urgent=False, email=False):
    if not user:
        return None
    n = Notification.objects.create(user=user, title=title, body=body, kind=kind, data=data or {}, urgent=urgent)
    if email and user.email:
        try:
            send_mail(f"[Thapar Parking] {title}", body, None, [user.email], fail_silently=True)
        except Exception as e:  # pragma: no cover
            log.warning("mail failed: %s", e)
    return n


def notify_role(roles, title, body="", kind="system", data=None, urgent=False):
    from accounts.models import User
    for u in User.objects.filter(role__in=roles, is_active=True):
        notify(u, title, body, kind, data, urgent)


# ------------------------------------------------------------------ housekeeping
def refresh_booking_states():
    """Lazy state machine: expire finished passes, release no-shows."""
    now = timezone.now()
    grace = timedelta(minutes=CFG["BOOKING_GRACE_MIN"])
    Booking.objects.filter(status=Booking.Status.APPROVED, end__lt=now).update(status=Booking.Status.EXPIRED)
    Booking.objects.filter(status=Booking.Status.PENDING, end__lt=now).update(status=Booking.Status.EXPIRED)
    Booking.objects.filter(status=Booking.Status.CHECKED_IN, end__lt=now - timedelta(hours=2)).update(
        status=Booking.Status.COMPLETED)
    # faculty bookings not used within grace after start release their slot
    Booking.objects.filter(kind=Booking.Kind.FACULTY, status=Booking.Status.APPROVED,
                           start__lt=now - grace, checked_in_at__isnull=True).update(status=Booking.Status.NO_SHOW)


# ------------------------------------------------------------------ vehicles
def find_vehicle(plate: str):
    """Exact match, else (vehicle, distance) of closest plate within edit distance 1."""
    plate = P.clean(plate)
    if not plate:
        return None, None
    v = Vehicle.objects.filter(plate=plate).select_related("owner", "sponsor", "home_lot").first()
    if v:
        return v, 0
    # cheap candidate filter: same length +/-1 and same last 3 digits or same state code
    cands = Vehicle.objects.filter(Q(plate__endswith=plate[-3:]) | Q(plate__startswith=plate[:4]))[:200]
    best = None
    for c in cands:
        d = P.edit_distance(plate, c.plate)
        if d <= 1 and (best is None or d < best[1]):
            best = (c, d)
    return best if best else (None, None)


def active_pass_for(plate: str, at=None):
    at = at or timezone.now()
    grace = timedelta(minutes=CFG["BOOKING_GRACE_MIN"])
    qs = Booking.objects.filter(vehicle_plate=plate, status__in=[Booking.Status.APPROVED, Booking.Status.CHECKED_IN])
    live = qs.filter(start__lte=at + grace, end__gte=at).order_by("start").first()
    if live:
        return live, "live"
    other = Booking.objects.filter(vehicle_plate=plate).exclude(
        status__in=[Booking.Status.REJECTED, Booking.Status.CANCELLED]).order_by("-end").first()
    if other:
        return other, "outside"
    return None, None


def vehicle_card(v: Vehicle, viewer):
    """Owner info with contact masked unless the viewer is admin."""
    if not v:
        return None
    full = viewer is not None and getattr(viewer, "is_admin", False)
    phone = v.contact_phone
    masked = (phone[:3] + "•" * max(len(phone) - 5, 0) + phone[-2:]) if len(phone) > 5 else ("•" * len(phone))
    owner = v.owner
    return {
        "id": v.id, "plate": v.plate, "pretty": P.pretty_plate(v.plate), "status": v.status,
        "status_label": v.get_status_display(), "expired": v.is_expired,
        "valid_until": v.valid_until.isoformat() if v.valid_until else None,
        "category": v.category, "vehicle_type": v.vehicle_type, "make": v.make, "model": v.model_name,
        "colour": v.colour, "sticker_no": v.sticker_no,
        "home_lot": v.home_lot.name if v.home_lot else None,
        "owner_name": v.contact_name,
        "owner_role": owner.get_role_display() if owner else ("Host: " + v.sponsor.display_name if v.sponsor else "—"),
        "owner_email": (owner.email if owner else "") if full else "",
        "department": owner.department if owner else "",
        "phone": phone if full else masked,
        "phone_masked": not full,
        "has_phone": bool(phone),
        "has_app_account": bool(v.notify_user),
        "open_flags": v.flags.filter(status__in=[Flag.Status.OPEN, Flag.Status.NOTIFIED, Flag.Status.ACKNOWLEDGED]).count(),
    }


# ------------------------------------------------------------------ gate decision
def decide(plate: str, valid_format: bool, confidence: float, at=None):
    """
    Gate decision flow (advisory; guard confirms):
      no read / bad format / low confidence  -> needs_review
      registry active & valid                -> authorized
      registry suspended/revoked             -> unauthorized
      registry expired                       -> expired
      registry pending                       -> needs_review
      visitor/vendor pass inside window      -> authorized
      pass outside window                    -> expired
      near-match in registry (1 char)        -> needs_review
      otherwise                              -> unauthorized
    Open flag on the vehicle forces needs_review (watch-list).
    """
    out = {"decision": GateEvent.Decision.REVIEW, "reason": "", "vehicle": None, "booking": None, "candidates": []}
    if not plate:
        out["reason"] = "No plate could be read. Enter plate manually."
        return out
    vehicle, dist = find_vehicle(plate)
    exact = vehicle if dist == 0 else None
    low_conf = confidence < CFG["GATE_AUTO_CONF"]

    if exact:
        out["vehicle"] = exact
        if exact.status == Vehicle.Status.ACTIVE and not exact.is_expired:
            out["decision"], out["reason"] = GateEvent.Decision.AUTHORIZED, f"Registered {exact.get_category_display().lower()} vehicle"
        elif exact.status in (Vehicle.Status.SUSPENDED, Vehicle.Status.REVOKED):
            out["decision"], out["reason"] = GateEvent.Decision.UNAUTHORIZED, f"Access {exact.status}"
        elif exact.is_expired:
            out["decision"], out["reason"] = GateEvent.Decision.EXPIRED, f"Registration expired {exact.valid_until:%d %b %Y}"
        else:
            out["decision"], out["reason"] = GateEvent.Decision.REVIEW, "Registration pending admin approval"
    if not exact or out["decision"] != GateEvent.Decision.AUTHORIZED:
        bk, state = active_pass_for(plate, at)
        if bk and state == "live":
            out["booking"] = bk
            out["decision"] = GateEvent.Decision.AUTHORIZED
            out["reason"] = f"{bk.get_kind_display()} {bk.code} valid till {timezone.localtime(bk.end):%H:%M}"
        elif bk and state == "outside" and not exact:
            out["booking"] = bk
            out["decision"] = GateEvent.Decision.EXPIRED
            out["reason"] = f"Pass {bk.code} is {bk.get_status_display().lower()} / outside its time window"
    if not exact and not out["booking"]:
        if vehicle:  # 1-char near match
            out["decision"] = GateEvent.Decision.REVIEW
            out["reason"] = f"Close match {P.pretty_plate(vehicle.plate)} in registry: possible OCR error"
            out["candidates"] = [{"id": vehicle.id, "plate": vehicle.plate, "pretty": P.pretty_plate(vehicle.plate)}]
        else:
            out["decision"] = GateEvent.Decision.UNAUTHORIZED
            out["reason"] = "Not in registry and no valid pass"

    # safety rules: never auto-approve weak reads
    if out["decision"] == GateEvent.Decision.AUTHORIZED and (low_conf or not valid_format):
        out["decision"] = GateEvent.Decision.REVIEW
        out["reason"] = (f"Low OCR confidence ({confidence:.0%}). " if low_conf else "Unusual plate format. ") + \
                        "Verify plate visually. " + out["reason"]
    if exact and exact.flags.filter(status__in=[Flag.Status.OPEN, Flag.Status.NOTIFIED]).exists():
        out["decision"] = GateEvent.Decision.REVIEW
        out["reason"] = "Vehicle has an open flag (watch-list). " + out["reason"]
    return out


def _save_image(field, data: bytes, ext="jpg"):
    if data:
        field.save(f"{uuid.uuid4().hex[:12]}.{ext}", ContentFile(data), save=False)


def record_gate_event(*, plate, raw="", confidence=1.0, valid_format=None, gate="Main Gate", direction="in",
                      source=GateEvent.Source.MANUAL, camera=None, image_bytes=None, user=None):
    plate = P.clean(plate)
    if valid_format is None:
        plate, valid_format = P.normalize_plate(plate)
    d = decide(plate, valid_format, confidence)
    now = timezone.now()

    # de-duplicate a car sitting in front of the camera
    if plate:
        recent = GateEvent.objects.filter(plate=plate, gate=gate, direction=direction,
                                          created_at__gte=now - timedelta(seconds=CFG["GATE_DEDUP_SECONDS"])).first()
        if recent:
            recent.hits += 1
            if confidence > recent.confidence:
                recent.confidence, recent.plate_raw, recent.valid_format = confidence, raw, valid_format
                recent.decision, recent.reason = d["decision"], d["reason"]
                if image_bytes:
                    _save_image(recent.image, image_bytes)
            recent.save()
            return recent, False

    ev = GateEvent(gate=gate, direction=direction, camera=camera, source=source, plate=plate, plate_raw=raw[:30],
                   confidence=confidence, valid_format=bool(valid_format), decision=d["decision"], reason=d["reason"][:200],
                   candidates=d["candidates"] or None, vehicle=d["vehicle"],
                   booking=d["booking"])
    if image_bytes:
        _save_image(ev.image, image_bytes)
    if source in (GateEvent.Source.MANUAL, GateEvent.Source.GUARD) and user is not None:
        ev.reviewed_by = user
    ev.save()

    # pass check-in / check-out
    bk = d["booking"]
    if bk and ev.decision == GateEvent.Decision.AUTHORIZED:
        if direction == "in" and bk.status == Booking.Status.APPROVED:
            bk.status, bk.checked_in_at = Booking.Status.CHECKED_IN, now
            bk.save(update_fields=["status", "checked_in_at"])
            notify(bk.user, "Checked in at " + gate, f"Pass {bk.code} for {P.pretty_plate(bk.vehicle_plate)} verified. "
                   f"Proceed to {bk.lot.name}" + (f", slot {bk.slot.label}" if bk.slot else "") + ".", "booking",
                   {"booking": bk.id})
        elif direction == "out" and bk.status == Booking.Status.CHECKED_IN:
            bk.status, bk.checked_out_at = Booking.Status.COMPLETED, now
            bk.save(update_fields=["status", "checked_out_at"])
    # members: tell them where to park
    v = d["vehicle"]
    if v and v.owner and ev.decision == GateEvent.Decision.AUTHORIZED and direction == "in":
        lot = v.home_lot or ParkingLot.objects.filter(is_active=True).order_by("sort_order").first()
        if lot:
            notify(v.owner, "Welcome to Thapar", f"{P.pretty_plate(v.plate)} verified at {gate}. "
                   f"{lot.name} has {lot.available()} slots free.", "gate", {"lot": lot.id, "event": ev.id})
    if ev.decision == GateEvent.Decision.UNAUTHORIZED:
        notify_role(["admin"], "Unauthorized vehicle at " + gate, f"{P.pretty_plate(plate) or 'Unknown'}: {ev.reason}",
                    "gate", {"event": ev.id})
    return ev, True


def process_gate_image(image_bytes: bytes, *, gate="Main Gate", direction="in", source=GateEvent.Source.CAMERA,
                       camera=None, user=None, create_event=True, require_read=False):
    img = P.decode_image(image_bytes)
    reads = P.read_plates(img)
    ok, jpg = cv2.imencode(".jpg", P.annotate_plates(img, reads), [cv2.IMWRITE_JPEG_QUALITY, 82])
    annotated = jpg.tobytes() if ok else image_bytes
    best = reads[0] if reads else None
    result = {"reads": [r.to_dict() for r in reads], "event": None}
    if camera:
        camera.last_seen_at = timezone.now()
        camera.last_result = {"reads": result["reads"][:3], "at": timezone.now().isoformat()}
        _save_image(camera.last_image, annotated)
        camera.save()
    if create_event and (best or (source != GateEvent.Source.CAMERA and not require_read)):
        ev, created = record_gate_event(plate=best.plate if best else "", raw=best.raw if best else "",
                                        confidence=best.confidence if best else 0.0,
                                        valid_format=best.valid_format if best else False,
                                        gate=gate, direction=direction, source=source, camera=camera,
                                        image_bytes=annotated, user=user)
        result["event"] = ev
        result["created"] = created
    result["annotated"] = annotated
    return result


def review_event(ev: GateEvent, user, final_decision, corrected_plate="", note=""):
    old = {"plate": ev.plate, "decision": ev.effective_decision}
    if corrected_plate:
        plate, valid = P.normalize_plate(corrected_plate)
        if plate != ev.plate:
            d = decide(plate, valid, 1.0)
            ev.plate, ev.valid_format = plate, valid
            ev.vehicle, ev.booking = d["vehicle"], d["booking"]
            ev.reason = "Corrected by guard: " + d["reason"]
            if not final_decision:
                final_decision = d["decision"]
    ev.final_decision = final_decision or ev.decision
    ev.reviewed_by, ev.reviewed_at, ev.review_note = user, timezone.now(), note[:200]
    ev.save()
    audit(user, "gate.review", ev, f"{old['plate']} → {ev.plate}: {old['decision']} → {ev.final_decision}",
          note=note, before=old)
    if ev.booking and ev.final_decision == GateEvent.Decision.AUTHORIZED and ev.booking.status == Booking.Status.APPROVED \
            and ev.direction == "in":
        ev.booking.status, ev.booking.checked_in_at = Booking.Status.CHECKED_IN, timezone.now()
        ev.booking.save(update_fields=["status", "checked_in_at"])
    return ev


# ------------------------------------------------------------------ passes / QR
def verify_pass_token(token: str, user=None, gate="Main Gate", check_in=True):
    from django.core import signing
    try:
        data = Booking.parse_token(token.strip())
    except signing.BadSignature:
        audit(user, "pass.reject", None, "Invalid / forged QR token")
        return {"valid": False, "result": "invalid", "message": "Invalid or tampered QR code."}
    bk = Booking.objects.select_related("lot", "slot", "user").filter(pk=data.get("b"), code=data.get("c")).first()
    if not bk:
        return {"valid": False, "result": "invalid", "message": "Pass not found."}
    refresh_booking_states()
    bk.refresh_from_db()
    now = timezone.now()
    grace = timedelta(minutes=CFG["BOOKING_GRACE_MIN"])
    info = booking_brief(bk)
    if bk.status not in (Booking.Status.APPROVED, Booking.Status.CHECKED_IN):
        return {"valid": False, "result": bk.status, "message": f"Pass is {bk.get_status_display().lower()}.", "booking": info}
    if now < bk.start - grace:
        return {"valid": False, "result": "early",
                "message": f"Pass starts at {timezone.localtime(bk.start):%d %b %H:%M}.", "booking": info}
    if now > bk.end:
        return {"valid": False, "result": "expired", "message": "Pass has expired.", "booking": info}
    if check_in and bk.status == Booking.Status.APPROVED:
        bk.status, bk.checked_in_at = Booking.Status.CHECKED_IN, now
        bk.save(update_fields=["status", "checked_in_at"])
        GateEvent.objects.create(gate=gate, direction="in", source=GateEvent.Source.QR, plate=bk.vehicle_plate,
                                 confidence=1.0, valid_format=True, decision=GateEvent.Decision.AUTHORIZED,
                                 reason=f"QR pass {bk.code} verified", booking=bk, reviewed_by=user if user else None)
        notify(bk.user, "Pass verified", f"Welcome! Proceed to {bk.lot.name}" + (f", slot {bk.slot.label}" if bk.slot else ""),
               "booking", {"booking": bk.id})
    audit(user, "pass.verify", bk, f"{bk.code} {bk.vehicle_plate} OK at {gate}")
    info = booking_brief(bk)
    return {"valid": True, "result": "valid", "message": "Valid pass", "booking": info}


def verify_pass_code(code: str, user=None, gate="Main Gate", check_in=True):
    bk = Booking.objects.filter(code__iexact=code.strip()).first()
    if not bk:
        return {"valid": False, "result": "invalid", "message": "No pass with this code."}
    token = bk.qr_token
    if not token:
        return {"valid": False, "result": bk.status, "message": f"Pass is {bk.get_status_display().lower()}.",
                "booking": booking_brief(bk)}
    res = verify_pass_token(token, user, gate, check_in)
    audit(user, "pass.manual_code", bk, f"Verified by code {bk.code} (no QR)")
    return res


def booking_brief(bk: Booking):
    return {
        "id": bk.id, "code": bk.code, "kind": bk.kind, "kind_label": bk.get_kind_display(),
        "status": bk.status, "status_label": bk.get_status_display(),
        "plate": bk.vehicle_plate, "pretty_plate": P.pretty_plate(bk.vehicle_plate),
        "holder": bk.user.display_name, "organisation": bk.user.organisation, "purpose": bk.purpose,
        "host": bk.host_name, "lot": bk.lot.name, "lot_id": bk.lot_id,
        "slot": bk.slot.label if bk.slot else None,
        "start": bk.start.isoformat(), "end": bk.end.isoformat(),
    }


def slot_states(lot: ParkingLot, start, end, for_kind=None):
    """Return list of slot dicts with state free/booked for a time window."""
    taken = set(Booking.objects.filter(lot=lot, slot__isnull=False, status__in=Booking.HOLDING,
                                       start__lt=end, end__gt=start).values_list("slot_id", flat=True))
    out = []
    for s in lot.slots.filter(is_active=True).order_by("number"):
        allowed = True
        if for_kind == Booking.Kind.FACULTY and s.kind == Slot.Kind.VISITOR:
            allowed = False
        if for_kind in (Booking.Kind.VISITOR, Booking.Kind.VENDOR) and lot.visitor_slots and s.kind != Slot.Kind.VISITOR:
            allowed = False
        out.append({"id": s.id, "label": s.label, "number": s.number, "kind": s.kind,
                    "state": "booked" if s.id in taken else ("free" if allowed else "restricted")})
    return out


@transaction.atomic
def create_booking(user, *, lot: ParkingLot, start, end, plate, slot_id=None, purpose="", host_name="",
                   vehicle_desc="", gate="Main Gate"):
    refresh_booking_states()
    now = timezone.now()
    plate, _valid = P.normalize_plate(plate)
    if not plate or len(plate) < 6:
        raise ServiceError("Enter a valid vehicle number.")
    if end <= start:
        raise ServiceError("End time must be after start time.")
    if end < now:
        raise ServiceError("This time window is already over.")
    if (end - start) > timedelta(hours=CFG["BOOKING_MAX_HOURS"]):
        raise ServiceError(f"Maximum booking length is {CFG['BOOKING_MAX_HOURS']} hours.")
    if start > now + timedelta(days=14):
        raise ServiceError("Bookings open 14 days in advance.")

    if user.role == "faculty":
        kind = Booking.Kind.FACULTY
        if not user.role_verified:
            raise ServiceError("Your faculty account is awaiting admin verification.", 403)
        if not lot.bookable:
            raise ServiceError("This lot does not accept pre-booking.")
    elif user.role in ("vendor", "visitor"):
        kind = Booking.Kind.VENDOR if user.role == "vendor" else Booking.Kind.VISITOR
    elif user.role == "admin":
        kind = Booking.Kind.VISITOR
    else:
        raise ServiceError("Your role cannot book parking slots.", 403)
    if user.role not in lot.roles and user.role != "admin":
        raise ServiceError(f"{lot.name} is not open to {user.get_role_display().lower()}s.")

    # one active booking per user for overlapping time
    if Booking.objects.filter(user=user, status__in=Booking.HOLDING, start__lt=end, end__gt=start).exists():
        raise ServiceError("You already have a booking that overlaps this time.")

    states = slot_states(lot, start, end, kind)
    if slot_id:
        st = next((s for s in states if s["id"] == int(slot_id)), None)
        if not st:
            raise ServiceError("Slot not found in this lot.")
        if st["state"] != "free":
            raise ServiceError(f"Slot {st['label']} is not available for this time. Pick another.", 409)
    else:
        st = next((s for s in states if s["state"] == "free"), None)
        if not st:
            raise ServiceError("No free slots for this window. Try another time or lot.", 409)
    slot = Slot.objects.select_for_update().get(pk=st["id"])
    # re-check inside the transaction (overlap safety)
    if Booking.objects.filter(slot=slot, status__in=Booking.HOLDING, start__lt=end, end__gt=start).exists():
        raise ServiceError("That slot was just taken. Pick another.", 409)

    auto = kind == Booking.Kind.FACULTY or user.role == "admin" or CFG["VENDOR_AUTO_APPROVE"]
    bk = Booking.objects.create(user=user, kind=kind, lot=lot, slot=slot, vehicle_plate=plate, vehicle_desc=vehicle_desc,
                                purpose=purpose, host_name=host_name, gate=gate, start=start, end=end,
                                status=Booking.Status.APPROVED if auto else Booking.Status.PENDING,
                                approved_at=now if auto else None, approved_by=user if auto else None)
    audit(user, "booking.create", bk, f"{bk.code} {plate} {lot.name} {slot.label}")
    if not auto:
        notify_role(["admin"], "New pass request", f"{user.display_name} ({user.get_role_display()}) requested "
                    f"{lot.name} on {timezone.localtime(start):%d %b %H:%M}.", "approval", {"booking": bk.id})
    return bk


def set_booking_status(bk: Booking, user, status, reason=""):
    if status == "approve":
        if bk.status != Booking.Status.PENDING:
            raise ServiceError("Only pending bookings can be approved.")
        clash = Booking.objects.filter(slot=bk.slot, status__in=[Booking.Status.APPROVED, Booking.Status.CHECKED_IN],
                                       start__lt=bk.end, end__gt=bk.start).exclude(pk=bk.pk).exists()
        if clash:
            raise ServiceError("Slot clashes with another approved booking.", 409)
        bk.status, bk.approved_by, bk.approved_at = Booking.Status.APPROVED, user, timezone.now()
        msg = (f"Your pass {bk.code} for {timezone.localtime(bk.start):%d %b, %H:%M}–{timezone.localtime(bk.end):%H:%M} "
               f"at {bk.lot.name} is approved. Show the green QR pass at the gate.")
        notify(bk.user, "Pass approved ✅", msg, "booking", {"booking": bk.id}, email=True)
    elif status == "reject":
        if bk.status != Booking.Status.PENDING:
            raise ServiceError("Only pending bookings can be rejected.")
        bk.status, bk.reject_reason = Booking.Status.REJECTED, reason[:200]
        notify(bk.user, "Pass request declined", reason or "Your pass request was declined.", "booking",
               {"booking": bk.id}, email=True)
    elif status == "cancel":
        if bk.status not in (Booking.Status.PENDING, Booking.Status.APPROVED):
            raise ServiceError("This booking can no longer be cancelled.")
        bk.status = Booking.Status.CANCELLED
    elif status == "checkout":
        bk.status, bk.checked_out_at = Booking.Status.COMPLETED, timezone.now()
    else:
        raise ServiceError("Unknown action")
    bk.save()
    audit(user, f"booking.{status}", bk, f"{bk.code} {bk.vehicle_plate}", reason=reason)
    return bk


# ------------------------------------------------------------------ flags & alerts
def create_flag(user, *, plate, reason, lot=None, location="", note="", image_bytes=None, notify_now=True):
    plate, _ = P.normalize_plate(plate)
    if not plate:
        raise ServiceError("Plate is required.")
    vehicle, dist = find_vehicle(plate)
    if dist != 0:
        vehicle = None
    existing = Flag.objects.filter(plate=plate, status__in=[Flag.Status.OPEN, Flag.Status.NOTIFIED,
                                                           Flag.Status.ACKNOWLEDGED]).first()
    if existing:
        flag = existing
    else:
        flag = Flag(plate=plate, vehicle=vehicle, lot=lot, location=location, reason=reason, note=note, created_by=user)
        if image_bytes:
            _save_image(flag.image, image_bytes)
        flag.save()
        audit(user, "flag.create", flag, f"{plate}: {flag.get_reason_display()}", lot=lot.name if lot else location)
    sent = None
    if notify_now:
        try:
            sent = send_flag_alert(flag, user)
        except ServiceError as e:
            sent = {"sent": False, "message": e.message}
    return flag, sent, existing is not None


def send_flag_alert(flag: Flag, user, force=False):
    now = timezone.now()
    target = flag.vehicle.notify_user if flag.vehicle else None
    if not flag.is_open:
        raise ServiceError("Flag is closed.")
    if flag.notified_count >= CFG["ALERT_MAX_PER_FLAG"] and not force:
        raise ServiceError(f"Alert limit reached ({CFG['ALERT_MAX_PER_FLAG']}). Escalate by phone.", 429)
    if flag.vehicle:
        last = Flag.objects.filter(vehicle=flag.vehicle, last_notified_at__isnull=False).order_by("-last_notified_at").first()
        cool = timedelta(minutes=CFG["ALERT_COOLDOWN_MIN"])
        if last and now - last.last_notified_at < cool and not force:
            wait = int((cool - (now - last.last_notified_at)).total_seconds() // 60) + 1
            raise ServiceError(f"Owner was alerted recently. Next alert allowed in ~{wait} min.", 429)
    if not target:
        flag.save()
        return {"sent": False, "message": "Owner has no app account. Call the contact number instead."}
    where = flag.lot.name if flag.lot else (flag.location or "campus")
    title = "🚨 Action needed: move your vehicle"
    body = (f"Your vehicle {P.pretty_plate(flag.plate)} at {where} was flagged by security: "
            f"{flag.get_reason_display()}." + (f" Note: {flag.note}" if flag.note else "") +
            " Please move it or contact the security desk.")
    notify(target, title, body, "flag", {"flag": flag.id, "plate": flag.plate}, urgent=True, email=True)
    flag.notified_count += 1
    flag.last_notified_at = now
    if flag.status == Flag.Status.OPEN:
        flag.status = Flag.Status.NOTIFIED
    flag.save()
    audit(user, "flag.alert", flag, f"Alert #{flag.notified_count} to {target.email} for {flag.plate}")
    return {"sent": True, "message": f"Alert sent to {target.display_name} (in-app + email)."}


def acknowledge_flag(flag: Flag, user, reply=""):
    flag.status, flag.acknowledged_at, flag.owner_reply = Flag.Status.ACKNOWLEDGED, timezone.now(), reply[:200]
    flag.save()
    audit(user, "flag.ack", flag, f"Owner acknowledged {flag.plate}: {reply}")
    if flag.created_by:
        notify(flag.created_by, f"Owner responded: {P.pretty_plate(flag.plate)}", reply or "Owner is on the way.", "flag",
               {"flag": flag.id})


def resolve_flag(flag: Flag, user, note="", cancel=False):
    flag.status = Flag.Status.CANCELLED if cancel else Flag.Status.RESOLVED
    flag.resolved_at, flag.resolved_by, flag.resolution_note = timezone.now(), user, note[:200]
    flag.save()
    audit(user, "flag.cancel" if cancel else "flag.resolve", flag, f"{flag.plate}: {note}")
    if flag.vehicle and flag.vehicle.notify_user and not cancel:
        notify(flag.vehicle.notify_user, "Flag resolved", f"The flag on {P.pretty_plate(flag.plate)} is closed. Thank you.",
               "flag", {"flag": flag.id})


# ------------------------------------------------------------------ occupancy
def process_lot_image(lot: ParkingLot, image_bytes: bytes, camera: Camera | None = None, user=None):
    from vision import vehicles as V
    img = P.decode_image(image_bytes)
    res = V.detect_vehicles(img, lot.detection_zone)
    ann = V.annotate_vehicles(img, res, lot.total_slots)
    ok, jpg = cv2.imencode(".jpg", ann, [cv2.IMWRITE_JPEG_QUALITY, 82])
    ok2, raw = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    lot.occupied = min(res["four_wheelers"], lot.total_slots)
    lot.occupied_2w = res["two_wheelers"]
    lot.occupancy_source = ParkingLot.Source.CAMERA
    lot.occupancy_updated_at = timezone.now()
    if ok2:
        _save_image(lot.last_snapshot, raw.tobytes())
    if ok:
        _save_image(lot.last_annotated, jpg.tobytes())
    lot.save()
    if camera:
        camera.last_seen_at = timezone.now()
        camera.last_result = {"four_wheelers": res["four_wheelers"], "two_wheelers": res["two_wheelers"],
                              "at": timezone.now().isoformat()}
        if ok:
            _save_image(camera.last_image, jpg.tobytes())
        camera.save()
    res.pop("zone_px", None)
    return {"four_wheelers": res["four_wheelers"], "two_wheelers": res["two_wheelers"],
            "total_slots": lot.total_slots, "available": lot.available(),
            "detections": res["detections"], "image_size": res["image_size"], "model": res["model"]}


def set_manual_occupancy(lot: ParkingLot, user, occupied: int, two_wheelers=None):
    lot.occupied = max(0, min(int(occupied), lot.total_slots))
    if two_wheelers is not None:
        lot.occupied_2w = max(0, int(two_wheelers))
    lot.occupancy_source = ParkingLot.Source.MANUAL
    lot.occupancy_updated_at = timezone.now()
    lot.save()
    audit(user, "lot.manual_count", lot, f"{lot.name}: occupied={lot.occupied}")
