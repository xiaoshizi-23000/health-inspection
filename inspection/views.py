from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from . import probes, services as biz
from .models import DailyHealthStat, Environment, HealthCheckLog, Service, Team
from .serializers import (
    DailyHealthStatSerializer,
    EnvironmentSerializer,
    HealthCheckLogSerializer,
    ServiceSerializer,
    TeamSerializer,
)


@api_view(["GET"])
def ping(request):
    """存活探针。巡检系统自己也需要被巡检，这个接口就是给它用的靶子。"""
    return Response({"status": "ok", "service": "health-inspection", "time": timezone.now()})


def _bool_param(request, key):
    raw = request.query_params.get(key)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class TeamViewSet(viewsets.ModelViewSet):
    serializer_class = TeamSerializer

    def get_queryset(self):
        # annotate 一次算完，避免列表页每个团队都去 count 一遍（N+1）
        qs = Team.objects.annotate(service_count=Count("services"))
        keyword = self.request.query_params.get("q")
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs


class EnvironmentViewSet(viewsets.ModelViewSet):
    serializer_class = EnvironmentSerializer

    def get_queryset(self):
        return Environment.objects.annotate(service_count=Count("services"))


class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer

    def get_queryset(self):
        qs = Service.objects.select_related("env", "team")
        params = self.request.query_params

        if params.get("env"):
            qs = qs.filter(env_id=params["env"])
        if params.get("env_code"):
            qs = qs.filter(env__code=params["env_code"])
        if params.get("team"):
            qs = qs.filter(team_id=params["team"])
        enabled = _bool_param(self.request, "enabled")
        if enabled is not None:
            qs = qs.filter(enabled=enabled)
        if params.get("q"):
            qs = qs.filter(name__icontains=params["q"])
        return qs

    def perform_create(self, serializer):
        # Token 由系统签发，不允许客户端自定义
        service = serializer.save()
        biz.issue_token(service)

    @action(detail=True, methods=["post"], url_path="check")
    def check(self, request, pk=None):
        """手动触发一次健康检查。

        注意：HTTP 200 表示「探测动作执行完成」，服务本身是否健康看响应体里的 ok 字段。
        探测失败是业务结果，不是接口错误，所以不用 4xx/5xx。
        """
        service = self.get_object()
        result, log = probes.probe_and_record(service, HealthCheckLog.Trigger.MANUAL)
        return Response(
            {
                "service_id": service.id,
                "service": str(service),
                "health_url": service.health_url,
                "trigger": log.trigger,
                "checked_at": log.created_at,
                **result.as_dict(),
            }
        )

    @action(detail=True, methods=["post"], url_path="rotate-token")
    def rotate_token(self, request, pk=None):
        """立即轮换 Token。明文只在这里返回一次，之后只能看尾号。"""
        service = self.get_object()
        plain = biz.issue_token(service)
        return Response(
            {
                "service_id": service.id,
                "token": plain,
                "token_hint": service.token_hint,
                "rotated_at": service.token_rotated_at,
                "previous_token_expires_at": service.previous_token_expires_at,
                "notice": "明文仅本次返回，服务端只保存密文，请立即下发给被巡检服务。",
            }
        )


class HealthCheckLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = HealthCheckLogSerializer

    def get_queryset(self):
        qs = HealthCheckLog.objects.select_related("service", "service__env")
        params = self.request.query_params
        if params.get("service"):
            qs = qs.filter(service_id=params["service"])
        ok = _bool_param(self.request, "ok")
        if ok is not None:
            qs = qs.filter(ok=ok)
        if params.get("trigger"):
            qs = qs.filter(trigger=params["trigger"])
        return qs


class DailyHealthStatViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DailyHealthStatSerializer

    def get_queryset(self):
        qs = DailyHealthStat.objects.select_related("service", "env", "team")
        params = self.request.query_params
        if params.get("date"):
            qs = qs.filter(stat_date=params["date"])
        if params.get("service"):
            qs = qs.filter(service_id=params["service"])
        if params.get("env"):
            qs = qs.filter(env_id=params["env"])
        if params.get("team"):
            qs = qs.filter(team_id=params["team"])
        return qs

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """按「环境 × 团队」汇总。

        健康率不能对每行 rate 求平均（小流量的服务会被放大权重），
        必须用 sum(ok)/sum(total) 重新算。
        """
        rows = (
            self.get_queryset()
            .values("stat_date", "env_id", "env__name", "team_id", "team__name")
            .annotate(
                services=Count("id"),
                total=Sum("total"),
                ok_count=Sum("ok_count"),
            )
            .order_by("stat_date", "env_id", "team_id")
        )

        data = []
        for row in rows:
            total = row["total"] or 0
            ok_count = row["ok_count"] or 0
            data.append(
                {
                    "stat_date": str(row["stat_date"]),
                    "env_id": row["env_id"],
                    "env": row["env__name"],
                    "team_id": row["team_id"],
                    "team": row["team__name"],
                    "services": row["services"],
                    "total": total,
                    "ok_count": ok_count,
                    "health_rate": DailyHealthStat.calc_rate(ok_count, total),
                }
            )
        return Response({"count": len(data), "results": data})
