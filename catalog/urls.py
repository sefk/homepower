from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health, name="health"),
    path("solar/", views.solar, name="solar"),
    path("solar/data.json", views.solar_data, name="solar_data"),
]
