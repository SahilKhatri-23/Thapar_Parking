from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from accounts.models import User
from vision.plates import pretty_plate
from .models import AuditLog, Booking, Camera, Flag, GateEvent, Notification, ParkingLot, Vehicle


def abs_url(request, f):
    if not f:
        return None
    try:
        url = f.url
    except ValueError:
        return None
    return request.build_absolute_uri(url) if request else url


class UserSerializer(serializers.ModelSerializer):
    role_label = serializers.CharField(source="get_role_display", read_only=True)
    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "display_name", "phone", "role", "role_label", "department",
                  "roll_or_emp_no", "organisation", "is_email_verified", "role_verified", "gate", "is_active",
                  "date_joined")
        read_only_fields = ("email", "role", "is_email_verified", "role_verified", "is_active", "date_joined")


class AdminUserSerializer(UserSerializer):
    class Meta(UserSerializer.Meta):
        read_only_fields = ("email", "date_joined")


class VehicleSerializer(serializers.ModelSerializer):
    pretty = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    category_label = serializers.CharField(source="get_category_display", read_only=True)
    type_label = serializers.CharField(source="get_vehicle_type_display", read_only=True)
    owner_display = serializers.CharField(source="contact_name", read_only=True)
    owner_email = serializers.SerializerMethodField()
    home_lot_name = serializers.CharField(source="home_lot.name", read_only=True, default=None)
    expired = serializers.BooleanField(source="is_expired", read_only=True)
    open_flags = serializers.SerializerMethodField()

    class Meta:
        model = Vehicle
        fields = ("id", "plate", "pretty", "vehicle_type", "type_label", "make", "model_name", "colour", "category",
                  "category_label", "status", "status_label", "valid_until", "expired", "home_lot", "home_lot_name",
                  "sticker_no", "notes", "owner", "owner_display", "owner_email", "owner_name", "owner_phone",
                  "sponsor", "approved_at", "created_at", "open_flags")
        read_only_fields = ("status", "approved_at", "created_at", "owner")

    def get_pretty(self, o):
        return pretty_plate(o.plate)

    def get_owner_email(self, o):
        req = self.context.get("request")
        if req and getattr(req.user, "is_admin", False) and o.owner:
            return o.owner.email
        return None

    def get_open_flags(self, o):
        return o.flags.filter(status__in=["open", "notified", "acknowledged"]).count()


class LotSerializer(serializers.ModelSerializer):
    available = serializers.SerializerMethodField()
    reserved = serializers.SerializerMethodField()
    stale = serializers.BooleanField(source="is_stale", read_only=True)
    roles = serializers.ListField(read_only=True)
    annotated_url = serializers.SerializerMethodField()
    snapshot_url = serializers.SerializerMethodField()
    source_label = serializers.CharField(source="get_occupancy_source_display", read_only=True)
    occupancy_pct = serializers.SerializerMethodField()
    can_book = serializers.SerializerMethodField()

    class Meta:
        model = ParkingLot
        fields = ("id", "name", "code", "description", "latitude", "longitude", "total_slots", "visitor_slots",
                  "two_wheeler_capacity", "allowed_roles", "roles", "bookable", "is_active", "detection_zone",
                  "occupied", "occupied_2w", "available", "reserved", "occupancy_pct", "occupancy_source",
                  "source_label", "occupancy_updated_at", "stale", "annotated_url", "snapshot_url", "sort_order",
                  "can_book")
        read_only_fields = ("occupied", "occupied_2w", "occupancy_source", "occupancy_updated_at")

    def get_available(self, o):
        return o.available()

    def get_reserved(self, o):
        return o.active_reservations()

    def get_occupancy_pct(self, o):
        if not o.total_slots:
            return 100
        return round(100 * (o.total_slots - o.available()) / o.total_slots)

    def get_annotated_url(self, o):
        return abs_url(self.context.get("request"), o.last_annotated)

    def get_snapshot_url(self, o):
        return abs_url(self.context.get("request"), o.last_snapshot)

    def get_can_book(self, o):
        req = self.context.get("request")
        u = getattr(req, "user", None)
        if not u or not u.is_authenticated:
            return False
        if u.role == "faculty":
            return o.bookable and "faculty" in o.roles and u.role_verified
        return u.role in ("vendor", "visitor", "admin") and (u.role in o.roles or u.role == "admin")


