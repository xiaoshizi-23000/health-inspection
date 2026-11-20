"""Celery 应用。

注意：这里只负责创建 app 并绑定 Django settings，任务本体在 inspection/tasks.py，
由 autodiscover_tasks() 自动加载。
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("health_inspection")

# 所有 CELERY_ 前缀的配置都从 Django settings 读取，避免两处维护。
app.config_from_object("django.conf:settings", namespace="CELERY")

# 自动发现每个 app 下的 tasks.py
app.autodiscover_tasks()
