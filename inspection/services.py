"""业务逻辑层。

这一层不依赖 Celery，也不依赖 HTTP，纯靠 ORM 就能跑，
所以单测和 shell 里都能直接调用；tasks.py 只是它的薄壳。
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from core import crypto

from .models import DailyHealthStat, HealthCheckLog, Service

logger = logging.getLogger("inspection")


# --------------------------------------------------------------------------
# Token 轮换
# --------------------------------------------------------------------------
def issue_token(service: Service, grace_seconds: int | None = None, save: bool = True) -> str:
    """给服务下发一枚新 Token，旧 Token 进入宽限期。

    返回明文——调用方（视图/任务）负责把它送到位，之后数据库里只剩密文。
    """
    plain = crypto.generate_token()
    now = timezone.now()
    grace = (
        settings.INSPECTION_TOKEN_GRACE_SECONDS if grace_seconds is None else grace_seconds
    )

    if service.token_cipher:
        service.previous_token_cipher = service.token_cipher
        service.previous_token_expires_at = now + timedelta(seconds=grace)
    else:
        service.previous_token_cipher = None
        service.previous_token_expires_at = None

    service.token_cipher = crypto.encrypt(plain)
    service.token_hint = crypto.token_hint(plain)
    service.token_rotated_at = now

    if save:
        service.save(
            update_fields=[
                "token_cipher",
                "token_hint",
                "token_rotated_at",
                "previous_token_cipher",
                "previous_token_expires_at",
                "updated_at",
            ]
        )
    return plain


def rotate_all_tokens() -> dict:
    """逐个轮换，单个失败不影响其他服务（fail-fast 在这里是反模式）。"""
    rotated, failed = 0, []
    for service in Service.objects.filter(enabled=True).iterator(chunk_size=200):
        try:
            issue_token(service)
            rotated += 1
        except Exception as exc:  # noqa: BLE001 - 任务需要尽量跑完
            logger.exception("轮换 Token 失败 service_id=%s", service.pk)
            failed.append({"service_id": service.pk, "error": str(exc)})

    logger.info("Token 轮换完成 rotated=%s failed=%s", rotated, len(failed))
    return {"rotated": rotated, "failed": failed}


def purge_expired_previous_tokens() -> int:
    """把过了宽限期的旧 Token 密文清掉，减少无谓的敏感数据驻留。"""
    now = timezone.now()
    count = (
        Service.objects.filter(
            previous_token_cipher__isnull=False,
            previous_token_expires_at__lt=now,
        )
        .update(previous_token_cipher=None, previous_token_expires_at=None)
    )
    return count


# --------------------------------------------------------------------------
# 每日健康率统计
# --------------------------------------------------------------------------
def day_bounds(stat_date) -> tuple[datetime, datetime]:
    """把「某一天」翻译成带时区的 [start, end) 区间。

    USE_TZ=True 时数据库里存的是 UTC，这里必须先 make_aware，
    否则会把「北京时间 00:00」当成「UTC 00:00」，统计窗口整体偏 8 小时。
    """
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(stat_date, time.min), tz)
    return start, start + timedelta(days=1)


def build_daily_stats(stat_date=None, overwrite: bool = True) -> dict:
    """统计 stat_date 当天的健康率并写入 DailyHealthStat。

    - 一次条件聚合拿到「总次数 + 成功次数」，不做 N 次查询；
    - 没有任何巡检记录的服务补 0 行，避免报表漏服务；
    - 先删后插 + 唯一约束，保证任务重跑幂等。
    """
    stat_date = stat_date or (timezone.localdate() - timedelta(days=1))
    start, end = day_bounds(stat_date)

    # values(...).annotate(...) 就是 GROUP BY service_id，
    # Count(filter=Q(ok=True)) 生成的是 COUNT(...) FILTER (WHERE ok) / SUM(ok)，
    # 一条 SQL 同时得到分子和分母。
    rows = (
        HealthCheckLog.objects.filter(created_at__gte=start, created_at__lt=end)
        .values("service_id")
        .annotate(total=Count("id"), ok_count=Count("id", filter=Q(ok=True)))
    )
    agg = {row["service_id"]: (row["total"], row["ok_count"]) for row in rows}

    services = Service.objects.values("id", "env_id", "team_id")
    objects, missing = [], 0
    for item in services:
        total, ok_count = agg.get(item["id"], (0, 0))
        if total == 0:
            missing += 1
        objects.append(
            DailyHealthStat(
                stat_date=stat_date,
                env_id=item["env_id"],
                team_id=item["team_id"],
                service_id=item["id"],
                total=total,
                ok_count=ok_count,
                health_rate=DailyHealthStat.calc_rate(ok_count, total),
            )
        )

    with transaction.atomic():
        if overwrite:
            DailyHealthStat.objects.filter(stat_date=stat_date).delete()
        DailyHealthStat.objects.bulk_create(objects, batch_size=500)

    summary = {
        "stat_date": str(stat_date),
        "services": len(objects),
        "no_traffic": missing,
        "window": [start.isoformat(), end.isoformat()],
    }
    logger.info("每日统计完成 %s", summary)
    return summary
