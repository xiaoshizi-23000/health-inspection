"""控制台的 CRUD / 查询端点。

与 DRF 的 ViewSet 并行存在：页面用这套简单的 JSON 端点，
保持"读一次渲染、写走 POST"的模式；DRF 接口仍是主 API。
所有写操作都带参数校验，避免脏数据进库。
"""
from __future__ import annotations

from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from . import services as biz
from .models import DailyHealthStat, Environment, HealthCheckLog, Service, Team


def _body(request) -> dict:
    import json

    if request.content_type != "application/json":
        return {}
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


# --------------------------------------------------------------------------
# 服务 CRUD
# --------------------------------------------------------------------------
def _validate_service(data: dict, partial: bool = False) -> tuple[dict, str | None]:
    fields = {}
    if not partial or "name" in data:
        name = str(data.get("name", "")).strip()
        if not name:
            return {}, "服务名不能为空"
        fields["name"] = name[:64]

    for key, model in (("env", Environment), ("team", Team)):
        if partial and key not in data:
            continue
        try:
            obj = model.objects.get(pk=int(data[key]))
        except (KeyError, TypeError, ValueError):
            return {}, f"请选择有效的{ '环境' if key == 'env' else '团队' }"
        except model.DoesNotExist:
            return {}, f"{ '环境' if key == 'env' else '团队' }不存在"
        fields[key] = obj

    if not partial or "base_url" in data:
        base_url = str(data.get("base_url", "")).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            return {}, "基础地址必须以 http:// 或 https:// 开头"
        fields["base_url"] = base_url[:255]

    if "health_path" in data:
        path = str(data["health_path"] or "").strip() or "/"
        fields["health_path"] = ("/" + path.lstrip("/"))[:128]

    if "http_method" in data:
        method = str(data["http_method"]).upper()
        if method not in {"GET", "HEAD"}:
            return {}, "请求方法只支持 GET / HEAD"
        fields["http_method"] = method

    if "timeout_seconds" in data:
        try:
            timeout = float(data["timeout_seconds"])
        except (TypeError, ValueError):
            return {}, "超时时间必须是数字"
        if not 0.1 <= timeout <= 30:
            return {}, "超时时间应在 0.1 ~ 30 秒之间"
        fields["timeout_seconds"] = timeout

    if "verify_tls" in data:
        fields["verify_tls"] = bool(data["verify_tls"])
    if "enabled" in data:
        fields["enabled"] = bool(data["enabled"])

    return fields, None


@require_GET
def service_list(request):
    qs = Service.objects.select_related("env", "team")
    keyword = request.query_params.get("q") if hasattr(request, "query_params") else request.GET.get("q")
    if keyword:
        qs = qs.filter(Q(name__icontains=keyword) | Q(base_url__icontains=keyword))
    items = [
        {
            "id": s.id,
            "name": s.name,
            "env": s.env_id,
            "env_name": s.env.name,
            "env_code": s.env.code,
            "team": s.team_id,
            "team_name": s.team.name,
            "base_url": s.base_url,
            "health_path": s.health_path,
            "health_url": s.health_url,
            "http_method": s.http_method,
            "timeout_seconds": s.timeout_seconds,
            "verify_tls": s.verify_tls,
            "enabled": s.enabled,
            "token_hint": s.token_hint,
            "token_rotated_at": s.token_rotated_at.isoformat() if s.token_rotated_at else None,
            "previous_token_valid": s.previous_token_valid,
        }
        for s in qs.order_by("env__sort_order", "name")
    ]
    return JsonResponse({"ok": True, "count": len(items), "results": items})


@require_POST
def service_create(request):
    fields, error = _validate_service(_body(request))
    if error:
        return _error(error)
    # 同环境内服务名唯一
    if Service.objects.filter(env=fields["env"], name=fields["name"]).exists():
        return _error("同一环境下已存在同名服务")
    service = Service.objects.create(**fields)
    token = biz.issue_token(service)  # 签发 Token，明文只回这一次
    return JsonResponse({"ok": True, "id": service.id, "token": token})


@require_POST
def service_update(request, pk: int):
    try:
        service = Service.objects.get(pk=pk)
    except Service.DoesNotExist:
        return _error("服务不存在", 404)

    data = _body(request)
    fields, error = _validate_service(data, partial=True)
    if error:
        return _error(error)

    target_env = fields.get("env", service.env)
    target_name = fields.get("name", service.name)
    if Service.objects.filter(env=target_env, name=target_name).exclude(pk=pk).exists():
        return _error("同一环境下已存在同名服务")

    for key, value in fields.items():
        setattr(service, key, value)
    service.save()
    return JsonResponse({"ok": True, "id": service.id})


@require_POST
def service_toggle(request, pk: int):
    try:
        service = Service.objects.get(pk=pk)
    except Service.DoesNotExist:
        return _error("服务不存在", 404)
    service.enabled = not service.enabled
    service.save(update_fields=["enabled", "updated_at"])
    return JsonResponse({"ok": True, "id": service.id, "enabled": service.enabled})


@require_POST
def service_delete(request, pk: int):
    try:
        service = Service.objects.get(pk=pk)
    except Service.DoesNotExist:
        return _error("服务不存在", 404)
    service.delete()  # 明细与统计随外键级联清理
    return JsonResponse({"ok": True, "id": pk})


# --------------------------------------------------------------------------
# 团队 / 环境 CRUD
# --------------------------------------------------------------------------
@require_GET
def team_list(request):
    items = [
        {"id": t.id, "name": t.name, "owner": t.owner, "service_count": t.services.count()}
        for t in Team.objects.order_by("name")
    ]
    return JsonResponse({"ok": True, "results": items})


