"""请求耗时统计中间件。

几个刻意的选择：
1. 用 time.perf_counter() 而不是 time.time()：前者是单调时钟，不受 NTP 校时影响，
   不会算出负数耗时。
2. 放在 MIDDLEWARE 第一位：Django 的中间件是「洋葱模型」，排在最外层才能把
   后面所有中间件和视图的耗时都覆盖进来。
3. 计时写在 finally 里：视图抛异常时也要记录，否则最慢的那批请求恰好是看不到的。
4. 只落日志不落库：每个请求写一次 INSERT 会把 QPS 打骨折，
   耗时数据属于「高写入、低价值密度」，走日志 + 采样聚合才是正解。
"""
from __future__ import annotations

import logging
import time

from django.conf import settings

logger = logging.getLogger("core.request")

SKIP_PREFIXES = ("/static/", "/favicon.ico")


class RequestLatencyMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_ms = getattr(settings, "INSPECTION_SLOW_REQUEST_MS", 500)

    # ---------------- 同步链路 ----------------
    def __call__(self, request):
        started = time.perf_counter()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            self._emit(request, response, (time.perf_counter() - started) * 1000)

    # ---------------- 异步链路 ----------------
    async def __acall__(self, request):
        started = time.perf_counter()
        response = None
        try:
            response = await self.get_response(request)
            return response
        finally:
            self._emit(request, response, (time.perf_counter() - started) * 1000)

    # ---------------- 公共 ----------------
    def _emit(self, request, response, duration_ms: float) -> None:
        # 挂到 request 上，方便视图 / 日志里直接取
        request.duration_ms = duration_ms

        if response is not None:
            response["X-Request-Duration-Ms"] = f"{duration_ms:.1f}"

        if request.path.startswith(SKIP_PREFIXES):
            return

        status = getattr(response, "status_code", "-")
        logger.info(
            "%s %s -> %s %.1fms ip=%s",
            request.method,
            request.get_full_path(),
            status,
            duration_ms,
            self._client_ip(request),
        )

        if duration_ms >= self.slow_ms:
            logger.warning(
                "慢请求 %s %s -> %s %.1fms (阈值 %sms)",
                request.method,
                request.get_full_path(),
                status,
                duration_ms,
                self.slow_ms,
            )

    @staticmethod
    def _client_ip(request) -> str:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "-")
