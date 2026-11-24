from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Flag, GateEvent


class Command(BaseCommand):
    help = "Delete gate/flag images older than PARKING['IMAGE_RETENTION_DAYS'] (privacy retention policy)"

    def handle(self, *a, **o):
        cutoff = timezone.now() - timedelta(days=settings.PARKING["IMAGE_RETENTION_DAYS"])
        n = 0
        for model in (GateEvent, Flag):
            for obj in model.objects.filter(created_at__lt=cutoff).exclude(image=""):
                obj.image.delete(save=True)
                n += 1
        self.stdout.write(self.style.SUCCESS(f"Removed {n} images older than {cutoff:%d %b %Y}"))
