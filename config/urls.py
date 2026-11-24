from django.contrib import admin
from django.urls import include, path

from inspection import web, web_api

urlpatterns = [
    path("", web.dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("api/", include("inspection.urls")),

    # 控制台：后台服务调度
    path("web/task/<str:action>/", web.trigger_task, name="trigger-task"),
    path("web/status/", web.worker_status, name="worker-status"),
    path("web/start/", web.start_worker, name="start-worker"),
    path("web/logs/", web.worker_logs, name="worker-logs"),

    # 控制台：CRUD 与查询
    path("web/services/", web_api.service_list, name="web-services"),
    path("web/services/create/", web_api.service_create, name="web-service-create"),
    path("web/services/<int:pk>/update/", web_api.service_update, name="web-service-update"),
    path("web/services/<int:pk>/toggle/", web_api.service_toggle, name="web-service-toggle"),
    path("web/services/<int:pk>/delete/", web_api.service_delete, name="web-service-delete"),
    path("web/teams/", web_api.team_list, name="web-teams"),
    path("web/teams/save/", web_api.team_save, name="web-team-save"),
    path("web/teams/<int:pk>/save/", web_api.team_save, name="web-team-edit"),
    path("web/teams/<int:pk>/delete/", web_api.team_delete, name="web-team-delete"),
    path("web/environments/", web_api.environment_list, name="web-envs"),
    path("web/environments/save/", web_api.environment_save, name="web-env-save"),
    path("web/environments/<int:pk>/save/", web_api.environment_save, name="web-env-edit"),
    path("web/environments/<int:pk>/delete/", web_api.environment_delete, name="web-env-delete"),
    path("web/checks/", web_api.check_list, name="web-checks"),
    path("web/stats/", web_api.stats_by_date, name="web-stats"),
]
