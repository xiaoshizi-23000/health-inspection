"""运维控制台首页。

Dashboard 是给运维/开发看的"系统门面"：一屏之内能看到当前有多少服务纳管、
昨天健康率如何、最近发生了什么，并直接执行最常用的三个动作。
所有写操作都走 POST + CSRF，读操作在服务端一次查完，不在页面里发一堆请求。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from celery import current_app
from django.conf import settings
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import tasks
from .models import DailyHealthStat, Environment, HealthCheckLog, Service, Team

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


def worker_ready_in_log() -> bool:
    """看 worker.log 里有没有 ready 标记——比 inspect 广播可靠。

    inspect().ping() 在机器上有多个同名 worker（DuplicateNodenameWarning）时
    会行为异常，明明 worker 活着却返回空，导致误判"启动失败"。
    """
    content = log_tail("worker.log", limit=200)
    return " ready." in content or " ready " in content


def process_alive(pid: int) -> bool:
    """检查进程是否还活着。

    Windows 上不用 tasklist：Git Bash 会把 /FI 当路径转换，且输出是 GBK，
    用 text=True 解码会直接抛 UnicodeDecodeError。改用 Win32 API（纯 ctypes，无依赖）。
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            # STILL_ACTIVE = 259
            return bool(ok) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0 只探测不杀进程
        return True
    except OSError:
        return False


def online_workers() -> dict:
    """判断 worker 是否在线。

    优先用 inspect（能拿到节点名），但它不可靠时降级到"日志 + 进程"判断：
    worker.log 里有 ready 且记录的 PID 还活着 → 视为在线。
    """
    try:
        result = current_app.control.inspect(timeout=2).ping()
        if result:
            return result
    except Exception:
        pass

    state = read_worker_state()
    if state.get("pid") and state.get("log_ready") and process_alive(state["pid"]):
        hostname = state.get("nodename", "worker")
        return {hostname: {}}
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
        "team_count": Team.objects.count(),
        "env_count": Environment.objects.count(),
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


def find_python() -> str:
    """挑一个真正能跑起 Celery 的解释器——不是"import 成功"就算数。

    踩过的坑：3.13 里 django/celery 都能 import，但 redis-py 是 6.0.0，
    kombu 要求 >= 6.1.0，worker 启动即崩。所以必须实际连一次 broker 才算可用。

    候选顺序：显式配置 → 已知的 3.12 → 当前解释器（最后兜底）。
    """
    candidates = [
        getattr(settings, "PYTHON_EXECUTABLE", ""),
        r"C:\Users\23000\AppData\Local\Programs\Python\Python312\python.exe",
        sys.executable,
    ]
    broker = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    # 让候选解释器自己验证：依赖齐全 + kombu 能连上 broker（版本不匹配会在这里炸）
    probe = (
        "import sys, django, celery, rest_framework, kombu; "
        f"from kombu import Connection; Connection({broker!r}).ensure_connection(max_retries=0); "
        "sys.exit(0)"
    )
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            check = subprocess.run(
                [candidate, "-c", probe], capture_output=True, timeout=10
            )
            if check.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return sys.executable  # 都不行就交给当前解释器，日志会说明真实原因


def log_tail(name: str, limit: int = 30) -> str:
    path = Path(settings.BASE_DIR) / "logs" / name
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def state_path() -> Path:
    return Path(settings.BASE_DIR) / "logs" / "worker-state.json"


def read_worker_state() -> dict:
    import json

    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def write_worker_state(pid: int, nodename: str) -> None:
    import json

    state_path().write_text(
        json.dumps({"pid": pid, "nodename": nodename}), encoding="utf-8"
    )


@require_POST
def start_worker(request):
    """一键拉起 worker + beat（仅 DEBUG 下可用）。

    生产环境进程由 systemd / supervisord / 容器编排管理，
    绝不能让 Web 请求去 fork 后台进程——这里只是本地演示的便利入口。
    """
    if not settings.DEBUG:
        return JsonResponse({"ok": False, "error": "仅开发环境允许启动后台服务"}, status=403)

    # 双重检查：inspect + 状态文件里的 PID 存活，防止重复启动堆积多个 worker
    state = read_worker_state()
    if online_workers() or (
        state.get("pid") and process_alive(state["pid"]) and worker_ready_in_log()
    ):
        return JsonResponse(
            {"ok": True, "already": True, "message": "后台服务已在线，无需重复启动"}
        )

    python = find_python()
    log_dir = Path(settings.BASE_DIR) / "logs"
    log_dir.mkdir(exist_ok=True)

    kwargs = {"cwd": Path(settings.BASE_DIR)}
    if os.name == "nt":
        # 脱离父进程 + 不弹窗：即使启动它的终端/PyCharm 会话结束，worker 也不被回收。
        # 输出重定向到日志文件，"闪退"也能看到原因。
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    for name in ("worker.log", "beat.log"):
        (log_dir / name).write_text("", encoding="utf-8")

    # 唯一节点名：避免机器上残留多个 celery@HUAWEI-PC 导致 inspect 广播混乱
    import uuid

    nodename = f"inspect@{uuid.uuid4().hex[:8]}"
    commands = [
        (
            "worker.log",
            [
                python, "-m", "celery", "-A", "config", "worker", "-l", "info",
                "--pool=solo", "-n", nodename,
            ],
        ),
        ("beat.log", [python, "-m", "celery", "-A", "config", "beat", "-l", "info"]),
    ]
    pids = []
    for log_name, command in commands:
        try:
            with (log_dir / log_name).open("ab") as handle:
                process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, **kwargs)
            pids.append(process.pid)
            if log_name == "worker.log":
                write_worker_state(process.pid, nodename)
        except OSError as exc:
            return JsonResponse({"ok": False, "error": f"启动失败：{exc}"}, status=500)

    # 最多等 12 秒：以「日志出现 ready 且进程还活着」为准，不依赖 inspect 广播
    for _ in range(12):
        state = read_worker_state()
        if (
            worker_ready_in_log()
            and state.get("pid")
            and process_alive(state["pid"])
        ):
            return JsonResponse(
                {
                    "ok": True,
                    "pids": pids,
                    "python": python,
                    "nodename": nodename,
                    "message": "后台服务已启动并就绪。",
                }
            )
        time.sleep(1)

    return JsonResponse(
        {
            "ok": False,
            "error": "进程已拉起但 12 秒内未就绪，通常是解释器缺依赖。请把下面的日志发给排查：",
            "python": python,
            "log": log_tail("worker.log") + "\n---- beat ----\n" + log_tail("beat.log"),
        },
        status=500,
    )


@require_GET
def worker_logs(request):
    """首页「查看日志」按钮：直接读两个日志文件，排障不用开黑窗口。"""
    return JsonResponse(
        {
            "worker": log_tail("worker.log"),
            "beat": log_tail("beat.log"),
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
