from django.urls import include, path

from collectors import eagle

urlpatterns = [
    path("", include("catalog.urls")),
    path("ingest/eagle/", eagle.ingest, name="ingest_eagle"),
]
