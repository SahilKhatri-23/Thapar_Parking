import random
from datetime import timedelta

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, role=User.Role.ADMIN, is_email_verified=True)
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        GUARD = "guard", "Guard"
        FACULTY = "faculty", "Faculty"
        STUDENT = "student", "Student"
        VENDOR = "vendor", "Vendor"
        VISITOR = "visitor", "Visitor / Parent"

    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STUDENT)
    department = models.CharField(max_length=120, blank=True)
    roll_or_emp_no = models.CharField(max_length=40, blank=True)
    organisation = models.CharField(max_length=120, blank=True, help_text="Vendor company / visitor purpose")
    is_email_verified = models.BooleanField(default=False)
    # faculty self-registrations must be confirmed by an admin before they can book slots
    role_verified = models.BooleanField(default=True)
    gate = models.CharField(max_length=60, blank=True, help_text="Guard's assigned gate")

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN or self.is_superuser

    @property
    def is_guard(self):
        return self.role == self.Role.GUARD

    @property
    def is_staff_role(self):
        return self.is_admin or self.is_guard

    @property
    def is_member(self):
        return self.role in (self.Role.FACULTY, self.Role.STUDENT)

    @property
    def is_guest(self):
        return self.role in (self.Role.VENDOR, self.Role.VISITOR)

    @property
    def display_name(self):
        return self.full_name or self.email.split("@")[0]

    def masked_phone(self):
        p = self.phone or ""
        if len(p) < 6:
            return "•" * len(p)
        return p[:3] + "•" * (len(p) - 5) + p[-2:]

    def __str__(self):
        return f"{self.display_name} <{self.email}>"


class EmailOTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="otps")
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    used = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)

    @classmethod
    def issue(cls, user):
        cls.objects.filter(user=user, used=False).update(used=True)
        return cls.objects.create(user=user, code=f"{random.SystemRandom().randint(0, 999999):06d}")

    def is_valid(self, ttl_min=10):
        return not self.used and self.attempts < 5 and timezone.now() - self.created_at < timedelta(minutes=ttl_min)
