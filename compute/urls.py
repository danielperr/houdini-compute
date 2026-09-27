from django.urls import path

from . import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("healthz", views.healthz, name="healthz"),
]
