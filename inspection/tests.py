"""关键链路的回归测试。

重点覆盖三类「最容易被改动破坏」的行为：
1. 加密链路：密文可逆、篡改不可解、明文不留痕；
2. 统计链路：条件聚合的正确性、零流量服务的补行、幂等重跑；
3. 接口链路：手动探测落库、新建服务自动签发 Token、中间件响应头。
"""
import sys
from datetime import datetime, timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import crypto
from inspection import probes
from inspection.models import DailyHealthStat, Environment, HealthCheckLog, Service, Team
from inspection.services import build_daily_stats, issue_token, rotate_all_tokens


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

    def test_rotate_covers_disabled_services(self):
        """题目要求重置「每个服务」的 Token，停用的服务也不能漏。"""
        self.service.enabled = False
        self.service.save(update_fields=["enabled"])
        self.assertIsNone(self.service.token_cipher)

        result = rotate_all_tokens()
        self.service.refresh_from_db()
        self.assertEqual(result["rotated"], Service.objects.count())
        self.assertIsNotNone(self.service.token_cipher)
        self.assertEqual(len(crypto.decrypt(self.service.token_cipher)), 43)


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

    def test_create_returns_issued_token_plaintext(self):
        payload = {
            "name": "search-api",
            "env": self.env.pk,
            "team": self.team.pk,
            "base_url": "http://127.0.0.1:8000",
            "health_path": "api/ping/",
        }
        response = self.client.post(
            reverse("service-list"), payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        service = Service.objects.get(pk=body["id"])
        # 明文随创建响应返回一次，且与库中密文对应
        self.assertEqual(crypto.decrypt(service.token_cipher), body["token"])
        self.assertEqual(service.token_hint, body["token"][-4:])
        self.assertNotIn("token_cipher", body)

    def test_invalid_query_params_are_ignored_not_500(self):
        for url in (
            "/api/services/?env=abc&team=x&enabled=whatever&q=%E5%95%A6",
            "/api/checks/?service=abc&ok=maybe&trigger=hmm",
            "/api/stats/?date=not-a-date&env=abc&service=x",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)


class DashboardTests(TestCase):
    def setUp(self):
        self.env = Environment.objects.create(code="prod", name="生产")
        self.team = Team.objects.create(name="基础架构")
        self.service = Service.objects.create(
            name="user-api", env=self.env, team=self.team, base_url="http://x"
        )
        HealthCheckLog.objects.create(
            service=self.service, ok=True, status_code=200, latency_ms=10
        )
        DailyHealthStat.objects.create(
            stat_date=timezone.localdate() - timedelta(days=1),
            env=self.env,
            team=self.team,
            service=self.service,
            total=10,
            ok_count=9,
            health_rate=90.0,
        )

    def test_dashboard_renders_overview(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("运维控制台", content)
        self.assertIn("user-api", content)
        self.assertIn("X-CSRFToken", content)
        self.assertIn("90.0%", content)

    def test_trigger_task_uses_whitelist(self):
        fake = mock.Mock(id="task-123")
        with mock.patch("inspection.web.online_workers", return_value={"w1": {}}), \
             mock.patch("inspection.web.tasks.inspect_all_services.delay", return_value=fake) as call:
            response = self.client.post("/web/task/inspect/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["task_id"], "task-123")
        self.assertEqual(response.json()["mode"], "async")
        call.assert_called_once_with()

        bad = self.client.post("/web/task/delete_everything/")
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.json()["ok"])

    def test_trigger_task_sync_fallback_without_worker(self):
        """worker 没启动也要有反馈：直接在当前进程执行任务。"""
        eager = mock.Mock(result={"checked": 9, "ok": 7, "failed": 2})
        with mock.patch("inspection.web.online_workers", return_value={}), \
             mock.patch("inspection.web.tasks.inspect_all_services.apply", return_value=eager):
            response = self.client.post("/web/task/inspect/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "sync")
        self.assertEqual(body["result"], {"checked": 9, "ok": 7, "failed": 2})

    def test_worker_status_endpoint(self):
        with mock.patch("inspection.web.online_workers", return_value={"w1@host": {}}):
            response = self.client.get("/web/status/")
        self.assertTrue(response.json()["online"])
        self.assertEqual(response.json()["workers"], ["w1@host"])

    @override_settings(DEBUG=True)
    def test_start_worker_launches_both_processes(self):
        # 成功判定：日志有 ready + 记录的 PID 存活，不再依赖 inspect 广播
        with mock.patch("inspection.web.find_python", return_value=sys.executable), \
             mock.patch("inspection.web.subprocess.run") as check, \
             mock.patch("inspection.web.subprocess.Popen") as popen, \
             mock.patch("inspection.web.online_workers", return_value={}), \
             mock.patch("inspection.web.read_worker_state", side_effect=[{}, {"pid": 12345}]), \
             mock.patch("inspection.web.worker_ready_in_log", return_value=True), \
             mock.patch("inspection.web.process_alive", return_value=True):
            check.return_value.returncode = 0
            popen.return_value.pid = 12345
            response = self.client.post("/web/start/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("nodename", response.json())
        self.assertEqual(popen.call_count, 2)

    @override_settings(DEBUG=False)
    def test_start_worker_forbidden_in_production(self):
        response = self.client.post("/web/start/")
        self.assertEqual(response.status_code, 403)

    def test_worker_logs_endpoint(self):
        response = self.client.get("/web/logs/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("worker", response.json())
        self.assertIn("beat", response.json())

    @override_settings(
        PYTHON_EXECUTABLE="",
        CELERY_BROKER_URL="redis://127.0.0.1:6379/0",
    )
    def test_web_crud_full_flow(self):
        """页面用的 CRUD 端点：建服务（带 Token）→ 改 → 停 → 删。"""
        payload = {
            "name": "crud-api", "env": self.env.pk, "team": self.team.pk,
            "base_url": "http://127.0.0.1:8000/", "health_path": "ping/",
            "timeout_seconds": 2.5,
        }
        created = self.client.post(
            "/web/services/create/", payload, content_type="application/json"
        ).json()
        self.assertTrue(created["ok"])
        self.assertEqual(len(created["token"]), 43)

        sid = created["id"]
        svc = Service.objects.get(pk=sid)
        self.assertEqual(svc.health_path, "/ping/")
        self.assertEqual(crypto.decrypt(svc.token_cipher), created["token"])

        updated = self.client.post(
            f"/web/services/{sid}/update/", {"name": "crud-api-v2", "enabled": False},
            content_type="application/json",
        ).json()
        self.assertTrue(updated["ok"])
        svc.refresh_from_db()
        self.assertEqual(svc.name, "crud-api-v2")
        self.assertFalse(svc.enabled)

        toggled = self.client.post(f"/web/services/{sid}/toggle/", content_type="application/json").json()
        self.assertTrue(toggled["enabled"])

        listed = self.client.get("/web/services/?q=crud").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["results"][0]["token_hint"], created["token"][-4:])

        deleted = self.client.post(f"/web/services/{sid}/delete/", content_type="application/json").json()
        self.assertTrue(deleted["ok"])
        self.assertFalse(Service.objects.filter(pk=sid).exists())

    def test_web_org_and_query_endpoints(self):
        team = self.client.post(
            "/web/teams/save/", {"name": "新团队", "owner": "张三"},
            content_type="application/json",
        ).json()
        self.assertTrue(team["ok"])
        # 重名拒绝
        dup = self.client.post(
            "/web/teams/save/", {"name": "新团队"}, content_type="application/json"
        )
        self.assertEqual(dup.status_code, 400)

        env = self.client.post(
            "/web/environments/save/", {"code": "test", "name": "测试", "sort_order": 2},
            content_type="application/json",
        ).json()
        self.assertTrue(env["ok"])
        teams = self.client.get("/web/teams/").json()["results"]
        new_team = next(t for t in teams if t["name"] == "新团队")
        self.assertEqual(new_team["service_count"], 0)
        self.assertEqual(self.client.get("/web/environments/").json()["ok"], True)

        checks = self.client.get("/web/checks/?ok=1&page=1").json()
        self.assertIn("pages", checks)
        self.assertTrue(all(r["ok"] for r in checks["results"]))

        stats = self.client.get("/web/stats/").json()
        self.assertIn("dates", stats)
        self.assertIn("summary", stats)

    def test_find_python_skips_broken_interpreter(self):
        """3.13 能 import 但 kombu 连不上 broker（redis-py 版本低），必须被跳过。"""
        from inspection.web import find_python

        py313 = r"C:\Users\23000\AppData\Local\Programs\Python\Python313\python.exe"
        py312 = r"C:\Users\23000\AppData\Local\Programs\Python\Python312\python.exe"
        outcomes = [mock.Mock(returncode=1), mock.Mock(returncode=0), mock.Mock(returncode=1)]
        with mock.patch("inspection.web.Path.exists", return_value=True), \
             mock.patch("inspection.web.subprocess.run", side_effect=outcomes) as run:
            chosen = find_python()
        self.assertEqual(chosen, py312)
        self.assertEqual(run.call_count, 2)  # 第一个失败后试第二个，成功即止
        self.assertIn("ensure_connection", run.call_args_list[0].args[0][-1])
