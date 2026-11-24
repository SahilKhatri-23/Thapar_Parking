import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


def _code(prefix="TSP", n=6):
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    return f"{prefix}-" + "".join(secrets.choice(alphabet) for _ in range(n))


def _camera_key():
    return secrets.token_urlsafe(18)


class ParkingLot(models.Model):
    class Source(models.TextChoices):
        NONE = "none", "No data"
        CAMERA = "camera", "Camera detection"
        MANUAL = "manual", "Guard update"

    name = models.CharField(max_length=80)
    code = models.SlugField(max_length=30, unique=True)
    description = models.CharField(max_length=200, blank=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    total_slots = models.PositiveIntegerField(default=20)
    visitor_slots = models.PositiveIntegerField(default=0, help_text="Slots reserved for visitor/vendor passes")
    two_wheeler_capacity = models.PositiveIntegerField(default=0)
    allowed_roles = models.CharField(max_length=120, default="faculty,student,visitor,vendor",
                                     help_text="Comma separated roles allowed to park here")
    bookable = models.BooleanField(default=True, help_text="Faculty can pre-book slots here")
    is_active = models.BooleanField(default=True)
    detection_zone = models.JSONField(blank=True, null=True,
                                      help_text="Polygon [[x,y],...] normalised 0..1 for camera detection")
    occupied = models.PositiveIntegerField(default=0)
    occupied_2w = models.PositiveIntegerField(default=0)
    occupancy_source = models.CharField(max_length=10, choices=Source.choices, default=Source.NONE)
    occupancy_updated_at = models.DateTimeField(null=True, blank=True)
    last_snapshot = models.ImageField(upload_to="lots/snapshots/", null=True, blank=True)
    last_annotated = models.ImageField(upload_to="lots/annotated/", null=True, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "name")

    def __str__(self):
        return self.name

    @property
    def roles(self):
        return [r.strip() for r in self.allowed_roles.split(",") if r.strip()]

    def active_reservations(self, at=None):
        """Bookings holding a slot right now but whose vehicle has not arrived yet."""
        at = at or timezone.now()
        grace = timedelta(minutes=settings.PARKING["BOOKING_GRACE_MIN"])
        return self.bookings.filter(status=Booking.Status.APPROVED,
                                    start__lte=at + grace, end__gte=at).count()

    def available(self, at=None):
        return max(self.total_slots - self.occupied - self.active_reservations(at), 0)

    @property
    def is_stale(self):
        if not self.occupancy_updated_at:
            return True
        return timezone.now() - self.occupancy_updated_at > timedelta(minutes=settings.PARKING["OCCUPANCY_STALE_MIN"])

    def sync_slots(self):
        """Make Slot rows match total_slots (A-01..). Visitor slots are the last N."""
        parts = [p for p in self.code.replace("_", "-").split("-") if p]
        prefix = ("".join(p[0] for p in parts) if len(parts) > 1 else self.code[:2]).upper()[:3]
        existing = list(self.slots.order_by("number"))
        for i in range(len(existing), self.total_slots):
            Slot.objects.create(lot=self, number=i + 1, label=f"{prefix}-{i + 1:02d}")
        for s in existing:
            want = f"{prefix}-{s.number:02d}"
            if s.label != want:
                s.label = want
                s.save(update_fields=["label"])
        Slot.objects.filter(lot=self, number__gt=self.total_slots).update(is_active=False)
        Slot.objects.filter(lot=self, number__lte=self.total_slots).update(is_active=True)
        first_visitor = self.total_slots - self.visitor_slots + 1
        Slot.objects.filter(lot=self, number__gte=first_visitor, number__lte=self.total_slots).update(kind=Slot.Kind.VISITOR)
        Slot.objects.filter(lot=self, number__lt=first_visitor).update(kind=Slot.Kind.GENERAL)


class Slot(models.Model):
    class Kind(models.TextChoices):
        GENERAL = "general", "General"
        VISITOR = "visitor", "Visitor / Vendor"
        ACCESSIBLE = "accessible", "Accessible"

    lot = models.ForeignKey(ParkingLot, on_delete=models.CASCADE, related_name="slots")
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=12)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.GENERAL)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("lot", "number")
        unique_together = ("lot", "number")

    def __str__(self):
        return f"{self.lot.code}:{self.label}"


