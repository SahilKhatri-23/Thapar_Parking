from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from core.models import Booking, Camera, GateEvent, ParkingLot, Vehicle
from core import services as S

PASSWORD = "Thapar@123"

LOTS = [
    # name, code, lat, lng, total, visitor, 2W, roles, description
    ("Main Gate Parking", "main-gate", 30.35269229496757, 76.37248511943653, 60, 15, 40,
     "faculty,student,visitor,vendor", "Next to the main gate. Visitor & vendor bays at the far end."),
    ("B Block Parking", "b-block", 30.35330296177362, 76.37044810027942, 40, 0, 30,
     "faculty,student", "Behind B Block academic building."),
    ("G Block Parking", "g-block", 30.353055998100306, 76.36945493407252, 45, 5, 30,
     "faculty,student,visitor", "Near G Block, close to the library side."),
    ("TSLAS Parking", "tslas", 30.356912795261987, 76.37250368437576, 30, 5, 20,
     "faculty,student,visitor", "Thapar School of Liberal Arts & Sciences."),
    ("COS Parking", "cos", 30.353699273772694, 76.36297823352935, 35, 5, 25,
     "faculty,student,visitor,vendor", "COS complex parking."),
    ("FRA/B/C Parking", "fr-abc", 30.353830233240263, 76.36037644363518, 50, 0, 40,
     "faculty,student", "Western campus parking for FR-A, FR-B, FR-C."),
]


