"""探测执行器。

把「怎么探测」和「探测结果怎么落库」拆开：
- probe_service() 是纯函数式的，不碰数据库，方便单测和并发调用；
- build_log() 只是把结果翻译成一条 ORM 对象，批量巡检时统一 bulk_create。

并发细节：requests.Session 不是线程安全的，所以用 threading.local 每个线程一个，
既复用了连接池，又不会串数据。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests
from django.utils import timezone

from core import crypto

from .models import HealthCheckLog, Service

logger = logging.getLogger("inspection")

# 2xx/3xx 视为健康；4xx 说明服务活着但路径/鉴权有问题，按不健康处理。
HEALTHY_STATUS_MAX = 400

_local = threading.local()

try:  # 关闭自签证书场景下的 InsecureRequestWarning 噪音
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass


@dataclass
class ProbeResult:
    ok: bool
    status_code: int | None = None
    latency_ms: int | None = None
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def get_session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "health-inspection/1.0"})
        _local.session = session
    return session


def probe_service(service: Service, token: str | None = None) -> ProbeResult:
    """对单个服务发起一次健康检查，永远不抛异常。"""
    url = service.health_url
    headers = {"Accept": "*/*"}
    token = token or crypto.try_decrypt(service.token_cipher)
    if token:
        headers["X-Inspection-Token"] = token

    started = time.perf_counter()
    try:
        response = get_session().request(
            service.http_method,
            url,
            timeout=service.timeout_seconds,
            headers=headers,
            verify=service.verify_tls,
            allow_redirects=False,
        )
    except requests.Timeout:
        latency = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, None, latency, f"timeout after {service.timeout_seconds}s")
    except requests.ConnectionError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, None, latency, _describe(exc))
    except requests.RequestException as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return ProbeResult(False, None, latency, _short(f"{type(exc).__name__}: {exc}"))

    latency = int((time.perf_counter() - started) * 1000)
    ok = response.status_code < HEALTHY_STATUS_MAX
    error = "" if ok else f"unexpected status {response.status_code}"
    return ProbeResult(ok, response.status_code, latency, error)


def build_log(service: Service, result: ProbeResult, trigger: str) -> HealthCheckLog:
    return HealthCheckLog(
        service=service,
        ok=result.ok,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        error=_short(result.error, 200),
        trigger=trigger,
        created_at=timezone.now(),
    )


def probe_and_record(service: Service, trigger: str) -> tuple[ProbeResult, HealthCheckLog]:
    result = probe_service(service)
    log = build_log(service, result, trigger)
    log.save(force_insert=True)
    return result, log


def _short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(exc: requests.ConnectionError) -> str:
    """把 requests 那串又臭又长的异常压成人能看懂的一句话。

    原始信息形如：
    HTTPConnectionPool(host='127.0.0.1', port=9): Max retries exceeded with url: /
    (Caused by NewConnectionError('<urllib3.connection.HTTPConnection ...>: Failed to establish
    a new connection: [WinError 10061] 由于目标计算机积极拒绝，无法连接。'))
    直接入库既难读又占空间，这里归类成固定几档，方便后续按错误类型聚合。
    """
    text = str(exc)
    if "10061" in text or "积极拒绝" in text or "Connection refused" in text:
        return "connection refused"
    if "getaddrinfo" in text or "Name or service not known" in text or "11001" in text:
        return "dns resolve failed"
    if "timed out" in text:
        return "connect timeout"
    if "Caused by" in text:
        return _short(text.split("Caused by")[-1].strip(" :()'"), 120)
    return _short(text, 120)