class Vehicle(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        ACTIVE = "active", "Authorized"
        SUSPENDED = "suspended", "Suspended"
        REVOKED = "revoked", "Revoked"

    class Category(models.TextChoices):
        FACULTY = "faculty", "Faculty"
        STUDENT = "student", "Student"
        STAFF = "staff", "Staff"
        VISITOR = "visitor", "Visitor"
        VENDOR = "vendor", "Vendor"
        SERVICE = "service", "Campus service"

    class Type(models.TextChoices):
        CAR = "car", "Car"
        SUV = "suv", "SUV"
        TWO_WHEELER = "two_wheeler", "Two-wheeler"
        EV = "ev", "EV / e-cart"
        GOODS = "goods", "Goods vehicle"
        OTHER = "other", "Other"

    plate = models.CharField(max_length=15, unique=True, help_text="Normalised, e.g. PB11AB1234")
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="vehicles")
    sponsor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="sponsored_vehicles",
                                help_text="Host responsible for a visitor/vendor vehicle")
    owner_name = models.CharField(max_length=120, blank=True, help_text="If owner has no app account")
    owner_phone = models.CharField(max_length=20, blank=True)
    vehicle_type = models.CharField(max_length=12, choices=Type.choices, default=Type.CAR)
    make = models.CharField(max_length=40, blank=True)
    model_name = models.CharField(max_length=40, blank=True)
    colour = models.CharField(max_length=30, blank=True)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.STUDENT)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    valid_until = models.DateField(null=True, blank=True)
    home_lot = models.ForeignKey(ParkingLot, null=True, blank=True, on_delete=models.SET_NULL, related_name="regular_vehicles")
    sticker_no = models.CharField(max_length=30, blank=True)
    notes = models.TextField(blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.plate

    @property
    def is_expired(self):
        return bool(self.valid_until and self.valid_until < timezone.localdate())

    @property
    def contact_name(self):
        if self.owner:
            return self.owner.display_name
        if self.sponsor and not self.owner_name:
            return f"{self.sponsor.display_name} (host)"
        return self.owner_name or "Unknown"

    @property
    def contact_phone(self):
        return (self.owner.phone if self.owner and self.owner.phone else "") or self.owner_phone or \
               (self.sponsor.phone if self.sponsor else "")

    @property
    def notify_user(self):
        return self.owner or self.sponsor


class Booking(models.Model):
    """A faculty slot booking, or a visitor/vendor time-limited pass (with QR)."""

    class Kind(models.TextChoices):
        FACULTY = "faculty", "Faculty slot booking"
        VISITOR = "visitor", "Visitor / parent pass"
        VENDOR = "vendor", "Vendor pass"

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        CHECKED_IN = "checked_in", "Checked in"
        COMPLETED = "completed", "Completed"
        EXPIRED = "expired", "Expired"
        NO_SHOW = "no_show", "No-show (released)"

    HOLDING = (Status.PENDING, Status.APPROVED, Status.CHECKED_IN)

    code = models.CharField(max_length=12, unique=True, default=_code)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="bookings")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    lot = models.ForeignKey(ParkingLot, on_delete=models.PROTECT, related_name="bookings")
    slot = models.ForeignKey(Slot, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings")
    vehicle_plate = models.CharField(max_length=15)
    vehicle_desc = models.CharField(max_length=80, blank=True)
    purpose = models.CharField(max_length=200, blank=True)
    host_name = models.CharField(max_length=120, blank=True, help_text="Person/department being visited")
    gate = models.CharField(max_length=60, default="Main Gate")
    start = models.DateTimeField()
    end = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=200, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-start",)
        indexes = [models.Index(fields=["lot", "status", "start", "end"])]

    def __str__(self):
        return f"{self.code} {self.vehicle_plate}"

    @property
    def qr_token(self):
        if self.status not in (self.Status.APPROVED, self.Status.CHECKED_IN):
            return None
        return signing.dumps({"b": self.pk, "c": self.code, "p": self.vehicle_plate}, salt="tsp-pass", compress=True)

    @staticmethod
    def parse_token(token):
        return signing.loads(token, salt="tsp-pass")


class Camera(models.Model):
    class Kind(models.TextChoices):
        GATE = "gate", "Gate ANPR camera"
        LOT = "lot", "Parking occupancy camera"

    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=5, choices=Kind.choices)
    gate = models.CharField(max_length=60, blank=True, default="Main Gate")
    direction = models.CharField(max_length=3, choices=(("in", "Entry"), ("out", "Exit")), default="in")
    lot = models.ForeignKey(ParkingLot, null=True, blank=True, on_delete=models.CASCADE, related_name="cameras")
    key = models.CharField(max_length=40, unique=True, default=_camera_key)
    stream_url = models.CharField(max_length=300, blank=True, help_text="Optional RTSP/HTTP/MJPEG url for camera_worker")
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_image = models.ImageField(upload_to="cameras/", null=True, blank=True)
    last_result = models.JSONField(blank=True, null=True)

    def __str__(self):
        return self.name

    @property
    def online(self):
        return bool(self.last_seen_at and timezone.now() - self.last_seen_at < timedelta(seconds=60))


