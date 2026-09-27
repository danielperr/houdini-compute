"""Django settings for Houdini Compute.

Everything that differs between environments comes from environment variables,
so the same image runs on a laptop (SQLite) and in the cluster (Postgres).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).lower() in ("1", "true", "yes", "on")


def env_float(name, default):
    return float(env(name, default))


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

if not DEBUG:
    if SECRET_KEY == "dev-only-insecure-key":
        raise RuntimeError("Set DJANGO_SECRET_KEY when DJANGO_DEBUG is false")
    # OpenShift routes terminate TLS and forward the original scheme.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "compute",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "houdini.urls"

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

WSGI_APPLICATION = "houdini.wsgi.application"

if env("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": env("POSTGRES_HOST"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "NAME": env("POSTGRES_DB", "houdini"),
            "USER": env("POSTGRES_USER", "houdini"),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Houdini Compute
# ---------------------------------------------------------------------------

# Thanos Querier base URL, e.g. https://thanos-querier.monitoring.svc:9091
THANOS_URL = env("THANOS_URL", "http://localhost:9090")
THANOS_TOKEN = env("THANOS_TOKEN", "")  # bearer token, optional
THANOS_CA_BUNDLE = env("THANOS_CA_BUNDLE", "")  # path to CA file, optional
THANOS_VERIFY_TLS = env_bool("THANOS_VERIFY_TLS", True)
THANOS_TIMEOUT_SECONDS = env_float("THANOS_TIMEOUT_SECONDS", 120)

# External label that tells clusters apart in the central Thanos.
CLUSTER_LABEL = env("CLUSTER_LABEL", "cluster")

# Label that carries the node name on node-exporter metrics.
NODE_EXPORTER_NODE_LABEL = env("NODE_EXPORTER_NODE_LABEL", "instance")

# Namespace label that holds the tenant, as it appears on the *namespace*
# (e.g. "tenant" or "company.io/tenant"). kube-state-metrics exposes it as
# label_<sanitized name> on kube_namespace_labels.
TENANT_LABEL = env("TENANT_LABEL", "tenant")

# Namespaces matching this regex are platform namespaces, shown separately.
PLATFORM_NAMESPACE_REGEX = env(
    "PLATFORM_NAMESPACE_REGEX", r"^(openshift|kube)(-.*)?$|^default$"
)

# Share of worker allocatable that should stay unrequested as a reserve.
TARGET_RESERVE = env_float("TARGET_RESERVE", 0.20)
