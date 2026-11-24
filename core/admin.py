from django.contrib import admin

from .models import AuditLog, Booking, Camera, Flag, GateEvent, Notification, ParkingLot, Slot, Vehicle


@admin.register(ParkingLot)
class LotAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "total_slots", "visitor_slots", "occupied", "occupancy_source", "is_active")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        obj.sync_slots()


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("plate", "owner", "owner_name", "category", "status", "valid_until", "home_lot")
    list_filter = ("status", "category", "vehicle_type")
    search_fields = ("plate", "owner__email", "owner__full_name", "owner_name")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("code", "user", "kind", "lot", "slot", "vehicle_plate", "start", "end", "status")
    list_filter = ("status", "kind", "lot")
    search_fields = ("code", "vehicle_plate", "user__email")


@admin.register(GateEvent)
class GateEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "gate", "direction", "plate", "confidence", "decision", "final_decision", "source")
    list_filter = ("decision", "gate", "source")
    search_fields = ("plate",)


@admin.register(Flag)
class FlagAdmin(admin.ModelAdmin):
    list_display = ("plate", "reason", "lot", "status", "notified_count", "created_at")
    list_filter = ("status", "reason")


@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "gate", "lot", "key", "is_active", "last_seen_at")


@admin.register(AuditLog)
class AuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "summary")
    list_filter = ("action",)
    search_fields = ("summary",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Slot)
admin.site.register(Notification)
