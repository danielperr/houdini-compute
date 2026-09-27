from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render

from . import overview as ov
from .recommend import DEFAULT_PROFILE, DEFAULT_WINDOW, PROFILES, WINDOWS


def _choice(value, allowed, default):
    return value if value in allowed else default


def overview(request):
    resource = _choice(request.GET.get("resource"), ov.RESOURCES, "cpu")
    profile = _choice(request.GET.get("profile"), PROFILES, DEFAULT_PROFILE)
    try:
        window = int(request.GET.get("window", DEFAULT_WINDOW))
    except ValueError:
        window = DEFAULT_WINDOW
    window = _choice(window, WINDOWS, DEFAULT_WINDOW)
    cluster = request.GET.get("cluster") or None

    data = ov.overview(resource, profile, window, cluster)
    label, unit, _ = ov.RESOURCES[resource]
    context = {
        **data,
        "filters": {"resource": resource, "profile": profile, "window": window, "cluster": cluster or ""},
        "resources": {k: v[0] for k, v in ov.RESOURCES.items()},
        "profiles": PROFILES.values(),
        "windows": WINDOWS,
        "resource_label": label,
        "unit": unit,
        "target_reserve": settings.TARGET_RESERVE,
    }
    template = "compute/_overview_body.html" if request.headers.get("HX-Request") else "compute/overview.html"
    return render(request, template, context)


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
