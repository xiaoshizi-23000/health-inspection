"""敏感字段加解密。

设计要点：
1. 明文只在「生成」和「使用」两个瞬间存在于内存，落库一律是密文；
2. 密钥不落库，从环境变量 / KMS 注入，库被拖走也解不开；
3. 额外存一个尾号 hint，用于人工核对「我现在用的是哪一版 Token」，
   避免为了看一眼 Token 就把密文解开。
"""
from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

TOKEN_BYTES = 32


def _fernet() -> Fernet:
    """从配置派生 Fernet 密钥。

    Fernet 要求 32 字节的 urlsafe-base64 密钥，直接让使用者填这种字符串容易写错，
    所以这里统一做一次 SHA-256 派生：填任意长随机串都能用。
    生产环境应改为直接读取 KMS 下发的 key。
    """
    seed = getattr(settings, "FERNET_KEY", "") or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(plain: str) -> bytes:
    if not isinstance(plain, str):
        raise TypeError("encrypt() 只接受 str")
    return _fernet().encrypt(plain.encode("utf-8"))


def decrypt(cipher: bytes | memoryview) -> str:
    """解密失败抛 InvalidToken，由调用方决定是降级还是告警。"""
    return _fernet().decrypt(bytes(cipher)).decode("utf-8")


def try_decrypt(cipher: bytes | memoryview | None) -> str | None:
    if not cipher:
        return None
    try:
        return decrypt(cipher)
    except (InvalidToken, ValueError):
        return None


def generate_token() -> str:
    """生成巡检 Token：32 字节随机数 → 43 字符 urlsafe 字符串。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hint(plain: str) -> str:
    """只留尾 4 位，用于列表页展示和人工核对。"""
    return plain[-4:] if len(plain) >= 4 else plain
