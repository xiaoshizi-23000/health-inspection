from rest_framework import serializers

from .models import DailyHealthStat, Environment, HealthCheckLog, Service, Team


class TeamSerializer(serializers.ModelSerializer):
    service_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Team
        fields = ["id", "name", "owner", "service_count", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class EnvironmentSerializer(serializers.ModelSerializer):
    service_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Environment
        fields = ["id", "code", "name", "sort_order", "service_count", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class ServiceSerializer(serializers.ModelSerializer):
    """注意：token_cipher 永远不出现在序列化结果里，列表页只暴露尾号。"""

    env_name = serializers.CharField(source="env.name", read_only=True)
    env_code = serializers.CharField(source="env.code", read_only=True)
    team_name = serializers.CharField(source="team.name", read_only=True)
    health_url = serializers.CharField(read_only=True)
    previous_token_valid = serializers.BooleanField(read_only=True)

    class Meta:
        model = Service
        fields = [
            "id",
            "name",
            "env",
            "env_name",
            "env_code",
            "team",
            "team_name",
            "base_url",
            "health_path",
            "http_method",
            "timeout_seconds",
            "verify_tls",
            "health_url",
            "token_hint",
            "token_rotated_at",
            "previous_token_valid",
            "enabled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "token_hint",
            "token_rotated_at",
            "previous_token_valid",
            "created_at",
            "updated_at",
        ]

    def validate_base_url(self, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise serializers.ValidationError("base_url 必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    def validate_health_path(self, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    def validate_timeout_seconds(self, value: float) -> float:
        if not 0.1 <= value <= 30:
            raise serializers.ValidationError("超时时间应在 0.1 ~ 30 秒之间")
        return value


class HealthCheckLogSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source="service.name", read_only=True)
    env_code = serializers.CharField(source="service.env.code", read_only=True)
    trigger_display = serializers.CharField(source="get_trigger_display", read_only=True)

    class Meta:
        model = HealthCheckLog
        fields = [
            "id",
            "service",
            "service_name",
            "env_code",
            "ok",
            "status_code",
            "latency_ms",
            "error",
            "trigger",
            "trigger_display",
            "created_at",
        ]
        read_only_fields = fields


class DailyHealthStatSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source="service.name", read_only=True)
    env_code = serializers.CharField(source="env.code", read_only=True)
    env_name = serializers.CharField(source="env.name", read_only=True)
    team_name = serializers.CharField(source="team.name", read_only=True)

    class Meta:
        model = DailyHealthStat
        fields = [
            "id",
            "stat_date",
            "env",
            "env_code",
            "env_name",
            "team",
            "team_name",
            "service",
            "service_name",
            "total",
            "ok_count",
            "health_rate",
        ]
        read_only_fields = fields
