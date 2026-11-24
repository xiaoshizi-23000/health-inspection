"""Django 配置。

所有可变项都走环境变量，方便赛后直接换成 PostgreSQL / 独立 Redis。
"""
import os
from pathlib import Path

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-key-please-change-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in (env("DJANGO_ALLOWED_HOSTS", "*") or "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
    "inspection",
]

MIDDLEWARE = [
    # 放在最外层：Django 的 MIDDLEWARE 顺序是「由外向内」，
    # 只有排第一才能把后面所有中间件 + 视图的耗时都算进来。
    "core.middleware.RequestLatencyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------
# 数据库：默认 SQLite，切 PostgreSQL 只需改这一处
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

# --------------------------------------------------------------------------
# 国际化 / 时区
# 这两行和 Celery 的 00:00 统计直接相关，写错会让定时任务整体偏 8 小时。
# --------------------------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# DRF
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DATETIME_FORMAT": "%Y-%m-%d %H:%M:%S",
}

# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------
# 一键启动后台服务时优先使用的解释器（必须装齐 django/celery/drf 且 redis-py >= 6.1）
# 留空则自动探测：依次尝试当前解释器、Python312 安装路径，选第一个能连上 broker 的。
PYTHON_EXECUTABLE = env("PYTHON_EXECUTABLE", "")

CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

# 关键：让 crontab(hour=0) 落在北京时间 00:00，而不是 UTC 00:00。
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = False

CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200

CELERY_BEAT_SCHEDULE = {
    # 每 8 小时轮换一次巡检 Token（00:00 / 08:00 / 16:00）
    "rotate-service-tokens": {
        "task": "inspection.tasks.rotate_service_tokens",
        "schedule": crontab(minute=0, hour="*/8"),
    },
    # 每 10 分钟对全部启用服务做一次巡检，沉淀健康率数据
    "inspect-all-services": {
        "task": "inspection.tasks.inspect_all_services",
        "schedule": crontab(minute="*/10"),
    },
    # 每天 00:00 统计「昨天」的健康率并入库
    "daily-health-stat": {
        "task": "inspection.tasks.daily_health_stat",
        "schedule": crontab(minute=0, hour=0),
    },
    # 每天凌晨清理过期巡检明细，避免日志表无限膨胀
    "cleanup-health-logs": {
        "task": "inspection.tasks.cleanup_old_logs",
        "schedule": crontab(minute=30, hour=3),
    },
}

# --------------------------------------------------------------------------
# 业务参数
# --------------------------------------------------------------------------
# Fernet 密钥种子。生产环境必须由 KMS / 环境变量注入，绝不进代码库。
FERNET_KEY = env("FERNET_KEY", "")

# 轮换后旧 Token 的宽限期（秒）：避免轮换瞬间正在并发巡检的请求全部 401。
INSPECTION_TOKEN_GRACE_SECONDS = int(env("INSPECTION_TOKEN_GRACE_SECONDS", "600"))

# 全量巡检的并发线程数
INSPECTION_MAX_WORKERS = int(env("INSPECTION_MAX_WORKERS", "16"))

# 慢请求阈值（毫秒），超过则单独打 WARNING
INSPECTION_SLOW_REQUEST_MS = int(env("INSPECTION_SLOW_REQUEST_MS", "500"))

# 巡检明细保留天数
INSPECTION_LOG_RETENTION_DAYS = int(env("INSPECTION_LOG_RETENTION_DAYS", "30"))

# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
(BASE_DIR / "logs").mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)-7s %(name)s | %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "app.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "plain",
            "encoding": "utf-8",
            "delay": True,
        },
    },
    "loggers": {
        "core.request": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "inspection": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
    },
}
