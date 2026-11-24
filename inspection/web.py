"""运维控制台首页。

Dashboard 是给运维/开发看的"系统门面"：一屏之内能看到当前有多少服务纳管、
昨天健康率如何、最近发生了什么，并直接执行最常用的三个动作。
所有写操作都走 POST + CSRF，读操作在服务端一次查完，不在页面里发一堆请求。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from celery import current_app
from django.conf import settings
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import tasks
from .models import DailyHealthStat, HealthCheckLog, Service

TASK_ACTIONS = {
    "inspect": (
        tasks.inspect_all_services,
        "全量巡检完成，可在「最近巡检」看到新记录。",
    ),
    "rotate": (
        tasks.rotate_service_tokens,
        "Token 轮换完成，旧 Token 仍有 10 分钟宽限期。",
    ),
    "stat": (
        tasks.daily_health_stat,
        "昨日统计重算完成，结果以最新一次计算为准（幂等）。",
    ),
}


def online_workers() -> dict:
    """通过 Celery inspect 问一圈：现在有哪些 worker 在线。"""
    try:
        return current_app.control.inspect(timeout=1).ping() or {}
    except Exception:  # broker 连不上 / 没有 worker
        return {}


@require_GET
def dashboard(request):
    today = timezone.localdate()

    service_total = Service.objects.count()
    service_enabled = Service.objects.filter(enabled=True).count()

    checks_today = HealthCheckLog.objects.filter(created_at__date=today).count()
    checks_failed_today = HealthCheckLog.objects.filter(
        created_at__date=today, ok=False
    ).count()

    stat_date = DailyHealthStat.objects.order_by("-stat_date").values_list(
        "stat_date", flat=True
    ).first()
    stat_totals = DailyHealthStat.objects.filter(stat_date=stat_date).aggregate(
        total=Sum("total"), ok=Sum("ok_count")
    )
    overall_rate = DailyHealthStat.calc_rate(
        stat_totals["ok"] or 0, stat_totals["total"] or 0
    )

    # 按环境 × 团队汇总（加权健康率 = sum(ok)/sum(total)，不是对各行 rate 求平均）
    summary_rows = (
        DailyHealthStat.objects.filter(stat_date=stat_date)
        .values("env__code", "env__name", "team__name")
        .annotate(
            services=Count("id"),
            total=Sum("total"),
            ok_count=Sum("ok_count"),
        )
        .order_by("env__sort_order", "team__name")
    )
    summary = []
    for row in summary_rows:
        total = row["total"] or 0
        ok_count = row["ok_count"] or 0
        summary.append(
            {
                "env": f"{row['env__name']} ({row['env__code']})",
                "team": row["team__name"],
                "services": row["services"],
                "total": total,
                "health_rate": DailyHealthStat.calc_rate(ok_count, total),
            }
        )

    services = (
        Service.objects.select_related("env", "team")
        .order_by("env__sort_order", "name")
        .values("id", "name", "env__code", "team__name", "token_hint")
    )

    recent_logs = (
        HealthCheckLog.objects.select_related("service", "service__env")
        .order_by("-created_at")[:10]
    )

    context = {
        "stat_date": stat_date,
        "service_total": service_total,
        "service_enabled": service_enabled,
        "checks_today": checks_today,
        "checks_failed_today": checks_failed_today,
        "overall_rate": overall_rate,
        "summary": summary,
        "services": services,
        "recent_logs": recent_logs,
    }
    return render(request, "inspection/dashboard.html", context)


@require_GET
def worker_status(request):
    """首页显示 worker 是否在线，避免用户对着按钮干等。"""
    workers = online_workers()
    return JsonResponse({"online": bool(workers), "workers": sorted(workers)})


@require_POST
def start_worker(request):
    """一键拉起 worker + beat（仅 DEBUG 下可用）。

    生产环境进程由 systemd / supervisord / 容器编排管理，
    绝不能让 Web 请求去 fork 后台进程——这里只是本地演示的便利入口。
    """
    if not settings.DEBUG:
        return JsonResponse({"ok": False, "error": "仅开发环境允许启动后台服务"}, status=403)

    if online_workers():
        return JsonResponse({"ok": True, "already": True, "message": "后台服务已在线，无需重复启动"})

    kwargs = {"cwd": Path(settings.BASE_DIR)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # Windows 上弹新窗口，能看到日志

    commands = [
        [sys.executable, "-m", "celery", "-A", "config", "worker", "-l", "info", "--pool=solo"],
        [sys.executable, "-m", "celery", "-A", "config", "beat", "-l", "info"],
    ]
    pids = []
    for command in commands:
        try:
            process = subprocess.Popen(command, **kwargs)
            pids.append(process.pid)
        except OSError as exc:
            return JsonResponse({"ok": False, "error": f"启动失败：{exc}"}, status=500)

    return JsonResponse(
        {
            "ok": True,
            "pids": pids,
            "message": "已拉起 worker 和 beat（见新窗口）。稍等几秒后点「检查状态」确认。",
        }
    )


@require_POST
def trigger_task(request, action: str):
    """执行任务：worker 在线则异步投递，离线则在当前进程同步执行。

    演示场景下按钮必须有反馈——不能因为忘了开 worker 就卡住或报错。
    """
    config = TASK_ACTIONS.get(action)
    if config is None:
        return JsonResponse({"ok": False, "error": "未知操作"}, status=400)

    task, message = config

    if online_workers():
        result = task.delay()
        return JsonResponse(
            {"ok": True, "mode": "async", "task_id": result.id, "message": message + "（异步）"}
        )

    # worker 不在：同步跑一遍，结果直接回显
    outcome = task.apply()
    return JsonResponse(
        {
            "ok": True,
            "mode": "sync",
            "result": outcome.result,
            "message": message + "（同步执行：后台 worker 未启动，本次直接在 Web 进程完成）",
        }
    )
