"""关键链路的回归测试。

重点覆盖三类「最容易被改动破坏」的行为：
1. 加密链路：密文可逆、篡改不可解、明文不留痕；
2. 统计链路：条件聚合的正确性、零流量服务的补行、幂等重跑；
3. 接口链路：手动探测落库、新建服务自动签发 Token、中间件响应头。
"""
from datetime import datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import crypto
from inspection import probes
from inspection.models import DailyHealthStat, Environment, HealthCheckLog, Service, Team
from inspection.services import build_daily_stats, issue_token


def make_aware(dt: datetime) -> datetime:
    return timezone.make_aware(dt, timezone.get_current_timezone())


class CryptoTests(TestCase):
    def test_roundtrip(self):
        plain = crypto.generate_token()
        cipher = crypto.encrypt(plain)
        self.assertNotEqual(cipher, plain.encode())
        self.assertEqual(crypto.decrypt(cipher), plain)

    def test_tampered_cipher_rejected(self):
        cipher = bytearray(crypto.encrypt(crypto.generate_token()))
        cipher[-1] ^= 0xFF
        self.assertIsNone(crypto.try_decrypt(bytes(cipher)))

    def test_token_hint_only_tail(self):
        token = crypto.generate_token()
        self.assertEqual(crypto.token_hint(token), token[-4:])
        self.assertEqual(len(crypto.generate_token()), 43)

    def test_tokens_are_random(self):
        self.assertNotEqual(crypto.generate_token(), crypto.generate_token())


class HealthRateTests(TestCase):
    def test_calc_rate(self):
        self.assertEqual(DailyHealthStat.calc_rate(0, 0), 0.0)
        self.assertEqual(DailyHealthStat.calc_rate(1, 0), 0.0)
        self.assertEqual(DailyHealthStat.calc_rate(1, 3), 33.33)
        self.assertEqual(DailyHealthStat.calc_rate(3, 3), 100.0)


class BuildDailyStatsTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="基础架构")
        self.env = Environment.objects.create(code="dev", name="开发")
        self.hot = Service.objects.create(
            name="hot-api", env=self.env, team=self.team, base_url="http://x"
        )
        self.cold = Service.objects.create(
            name="cold-api", env=self.env, team=self.team, base_url="http://x"
        )

    def _seed(self):
        # 「昨天」窗口内：hot-api 3 条（2 成功），cold-api 没有任何记录
        stat_date = timezone.localdate() - timedelta(days=1)
        base = make_aware(datetime.combine(stat_date, datetime.min.time()).replace(hour=12))
        HealthCheckLog.objects.bulk_create(
            [
                HealthCheckLog(service=self.hot, ok=True, status_code=200, latency_ms=10, created_at=base),
                HealthCheckLog(service=self.hot, ok=True, status_code=200, latency_ms=10, created_at=base),
                HealthCheckLog(
                    service=self.hot, ok=False, status_code=502, error="unexpected status 502",
                    created_at=base + timedelta(minutes=1),
                ),
            ]
        )
        return stat_date

    def test_aggregate_and_zero_fill(self):
        stat_date = self._seed()
        summary = build_daily_stats(stat_date)

        self.assertEqual(summary["services"], 2)
        self.assertEqual(summary["no_traffic"], 1)

        hot = DailyHealthStat.objects.get(stat_date=stat_date, service=self.hot)
        self.assertEqual((hot.total, hot.ok_count, float(hot.health_rate)), (3, 2, 66.67))

        # 零流量服务也要有一行，不能从报表里消失
        cold = DailyHealthStat.objects.get(stat_date=stat_date, service=self.cold)
        self.assertEqual((cold.total, cold.ok_count), (0, 0))

    def test_out_of_window_not_counted(self):
        stat_date = self._seed()
        # 窗口外的记录不该被算进去
        HealthCheckLog.objects.create(service=self.hot, ok=True, status_code=200)
        build_daily_stats(stat_date)
        hot = DailyHealthStat.objects.get(stat_date=stat_date, service=self.hot)
        self.assertEqual(hot.total, 3)

    def test_rerun_is_idempotent(self):
        stat_date = self._seed()
        build_daily_stats(stat_date)
        build_daily_stats(stat_date)
        self.assertEqual(DailyHealthStat.objects.count(), 2)
        hot = DailyHealthStat.objects.get(stat_date=stat_date, service=self.hot)
        self.assertEqual((hot.total, hot.ok_count), (3, 2))


