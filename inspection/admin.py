from django.contrib import admin

from .models import DailyHealthStat, Environment, HealthCheckLog, Service, Team


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "created_at")
    search_fields = ("name", "owner")


@admin.register(Environment)
class EnvironmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "sort_order")
    list_editable = ("sort_order",)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "env", "team", "base_url", "token_hint", "token_rotated_at", "enabled")
    list_filter = ("env", "team", "enabled")
    search_fields = ("name", "base_url")
    # 密文字段一律不在后台展示，避免误操作
    exclude = ("token_cipher", "previous_token_cipher")
    readonly_fields = ("token_hint", "token_rotated_at", "previous_token_expires_at")


@admin.register(HealthCheckLog)
class HealthCheckLogAdmin(admin.ModelAdmin):
    list_display = ("service", "ok", "status_code", "latency_ms", "trigger", "created_at")
    list_filter = ("ok", "trigger", "service__env")
    date_hierarchy = "created_at"


@admin.register(DailyHealthStat)
class DailyHealthStatAdmin(admin.ModelAdmin):
    list_display = ("stat_date", "env", "team", "service", "total", "ok_count", "health_rate")
    list_filter = ("stat_date", "env", "team")