@require_POST
def team_save(request, pk: int | None = None):
    data = _body(request)
    name = str(data.get("name", "")).strip()
    if not name:
        return _error("团队名称不能为空")
    owner = str(data.get("owner", ""))[:32]
    if pk:
        team = Team.objects.filter(pk=pk).first()
        if not team:
            return _error("团队不存在", 404)
    else:
        team = Team()
    if Team.objects.filter(name=name).exclude(pk=pk).exists():
        return _error("团队名称已存在")
    team.name, team.owner = name, owner
    team.save()
    return JsonResponse({"ok": True, "id": team.id})


@require_POST
def team_delete(request, pk: int):
    if Team.objects.filter(pk=pk, services__isnull=False).exists():
        return _error("该团队下还有服务，请先转移或删除服务")
    deleted, _ = Team.objects.filter(pk=pk).delete()
    return JsonResponse({"ok": True, "deleted": deleted}) if deleted else _error("团队不存在", 404)


@require_GET
def environment_list(request):
    items = [
        {
            "id": e.id,
            "code": e.code,
            "name": e.name,
            "sort_order": e.sort_order,
            "service_count": e.services.count(),
        }
        for e in Environment.objects.order_by("sort_order", "code")
    ]
    return JsonResponse({"ok": True, "results": items})


@require_POST
def environment_save(request, pk: int | None = None):
    data = _body(request)
    code = str(data.get("code", "")).strip().lower()
    name = str(data.get("name", "")).strip()
    if code not in dict(Environment.Code.choices):
        return _error("环境标识必须是 dev / test / prod")
    if not name:
        return _error("环境名称不能为空")
    try:
        sort_order = int(data.get("sort_order", 0))
    except (TypeError, ValueError):
        return _error("排序必须是整数")
    if pk:
        env = Environment.objects.filter(pk=pk).first()
        if not env:
            return _error("环境不存在", 404)
    else:
        env = Environment()
    if Environment.objects.filter(code=code).exclude(pk=pk).exists():
        return _error("环境标识已存在")
    env.code, env.name, env.sort_order = code, name, sort_order
    env.save()
    return JsonResponse({"ok": True, "id": env.id})


@require_POST
def environment_delete(request, pk: int):
    if Environment.objects.filter(pk=pk, services__isnull=False).exists():
        return _error("该环境下还有服务，请先转移或删除服务")
    deleted, _ = Environment.objects.filter(pk=pk).delete()
    return JsonResponse({"ok": True, "deleted": deleted}) if deleted else _error("环境不存在", 404)


# --------------------------------------------------------------------------
# 明细筛选 / 统计日期
# --------------------------------------------------------------------------
@require_GET
def check_list(request):
    qs = HealthCheckLog.objects.select_related("service", "service__env")
    service_id = request.GET.get("service")
    if service_id and service_id.isdigit():
        qs = qs.filter(service_id=int(service_id))
    ok = request.GET.get("ok")
    if ok in {"1", "true"}:
        qs = qs.filter(ok=True)
    elif ok in {"0", "false"}:
        qs = qs.filter(ok=False)
    if request.GET.get("env"):
        qs = qs.filter(service__env__code=request.GET["env"])

    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1
    page_size = 15
    total = qs.count()
    rows = qs.order_by("-created_at")[(page - 1) * page_size : page * page_size]
    return JsonResponse(
        {
            "ok": True,
            "total": total,
            "page": page,
            "pages": (total + page_size - 1) // page_size,
            "results": [
                {
                    "id": log.id,
                    "service_name": log.service.name,
                    "env_code": log.service.env.code,
                    "ok": log.ok,
                    "status_code": log.status_code,
                    "latency_ms": log.latency_ms,
                    "error": log.error,
                    "trigger": log.get_trigger_display(),
                    "created_at": log.created_at.isoformat(),
                }
                for log in rows
            ],
        }
    )


@require_GET
def stats_by_date(request):
    """按日期返回统计 + 环境×团队汇总（供日期切换用）。"""
    raw = request.GET.get("date", "")
    stat_date = parse_date(raw) if raw else None
    if raw and not stat_date:
        return _error("日期格式应为 YYYY-MM-DD")

    if stat_date is None:
        stat_date = (
            DailyHealthStat.objects.order_by("-stat_date")
            .values_list("stat_date", flat=True)
            .first()
        )
    if stat_date is None:
        return JsonResponse(
            {"ok": True, "stat_date": None, "dates": [], "services": [], "summary": []}
        )

    rows = (
        DailyHealthStat.objects.filter(stat_date=stat_date)
        .select_related("service", "env", "team")
        .order_by("-health_rate")
    )
    services = [
        {
            "service_name": r.service.name,
            "env": r.env.name,
            "team": r.team.name,
            "total": r.total,
            "ok_count": r.ok_count,
            "health_rate": float(r.health_rate),
        }
        for r in rows
    ]
    summary_rows = (
        DailyHealthStat.objects.filter(stat_date=stat_date)
        .values("env__name", "team__name")
        .annotate(service_count=Count("id"), checks=Sum("total"), ok=Sum("ok_count"))
        .order_by("env__name", "team__name")
    )
    summary = [
        {
            "env": r["env__name"],
            "team": r["team__name"],
            "services": r["service_count"],
            "checks": r["checks"] or 0,
            "health_rate": DailyHealthStat.calc_rate(r["ok"] or 0, r["checks"] or 0),
        }
        for r in summary_rows
    ]
    dates = list(
        DailyHealthStat.objects.order_by("-stat_date")
        .values_list("stat_date", flat=True)
        .distinct()[:30]
    )
    return JsonResponse(
        {
            "ok": True,
            "stat_date": str(stat_date),
            "dates": [str(d) for d in dates],
            "services": services,
            "summary": summary,
        }
    )