class TokenIssuanceTests(TestCase):
    def setUp(self):
        self.env = Environment.objects.create(code="prod", name="生产")
        self.team = Team.objects.create(name="支付中台")
        self.service = Service.objects.create(
            name="order-api", env=self.env, team=self.team, base_url="http://x"
        )

    def test_issue_token_keeps_ciphertext_only(self):
        plain = issue_token(self.service)
        self.assertEqual(crypto.decrypt(self.service.token_cipher), plain)
        self.assertEqual(self.service.token_hint, plain[-4:])
        self.assertEqual(Service.objects.get(pk=self.service.pk).token_hint, plain[-4:])

    def test_reissue_grants_grace_period(self):
        old_plain = issue_token(self.service)
        new_plain = issue_token(self.service)

        self.assertEqual(crypto.try_decrypt(self.service.token_cipher), new_plain)
        # 旧 Token 进入宽限期，且过期时间在未来
        self.assertEqual(crypto.try_decrypt(self.service.previous_token_cipher), old_plain)
        self.assertTrue(self.service.previous_token_valid)
        self.assertGreater(
            self.service.previous_token_expires_at,
            timezone.now() + timedelta(minutes=5),
        )

    def test_first_issue_has_no_previous(self):
        issue_token(self.service)
        self.assertIsNone(self.service.previous_token_cipher)
        self.assertIsNone(self.service.previous_token_expires_at)


class ApiTests(TestCase):
    def setUp(self):
        self.env = Environment.objects.create(code="prod", name="生产")
        self.team = Team.objects.create(name="基础架构")
        self.service = Service.objects.create(
            name="user-api", env=self.env, team=self.team, base_url="http://127.0.0.1:8000",
            health_path="/ping/",
        )

    def test_service_list_hides_ciphertext(self):
        issue_token(self.service)
        response = self.client.get(reverse("service-list"))
        data = response.json()["results"][0]
        self.assertNotIn("token_cipher", data)
        self.assertEqual(data["token_hint"], Service.objects.get(pk=self.service.pk).token_hint)
        self.assertEqual(data["health_url"], "http://127.0.0.1:8000/ping/")

    def test_manual_check_records_log(self):
        result = probes.ProbeResult(ok=True, status_code=200, latency_ms=10, error="")
        with mock.patch.object(probes, "probe_service", return_value=result):
            response = self.client.post(reverse("service-check", args=[self.service.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status_code"], 200)

        log = HealthCheckLog.objects.get(service=self.service)
        self.assertTrue(log.ok)
        self.assertEqual(log.trigger, HealthCheckLog.Trigger.MANUAL)

    def test_manual_check_failure_is_200_with_ok_false(self):
        result = probes.ProbeResult(
            ok=False, status_code=None, latency_ms=2000, error="connection refused"
        )
        with mock.patch.object(probes, "probe_service", return_value=result):
            response = self.client.post(reverse("service-check", args=[self.service.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["error"], "connection refused")

    def test_rotate_token_returns_plaintext_once(self):
        response = self.client.post(reverse("service-rotate-token", args=[self.service.pk]))
        self.assertEqual(response.status_code, 200)
        token = response.json()["token"]
        # 视图里签发的是数据库中的记录，本地实例需要刷新后再验证密文
        self.service.refresh_from_db()
        self.assertEqual(crypto.decrypt(self.service.token_cipher), token)
        self.assertEqual(response.json()["token_hint"], token[-4:])

    def test_middleware_sets_latency_header(self):
        response = self.client.get(reverse("ping"))
        self.assertEqual(response.status_code, 200)
        header = response.headers.get("X-Request-Duration-Ms")
        self.assertIsNotNone(header)
        self.assertGreaterEqual(float(header), 0.0)
