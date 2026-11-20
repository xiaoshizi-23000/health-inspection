from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """公共时间戳，所有业务表都有。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Team(TimeStampedModel):
    """负责团队。"""

    name = models.CharField("团队名称", max_length=64, unique=True)
    owner = models.CharField("负责人", max_length=32, blank=True, default="")

    class Meta:
        verbose_name = "负责团队"
        verbose_name_plural = "负责团队"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Environment(TimeStampedModel):
    """环境：dev / test / prod。

    用独立表而不是 choices，一是便于挂载环境维度的扩展字段，
    二是后续要接审批流、配额、告警策略时直接加字段即可。
    """

    class Code(models.TextChoices):
        DEV = "dev", "开发"
        TEST = "test", "测试"
        PROD = "prod", "生产"

    code = models.CharField("环境标识", max_length=16, choices=Code.choices, unique=True)
    name = models.CharField("环境名称", max_length=32)
    sort_order = models.PositiveSmallIntegerField("排序", default=0)

    class Meta:
        verbose_name = "环境"
        verbose_name_plural = "环境"
        ordering = ["sort_order", "code"]

    def __str__(self) -> str:
        return f"{self.name}({self.code})"


class Service(TimeStampedModel):
    """被巡检的 HTTP 服务。"""

    name = models.CharField("服务名", max_length=64)
    env = models.ForeignKey(
        Environment, on_delete=models.PROTECT, related_name="services", verbose_name="环境"
    )
    team = models.ForeignKey(
        Team, on_delete=models.PROTECT, related_name="services", verbose_name="负责团队"
    )

    base_url = models.URLField("基础地址", max_length=255, help_text="如 http://10.0.0.8:8080")
    health_path = models.CharField("健康检查路径", max_length=128, default="/health")
    http_method = models.CharField(
        "请求方法", max_length=8, choices=[("GET", "GET"), ("HEAD", "HEAD")], default="GET"
    )
    timeout_seconds = models.FloatField("超时时间(秒)", default=3.0)
    verify_tls = models.BooleanField("校验 TLS 证书", default=True)

    # ---- 巡检调用 Token ----
    # 只存密文，明文仅在生成/轮换的那一刻返回给调用方一次。
    token_cipher = models.BinaryField("Token 密文", null=True, blank=True, editable=False)
    token_hint = models.CharField("Token 尾号", max_length=8, blank=True, default="", editable=False)
    token_rotated_at = models.DateTimeField("轮换时间", null=True, blank=True, editable=False)

    # 轮换宽限期：新 Token 生效后，旧 Token 再存活一段时间，
    # 避免正在并发的巡检请求因为刚好卡在轮换瞬间而全部失败。
    previous_token_cipher = models.BinaryField("上一版 Token 密文", null=True, blank=True, editable=False)
    previous_token_expires_at = models.DateTimeField("旧 Token 失效时间", null=True, blank=True, editable=False)

    enabled = models.BooleanField("是否启用巡检", default=True)

    class Meta:
        verbose_name = "服务"
        verbose_name_plural = "服务"
        ordering = ["env__sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["env", "name"], name="uniq_service_name_per_env"),
        ]
        indexes = [
            models.Index(fields=["env", "team"], name="idx_service_env_team"),
            models.Index(fields=["enabled"], name="idx_service_enabled"),
        ]

    def __str__(self) -> str:
        return f"{self.name}@{self.env.code}"

    @property
    def health_url(self) -> str:
        """拼出完整探测地址，容忍 base_url 结尾带不带 /。"""
        base = self.base_url.rstrip("/")
        path = (self.health_path or "/").lstrip("/")
        return f"{base}/{path}"

    @property
    def previous_token_valid(self) -> bool:
        if not (self.previous_token_cipher and self.previous_token_expires_at):
            return False
        return self.previous_token_expires_at > timezone.now()


class HealthCheckLog(models.Model):
    """每一次巡检的明细。手动触发和定时巡检共用这张表，保证统计口径唯一。"""

    class Trigger(models.TextChoices):
        MANUAL = "manual", "手动触发"
        BEAT = "beat", "定时巡检"

    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="checks", verbose_name="服务"
    )
    ok = models.BooleanField("是否健康", db_index=True)
    status_code = models.PositiveSmallIntegerField("HTTP 状态码", null=True, blank=True)
    latency_ms = models.PositiveIntegerField("耗时(毫秒)", null=True, blank=True)
    error = models.CharField("错误摘要", max_length=200, blank=True, default="")
    trigger = models.CharField(
        "触发方式", max_length=8, choices=Trigger.choices, default=Trigger.MANUAL
    )
    created_at = models.DateTimeField("巡检时间", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "巡检明细"
        verbose_name_plural = "巡检明细"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["service", "-created_at"], name="idx_check_service_time"),
            models.Index(fields=["created_at", "ok"], name="idx_check_time_ok"),
        ]

    def __str__(self) -> str:
        return f"{self.service_id} {'OK' if self.ok else 'FAIL'} @{self.created_at:%m-%d %H:%M}"


class DailyHealthStat(models.Model):
    """每日健康率快照。

    粒度是「天 × 服务」，env/team 在写入时一并落库，
    相当于对「归属关系」做了快照：以后服务换团队，历史统计不会被改写。
    """

    stat_date = models.DateField("统计日期", db_index=True)
    env = models.ForeignKey(
        Environment, on_delete=models.PROTECT, related_name="daily_stats", verbose_name="环境"
    )
    team = models.ForeignKey(
        Team, on_delete=models.PROTECT, related_name="daily_stats", verbose_name="负责团队"
    )
    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="daily_stats", verbose_name="服务"
    )

    total = models.PositiveIntegerField("巡检总次数", default=0)
    ok_count = models.PositiveIntegerField("健康次数", default=0)
    health_rate = models.DecimalField("健康率(%)", max_digits=5, decimal_places=2, default=0)

    created_at = models.DateTimeField("生成时间", auto_now_add=True)

    class Meta:
        verbose_name = "每日健康率"
        verbose_name_plural = "每日健康率"
        ordering = ["-stat_date", "env__sort_order", "service__name"]
        constraints = [
            # 保证任务可重跑：同一天同一服务只会有一行
            models.UniqueConstraint(fields=["stat_date", "service"], name="uniq_stat_date_service"),
        ]
        indexes = [
            models.Index(fields=["stat_date", "env", "team"], name="idx_stat_date_env_team"),
        ]

    def __str__(self) -> str:
        return f"{self.stat_date} {self.service_id} {self.health_rate}%"

    @staticmethod
    def calc_rate(ok_count: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round(ok_count * 100.0 / total, 2)
