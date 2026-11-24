"""
Thapar Smart Parking - Django settings (local / LAN deployment).
Everything runs on your machine: SQLite, local ONNX models, console email.
"""
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key, default=None):
    return os.environ.get(key, default)


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me-thapar-smart-parking-7f3c2a")
DEBUG = env("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]  # LAN access from phones (runserver 0.0.0.0:8000)
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "accounts",
    "core",
    "web",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}},
]
LOGIN_URL = "/portal/login/"

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DATA_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------ API
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": None,
    "DATETIME_FORMAT": "iso-8601",
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}
CORS_ALLOW_ALL_ORIGINS = True

# ------------------------------------------------------------------ Email
# Console backend prints OTPs / alerts in the server terminal. To send real mail
# set EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend and SMTP vars.
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "Thapar Smart Parking <no-reply@thapar.edu>")

# ------------------------------------------------------------------ App rules
PARKING = {
    "ALLOWED_DOMAINS": ["thapar.edu"],          # campus sign-in domains
    "SHOW_OTP_IN_RESPONSE": env("SHOW_OTP", "1") == "1",  # demo convenience on localhost
    "OTP_TTL_MIN": 10,
    "GATE_AUTO_CONF": 0.80,         # below this an OCR read always needs guard review
    "GATE_DEDUP_SECONDS": 45,       # same plate at same gate within this window = one event
    "BOOKING_GRACE_MIN": 30,        # pass valid this long before start; no-show released after
    "BOOKING_MAX_HOURS": 12,
    "VENDOR_AUTO_APPROVE": False,   # vendor/visitor bookings need admin approval
    "ALERT_COOLDOWN_MIN": 10,       # min gap between alerts for the same vehicle
    "ALERT_MAX_PER_FLAG": 3,
    "IMAGE_RETENTION_DAYS": 30,
    "OCCUPANCY_STALE_MIN": 30,
}

# ------------------------------------------------------------------ Vision (local ONNX models)
VISION = {
    # licence plate: YOLOv9 detector + CCT OCR (fast-alpr / open-image-models / fast-plate-ocr)
    "PLATE_DETECTOR": env("PLATE_DETECTOR", "yolo-v9-s-608-license-plate-end2end"),
    "PLATE_DET_CONF": 0.30,
    "PLATE_OCR": env("PLATE_OCR", "cct-s-v2-global-model"),
    "EASYOCR_FALLBACK": True,       # used only if `pip install easyocr` was done
    # vehicles: RF-DETR COCO. Options: rf-detr-nano-384-coco | rf-detr-small-512-coco |
    #           rf-detr-medium-576-coco | rf-detr-large-704-coco (most accurate, ~2s CPU)
    "VEHICLE_MODEL": env("VEHICLE_MODEL", "rf-detr-medium-576-coco"),
    "MAX_SIDE": 1920,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "vision": {"handlers": ["console"], "level": "INFO"},
        "core": {"handlers": ["console"], "level": "INFO"},
    },
}
