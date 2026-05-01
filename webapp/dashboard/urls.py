from django.urls import path
from dashboard import views
from dashboard import api

urlpatterns = [
    path("", views.index, name="index"),
    path("diag/", views.diag, name="diag"),
    path("api/sources/", api.api_sources, name="api_sources"),
    path("api/preview/", api.api_preview, name="api_preview"),
    path("api/files/", api.api_files, name="api_files"),
    path("api/upload/", api.api_upload, name="api_upload"),
    path("api/status/", api.api_status, name="api_status"),
]
