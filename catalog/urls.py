from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health, name="health"),
    path("solar/", views.solar, name="solar"),
    path("solar/data.json", views.solar_data, name="solar_data"),
    path("grid/", views.grid, name="grid"),
    path("grid/data.json", views.grid_data, name="grid_data"),
    path("trueup/", views.trueup, name="trueup"),
    path("trueup/data.json", views.trueup_data, name="trueup_data"),
    path("peak/", views.peak, name="peak"),
    path("peak/data.json", views.peak_data, name="peak_data"),
    path("costmap/", views.costmap, name="costmap"),
    path("costmap/data.json", views.costmap_data, name="costmap_data"),
]