class BookingSerializer(serializers.ModelSerializer):
    pretty_plate = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    lot_name = serializers.CharField(source="lot.name", read_only=True)
    lot_lat = serializers.FloatField(source="lot.latitude", read_only=True)
    lot_lng = serializers.FloatField(source="lot.longitude", read_only=True)
    slot_label = serializers.CharField(source="slot.label", read_only=True, default=None)
    holder = serializers.CharField(source="user.display_name", read_only=True)
    holder_email = serializers.CharField(source="user.email", read_only=True)
    holder_phone = serializers.SerializerMethodField()
    organisation = serializers.CharField(source="user.organisation", read_only=True)
    qr_token = serializers.SerializerMethodField()
    is_live = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = ("id", "code", "kind", "kind_label", "status", "status_label", "lot", "lot_name", "lot_lat",
                  "lot_lng", "slot", "slot_label", "vehicle_plate", "pretty_plate", "vehicle_desc", "purpose",
                  "host_name", "gate", "start", "end", "holder", "holder_email", "holder_phone", "organisation",
                  "approved_at", "reject_reason", "checked_in_at", "checked_out_at", "created_at", "qr_token", "is_live")

    def get_pretty_plate(self, o):
        return pretty_plate(o.vehicle_plate)

    def get_holder_phone(self, o):
        req = self.context.get("request")
        if req and (req.user.is_staff_role or req.user == o.user):
            return o.user.phone
        return None

    def get_qr_token(self, o):
        req = self.context.get("request")
        if req and (req.user == o.user or getattr(req.user, "is_admin", False)):
            return o.qr_token
        return None

    def get_is_live(self, o):
        now = timezone.now()
        return o.status in ("approved", "checked_in") and o.start <= now + timedelta(minutes=30) and o.end >= now


class GateEventSerializer(serializers.ModelSerializer):
    pretty = serializers.SerializerMethodField()
    decision_label = serializers.CharField(source="get_decision_display", read_only=True)
    effective = serializers.CharField(source="effective_decision", read_only=True)
    image_url = serializers.SerializerMethodField()
    vehicle_info = serializers.SerializerMethodField()
    booking_code = serializers.CharField(source="booking.code", read_only=True, default=None)
    booking_holder = serializers.CharField(source="booking.user.display_name", read_only=True, default=None)
    reviewed_by_name = serializers.CharField(source="reviewed_by.display_name", read_only=True, default=None)
    camera_name = serializers.CharField(source="camera.name", read_only=True, default=None)
    source_label = serializers.CharField(source="get_source_display", read_only=True)

    class Meta:
        model = GateEvent
        fields = ("id", "gate", "direction", "source", "source_label", "camera_name", "plate", "pretty", "plate_raw",
                  "confidence", "valid_format", "decision", "decision_label", "final_decision", "effective", "reason",
                  "candidates", "vehicle", "vehicle_info", "booking", "booking_code", "booking_holder", "image_url",
                  "reviewed_by_name", "reviewed_at", "review_note", "hits", "created_at", "updated_at")

    def get_pretty(self, o):
        return pretty_plate(o.plate)

    def get_image_url(self, o):
        return abs_url(self.context.get("request"), o.image)

    def get_vehicle_info(self, o):
        v = o.vehicle
        if not v:
            return None
        return {"owner": v.contact_name, "category": v.get_category_display(), "make": v.make,
                "model": v.model_name, "colour": v.colour, "status": v.status}


class FlagSerializer(serializers.ModelSerializer):
    pretty = serializers.SerializerMethodField()
    reason_label = serializers.CharField(source="get_reason_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    lot_name = serializers.CharField(source="lot.name", read_only=True, default=None)
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True, default=None)
    owner_name = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    has_app_owner = serializers.SerializerMethodField()

    class Meta:
        model = Flag
        fields = ("id", "plate", "pretty", "vehicle", "lot", "lot_name", "location", "reason", "reason_label", "note",
                  "status", "status_label", "created_by_name", "created_at", "notified_count", "last_notified_at",
                  "owner_reply", "acknowledged_at", "resolved_at", "resolution_note", "owner_name", "image_url",
                  "has_app_owner")

    def get_pretty(self, o):
        return pretty_plate(o.plate)

    def get_owner_name(self, o):
        return o.vehicle.contact_name if o.vehicle else None

    def get_image_url(self, o):
        return abs_url(self.context.get("request"), o.image)

    def get_has_app_owner(self, o):
        return bool(o.vehicle and o.vehicle.notify_user)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ("id", "kind", "title", "body", "data", "urgent", "read_at", "created_at")


class AuditSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ("id", "actor_name", "action", "target_type", "target_id", "summary", "details", "created_at")

    def get_actor_name(self, o):
        return o.actor.display_name if o.actor else "system"


class CameraSerializer(serializers.ModelSerializer):
    lot_name = serializers.CharField(source="lot.name", read_only=True, default=None)
    online = serializers.BooleanField(read_only=True)
    last_image_url = serializers.SerializerMethodField()
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = Camera
        fields = ("id", "name", "kind", "kind_label", "gate", "direction", "lot", "lot_name", "key", "stream_url",
                  "is_active", "last_seen_at", "online", "last_result", "last_image_url")
        read_only_fields = ("key", "last_seen_at", "last_result")

    def get_last_image_url(self, o):
        return abs_url(self.context.get("request"), o.last_image)
