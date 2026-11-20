from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DailyHealthStatViewSet,
    EnvironmentViewSet,
    HealthCheckLogViewSet,
    ServiceViewSet,
    TeamViewSet,
    ping,
)

router = DefaultRouter()
router.register("teams", TeamViewSet, basename="team")
router.register("environments", EnvironmentViewSet, basename="environment")
router.register("services", ServiceViewSet, basename="service")
router.register("checks", HealthCheckLogViewSet, basename="check")
router.register("stats", DailyHealthStatViewSet, basename="stat")

urlpatterns = [
    path("ping/", ping, name="ping"),
    path("", include(router.urls)),
]
