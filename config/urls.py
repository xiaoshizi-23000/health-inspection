from django.contrib import admin
from django.urls import include, path

from inspection import web

urlpatterns = [
    path("", web.dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("api/", include("inspection.urls")),
    path("web/task/<str:action>/", web.trigger_task, name="trigger-task"),
    path("web/status/", web.worker_status, name="worker-status"),
    path("web/start/", web.start_worker, name="start-worker"),
    path("web/logs/", web.worker_logs, name="worker-logs"),
]
