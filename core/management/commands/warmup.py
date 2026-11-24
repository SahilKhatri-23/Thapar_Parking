from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Download and load the ONNX vision models once (plate detector, plate OCR, vehicle detector)"

    def handle(self, *a, **o):
        import numpy as np
        from vision import plates, vehicles
        self.stdout.write("Loading plate detector + OCR…")
        plates.get_alpr()
        plates.read_plates(np.zeros((480, 640, 3), dtype=np.uint8))
        self.stdout.write("Loading vehicle detector…")
        vehicles.detect_vehicles(np.zeros((480, 640, 3), dtype=np.uint8))
        self.stdout.write(self.style.SUCCESS("Vision models ready (cached for offline use)."))