class GateEvent(models.Model):
    class Decision(models.TextChoices):
        AUTHORIZED = "authorized", "Authorized"
        UNAUTHORIZED = "unauthorized", "Unauthorized"
        EXPIRED = "expired", "Expired"
        REVIEW = "needs_review", "Needs review"

    class Source(models.TextChoices):
        CAMERA = "camera", "Fixed camera"
        PHONE = "phone", "Phone camera node"
        WEB = "web", "Dashboard webcam/upload"
        GUARD = "guard", "Guard app"
        MANUAL = "manual", "Manual entry"
        QR = "qr", "QR pass scan"

    gate = models.CharField(max_length=60, default="Main Gate")
    direction = models.CharField(max_length=3, default="in")
    camera = models.ForeignKey(Camera, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.CAMERA)
    plate = models.CharField(max_length=15, blank=True)
    plate_raw = models.CharField(max_length=30, blank=True)
    confidence = models.FloatField(default=0)
    valid_format = models.BooleanField(default=False)
    decision = models.CharField(max_length=13, choices=Decision.choices)
    reason = models.CharField(max_length=200, blank=True)
    candidates = models.JSONField(blank=True, null=True)
    vehicle = models.ForeignKey(Vehicle, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    image = models.ImageField(upload_to="gate/%Y/%m/%d/", null=True, blank=True)
    final_decision = models.CharField(max_length=13, choices=Decision.choices, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=200, blank=True)
    hits = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def effective_decision(self):
        return self.final_decision or self.decision


class Flag(models.Model):
    class Reason(models.TextChoices):
        NO_PARKING = "no_parking", "Parked in no-parking / flagged zone"
        BLOCKING = "blocking", "Blocking access / another vehicle"
        WRONG_SLOT = "wrong_slot", "Parked in wrong / reserved slot"
        IDENTITY = "identity", "Identity confirmation required"
        UNAUTHORIZED = "unauthorized", "Unauthorized vehicle on campus"
        LIGHTS = "lights", "Lights on / window open"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        NOTIFIED = "notified", "Owner notified"
        ACKNOWLEDGED = "acknowledged", "Owner acknowledged"
        RESOLVED = "resolved", "Resolved"
        CANCELLED = "cancelled", "Cancelled"

    vehicle = models.ForeignKey(Vehicle, null=True, blank=True, on_delete=models.SET_NULL, related_name="flags")
    plate = models.CharField(max_length=15)
    lot = models.ForeignKey(ParkingLot, null=True, blank=True, on_delete=models.SET_NULL, related_name="flags")
    location = models.CharField(max_length=120, blank=True)
    reason = models.CharField(max_length=13, choices=Reason.choices, default=Reason.NO_PARKING)
    note = models.CharField(max_length=300, blank=True)
    image = models.ImageField(upload_to="flags/", null=True, blank=True)
    status = models.CharField(max_length=13, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="flags_created")
    created_at = models.DateTimeField(auto_now_add=True)
    notified_count = models.PositiveSmallIntegerField(default=0)
    last_notified_at = models.DateTimeField(null=True, blank=True)
    owner_reply = models.CharField(max_length=200, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resolution_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_open(self):
        return self.status in (self.Status.OPEN, self.Status.NOTIFIED, self.Status.ACKNOWLEDGED)


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, default="system")
    title = models.CharField(max_length=120)
    body = models.TextField(blank=True)
    data = models.JSONField(blank=True, null=True)
    urgent = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)


class AuditLog(models.Model):
    """Append-only audit trail of approvals, revocations, reviews, flags, contact reveals."""
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=40)
    target_type = models.CharField(max_length=30, blank=True)
    target_id = models.CharField(max_length=40, blank=True)
    summary = models.CharField(max_length=250, blank=True)
    details = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Audit log entries are append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit log entries are append-only")
