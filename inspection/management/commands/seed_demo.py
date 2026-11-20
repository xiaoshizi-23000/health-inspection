"""生成演示数据。

默认所有服务的 base_url 都指向本项目自己的 /api/ping/，
这样不依赖任何外网，起个 runserver 就能演示「探测成功」；
另外放两个故意打不通的地址，用来演示健康率掉下来和统计口径。
"""
from __future__ import annotations

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from inspection import services as biz
from inspection.models import DailyHealthStat, Environment, HealthCheckLog, Service, Team

LOCAL_BASE_URL = "http://127.0.0.1:8000"

TEAMS = [
    ("基础架构", "张伟"),
    ("支付中台", "李娜"),
    ("数据平台", "王强"),
]

ENVS = [
    ("dev", "开发", 1),
    ("test", "测试", 2),
    ("prod", "生产", 3),
]

# (服务名, 环境, 团队, base_url, 期望健康率, 说明)
SERVICES = [
    ("user-api", "prod", "基础架构", LOCAL_BASE_URL, 0.99, ""),
    ("order-api", "prod", "支付中台", LOCAL_BASE_URL, 0.96, ""),
    ("payment-gateway", "prod", "支付中台", LOCAL_BASE_URL, 0.88, ""),
    ("report-worker", "test", "数据平台", LOCAL_BASE_URL, 0.93, ""),
    ("etl-scheduler", "test", "数据平台", LOCAL_BASE_URL, 0.75, ""),
    ("user-api", "dev", "基础架构", LOCAL_BASE_URL, 0.90, ""),
    ("legacy-sso", "prod", "基础架构", "http://127.0.0.1:9", 0.20, "故意指向关闭端口，演示不健康"),
    ("notify-svc", "dev", "支付中台", "http://127.0.0.1:9", 0.35, "故意指向关闭端口"),
]


class Command(BaseCommand):
    help = "创建演示用的团队 / 环境 / 服务，可选生成近 24 小时巡检历史"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="先清空巡检明细、统计与业务数据")
        parser.add_argument("--history", action="store_true", help="生成近 24 小时巡检历史，便于立刻演示统计")
        parser.add_argument("--per-service", type=int, default=120, help="每个服务生成的历史条数")

    def handle(self, *args, **options):
        if options["reset"]:
            HealthCheckLog.objects.all().delete()
            DailyHealthStat.objects.all().delete()
            Service.objects.all().delete()
            self.stdout.write("已清空巡检明细 / 统计 / 服务")

        teams = {}
        for name, owner in TEAMS:
            team, _ = Team.objects.get_or_create(name=name, defaults={"owner": owner})
            teams[name] = team

        envs = {}
        for code, name, order in ENVS:
            env, _ = Environment.objects.get_or_create(
                code=code, defaults={"name": name, "sort_order": order}
            )
            envs[code] = env

        created, existing = 0, 0
        service_rows = []
        for name, env_code, team_name, base_url, rate, note in SERVICES:
            service, is_new = Service.objects.get_or_create(
                env=envs[env_code],
                name=name,
                defaults={
                    "team": teams[team_name],
                    "base_url": base_url,
                    "health_path": "/api/ping/" if base_url == LOCAL_BASE_URL else "/",
                    "timeout_seconds": 3.0,
                },
            )
            if is_new:
                biz.issue_token(service)  # 新建服务自动签发 Token
                created += 1
            else:
                existing += 1
            service_rows.append((service, rate, note))

        self.stdout.write(self.style.SUCCESS(f"服务创建 {created} 个，已存在 {existing} 个"))

        if options["history"]:
            count = self._gen_history(service_rows, options["per_service"])
            self.stdout.write(self.style.SUCCESS(f"已生成巡检历史 {count} 条"))

        self.stdout.write("")
        self.stdout.write("可演示的服务 ID：")
        for service, _rate, note in service_rows:
            suffix = f"  # {note}" if note else ""
            self.stdout.write(f"  {service.pk:>3}  {service.name}@{service.env.code}{suffix}")

    def _gen_history(self, service_rows, per_service: int) -> int:
        rng = random.Random(42)  # 固定种子，保证演示结果可复现
        # 对齐到「昨天 00:00 ~ 24:00」，正好是每天 00:00 那个统计任务的窗口，
        # 这样生成完历史直接跑一次统计就能看到结果，不用等到明天。
        start, end = biz.day_bounds(timezone.localdate() - timedelta(days=1))
        step = (end - start) / max(per_service, 1)

        batch = []
        for service, rate, _note in service_rows:
            for i in range(per_service):
                ok = rng.random() < rate
                batch.append(
                    HealthCheckLog(
                        service=service,
                        ok=ok,
                        status_code=200 if ok else rng.choice([500, 502, 503]),
                        latency_ms=rng.randint(18, 260) if ok else rng.randint(3000, 8000),
                        error="" if ok else "unexpected status",
                        trigger=HealthCheckLog.Trigger.BEAT,
                        created_at=start + step * i,
                    )
                )

        HealthCheckLog.objects.bulk_create(batch, batch_size=500)
        return len(batch)
