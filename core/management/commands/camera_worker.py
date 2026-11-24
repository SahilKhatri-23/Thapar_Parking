"""
Pull frames from an RTSP / HTTP(MJPEG) / USB camera and feed them to the system.

  python manage.py camera_worker --key <camera key> --source rtsp://user:pass@ip:554/stream --interval 3
  python manage.py camera_worker --key <key> --source 0            # USB webcam
  python manage.py camera_worker --key <key> --source http://PHONE_IP:8080/video   # Android "IP Webcam" app

Gate cameras: every frame goes through plate recognition; a gate event is created only when a plate is read
(duplicates within GATE_DEDUP_SECONDS are merged). Lot cameras: vehicle counting updates occupancy.
"""
import time

import cv2
from django.core.management.base import BaseCommand, CommandError

from core import services as S
from core.models import Camera, GateEvent


class Command(BaseCommand):
    help = "Read frames from an IP/USB camera and run ANPR or occupancy detection"

    def add_arguments(self, p):
        p.add_argument("--key", required=True, help="Camera key from the portal Cameras page")
        p.add_argument("--source", help="RTSP/HTTP url or webcam index (defaults to camera.stream_url)")
        p.add_argument("--interval", type=float, default=None, help="Seconds between processed frames "
                                                                     "(default 2 for gate, 30 for lot)")

    def handle(self, *a, **o):
        cam = Camera.objects.filter(key=o["key"]).select_related("lot").first()
        if not cam:
            raise CommandError("Unknown camera key")
        src = o["source"] or cam.stream_url
        if src is None or src == "":
            raise CommandError("Give --source or set stream_url on the camera")
        src = int(src) if str(src).isdigit() else src
        interval = o["interval"] or (2.0 if cam.kind == Camera.Kind.GATE else 30.0)
        self.stdout.write(self.style.SUCCESS(f"[{cam.name}] reading {src} every {interval}s. Ctrl+C to stop."))

        # If source is an HTTP snapshot/capture endpoint (e.g. ESP32 /capture)
        is_http = isinstance(src, str) and (src.startswith("http://") or src.startswith("https://"))
        if is_http and ("capture" in src or "snapshot" in src or "jpg" in src or "jpeg" in src):
            import urllib.request
            while True:
                try:
                    cam.refresh_from_db()
                    if not cam.is_active:
                        time.sleep(interval)
                        continue
                    req = urllib.request.Request(src, headers={"User-Agent": "TSP-CameraWorker/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        jpg_bytes = resp.read()
                    if cam.kind == Camera.Kind.GATE:
                        r = S.process_gate_image(jpg_bytes, gate=cam.gate or "Main Gate", direction=cam.direction,
                                                 source=GateEvent.Source.CAMERA, camera=cam)
                        ev = r.get("event")
                        if ev:
                            self.stdout.write(f"  {ev.plate or '?':12s} {ev.decision:13s} {ev.confidence:.0%}  {ev.reason}")
                    else:
                        r = S.process_lot_image(cam.lot, jpg_bytes, camera=cam)
                        self.stdout.write(f"  {cam.lot.name}: {r['four_wheelers']} vehicles, {r['available']} available")
                except Exception as e:
                    self.stderr.write(f"  error: {e}")
                time.sleep(interval)
            return

        cap = None
        last = 0.0
        while True:
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(src)
                if not cap.isOpened():
                    self.stderr.write("  cannot open source, retrying in 5s")
                    time.sleep(5)
                    continue
            ok, frame = cap.read()  # keep reading so the buffer stays fresh
            if not ok:
                cap.release()
                cap = None
                time.sleep(1)
                continue
            if time.time() - last < interval:
                continue
            last = time.time()
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
            if not ok:
                continue
            try:
                cam.refresh_from_db()
                if not cam.is_active:
                    time.sleep(interval)
                    continue
                if cam.kind == Camera.Kind.GATE:
                    r = S.process_gate_image(jpg.tobytes(), gate=cam.gate or "Main Gate", direction=cam.direction,
                                             source=GateEvent.Source.CAMERA, camera=cam)
                    ev = r.get("event")
                    if ev:
                        self.stdout.write(f"  {ev.plate or '?':12s} {ev.decision:13s} {ev.confidence:.0%}  {ev.reason}")
                else:
                    r = S.process_lot_image(cam.lot, jpg.tobytes(), camera=cam)
                    self.stdout.write(f"  {cam.lot.name}: {r['four_wheelers']} vehicles, {r['available']} available")
            except Exception as e:  # keep running
                self.stderr.write(f"  error: {e}")