class Command(BaseCommand):
    help = "Create demo lots, users, vehicles, cameras and bookings (safe to re-run)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete demo data first")

    def handle(self, *args, **o):
        if o["reset"]:
            GateEvent.objects.all().delete()
            Booking.objects.all().delete()
            Vehicle.objects.all().delete()
            Camera.objects.all().delete()
            ParkingLot.objects.all().delete()
            User.objects.filter(email__in=[u[0] for u in self.users()]).delete()

        lots = {}
        for i, (name, code, lat, lng, total, vis, tw, roles, desc) in enumerate(LOTS):
            lot, created = ParkingLot.objects.get_or_create(code=code, defaults=dict(
                name=name, latitude=lat, longitude=lng, total_slots=total, visitor_slots=vis,
                two_wheeler_capacity=tw, allowed_roles=roles, description=desc, sort_order=i))
            lot.sync_slots()
            lots[code] = lot

        users = {}
        for email, name, role, phone, extra in self.users():
            u = User.objects.filter(email=email).first()
            if not u:
                u = User.objects.create_user(email=email, password=PASSWORD, full_name=name, role=role, phone=phone,
                                             is_email_verified=True, role_verified=True, **extra)
                if role == "admin":
                    u.is_staff = True
                    u.is_superuser = True
                    u.save()
            users[role if role not in users else role + "2"] = u

        now = timezone.now()
        today = timezone.localdate()
        vehicles = [
            ("PB11CD4521", users["faculty"], None, "", "", "suv", "Hyundai", "Creta", "Black", "faculty", "active",
             today + timedelta(days=365), lots["g-block"]),
            ("PB11AK7788", users["student"], None, "", "", "car", "Maruti", "Swift", "White", "student", "active",
             today + timedelta(days=200), lots["main-gate"]),
            ("PB11BX2045", users["student"], None, "", "", "two_wheeler", "Honda", "Activa", "Grey", "student", "pending",
             None, None),
            ("CH01AZ5566", None, None, "Ramesh Kumar (Estate Office)", "+919876543210", "car", "Toyota", "Etios", "White",
             "staff", "active", today + timedelta(days=500), lots["main-gate"]),
            ("PB10GH3344", None, None, "Ankit Sharma", "+919812345678", "car", "Honda", "City", "Silver", "student",
             "suspended", today + timedelta(days=100), None),
            ("HR26DK8890", None, None, "Neha Verma", "+919811122233", "car", "Hyundai", "i20", "White", "faculty",
             "active", today - timedelta(days=5), None),
            ("PB65AB1234", users["faculty"], None, "", "", "car", "Volkswagen", "Polo", "Red", "faculty", "active",
             today + timedelta(days=365), lots["b-block"]),
            ("DL3CBK9087", None, users["faculty"], "Guest Lecturer", "+919900112233", "suv", "Toyota", "Fortuner",
             "White", "visitor", "active", today + timedelta(days=30), lots["tslas"]),
        ]
        for plate, owner, sponsor, oname, ophone, vt, make, model, colour, cat, st, valid, home in vehicles:
            Vehicle.objects.get_or_create(plate=plate, defaults=dict(
                owner=owner, sponsor=sponsor, owner_name=oname, owner_phone=ophone, vehicle_type=vt, make=make,
                model_name=model, colour=colour, category=cat, status=st, valid_until=valid, home_lot=home,
                approved_by=users["admin"] if st == "active" else None, approved_at=now if st == "active" else None))

        cams = [
            ("Main Gate - Entry ANPR", "gate", "Main Gate", "in", None),
            ("Main Gate - Exit ANPR", "gate", "Main Gate", "out", None),
            ("Main Gate Parking - Overview", "lot", "", "in", lots["main-gate"]),
            ("G Block Parking - Overview", "lot", "", "in", lots["g-block"]),
        ]
        for name, kind, gate, d, lot in cams:
            Camera.objects.get_or_create(name=name, defaults=dict(kind=kind, gate=gate, direction=d, lot=lot))

        if not Booking.objects.exists():
            vendor, visitor, faculty = users["vendor"], users["visitor"], users["faculty"]
            mg = lots["main-gate"]
            start = now - timedelta(minutes=20)
            S.create_booking(vendor, lot=mg, start=start, end=start + timedelta(hours=4), plate="PB11GV6612",
                             purpose="Canteen supplies delivery", host_name="Hostel Mess Office",
                             vehicle_desc="Tata Ace (white)")
            bk = Booking.objects.get(vehicle_plate="PB11GV6612")
            S.set_booking_status(bk, users["admin"], "approve")
            tomorrow = (timezone.localtime(now) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
            S.create_booking(visitor, lot=mg, start=tomorrow, end=tomorrow + timedelta(hours=3), plate="PB11AL3030",
                             purpose="Parent visit - hostel", host_name="Hostel J", vehicle_desc="Maruti Ertiga (blue)")
            S.create_booking(faculty, lot=lots["g-block"], start=tomorrow - timedelta(hours=1),
                             end=tomorrow + timedelta(hours=7), plate="PB11CD4521")

        if not GateEvent.objects.exists():
            for plate, conf, d in [("PB11CD4521", 0.97, "in"), ("CH01AZ5566", 0.93, "in"), ("PB10GH3344", 0.95, "in"),
                                   ("HR26DK8890", 0.91, "in"), ("PB11AK7788", 0.64, "in"), ("UP16BT4402", 0.90, "in")]:
                S.record_gate_event(plate=plate, raw=plate, confidence=conf, gate="Main Gate", direction=d,
                                    source=GateEvent.Source.CAMERA)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {ParkingLot.objects.count()} lots, {User.objects.count()} users, {Vehicle.objects.count()} vehicles, "
            f"{Camera.objects.count()} cameras. Demo password for all accounts: {PASSWORD}"))
        for email, _n, role, *_ in self.users():
            self.stdout.write(f"   {role:8s} {email}")

    @staticmethod
    def users():
        return [
            ("admin@thapar.edu", "Parking Office Admin", "admin", "+919800000001", {"department": "Estate Office"}),
            ("guard@thapar.edu", "Gurpreet Singh (Main Gate)", "guard", "+919800000002", {"gate": "Main Gate"}),
            ("patrol@thapar.edu", "Harjit Kaur (Patrol)", "guard", "+919800000003", {"gate": "Main Gate"}),
            ("faculty@thapar.edu", "Dr. Ananya Mehta", "faculty", "+919800000004",
             {"department": "Computer Science & Engg.", "roll_or_emp_no": "EMP1043"}),
            ("student@thapar.edu", "Arjun Malhotra", "student", "+919800000005",
             {"department": "ECE", "roll_or_emp_no": "102203456"}),
            ("vendor@example.com", "Sukhwinder (Vendor)", "vendor", "+919800000006",
             {"organisation": "Punjab Fresh Supplies Pvt Ltd"}),
            ("visitor@example.com", "Mrs. Kavita Sharma", "visitor", "+919800000007", {"organisation": "Parent"}),
        ]
