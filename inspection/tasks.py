"""Celery 任务。

这一层刻意做得很薄：只负责「取数据、并发、批量落库」，
真正的业务规则在 services.py 里，脱离了 Celery 也能跑、也能单测。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from . import probes, services as biz
from .models import HealthCheckLog, Service

logger = logging.getLogger("inspection")


@shared_task(name="inspection.tasks.rotate_service_tokens")
def rotate_service_tokens() -> dict:
    """每 8 小时轮换全部服务 Token。"""
    return biz.rotate_all_tokens()


@shared_task(name="inspection.tasks.inspect_all_services")
def inspect_all_services(max_workers: int | None = None) -> dict:
    """全量巡检。

    IO 密集，用线程池而不是开一堆 celery 子任务：
    子任务会让 broker 里的消息数随服务数量线性膨胀，而且汇总结果还得再同步一次。
    """
    targets = list(Service.objects.filter(enabled=True).select_related("env", "team"))
    if not targets:
        return {"checked": 0, "ok": 0, "failed": 0}

    workers = max_workers or settings.INSPECTION_MAX_WORKERS
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probes.probe_service, targets))

    logs = [
        probes.build_log(service, result, HealthCheckLog.Trigger.BEAT)
        for service, result in zip(targets, results)
    ]
    # 只留一次 INSERT：几百个服务也不会把数据库打出一堆小事务
    HealthCheckLog.objects.bulk_create(logs, batch_size=500)

    ok = sum(1 for result in results if result.ok)
    summary = {"checked": len(logs), "ok": ok, "failed": len(logs) - ok}
    logger.info("全量巡检完成 %s", summary)
    return summary


@shared_task(name="inspection.tasks.daily_health_stat")
def daily_health_stat(stat_date: str | None = None) -> dict:
    """每天 00:00 统计前一天的健康率。

    stat_date 允许传入，是为了补跑历史数据和本地演示。
    """
    target = date.fromisoformat(stat_date) if stat_date else None
    return biz.build_daily_stats(target)


@shared_task(name="inspection.tasks.cleanup_old_logs")
def cleanup_old_logs(days: int | None = None) -> dict:
    """清理过期明细 + 清理已过宽限期的旧 Token 密文。"""
    days = days or settings.INSPECTION_LOG_RETENTION_DAYS
    cutoff = timezone.now() - timedelta(days=days)
    # 生产环境建议按主键分片删除，避免一次大事务长时间锁表
    deleted, _ = HealthCheckLog.objects.filter(created_at__lt=cutoff).delete()
    cleared = biz.purge_expired_previous_tokens()
    summary = {"cutoff": cutoff.isoformat(), "deleted_logs": deleted, "expired_tokens_cleared": cleared}
    logger.info("清理完成 %s", summary)
    return summary
