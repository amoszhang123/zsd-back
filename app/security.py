"""安全基础设施：密码哈希、密码指纹、登录失败限速。

为什么自己写而不是引 passlib：只需要 scrypt 一种算法，标准库 hashlib 就有，
少一个依赖就少一条升级、少一处 CVE。密码哈希格式固定成
``scrypt$<salt_hex>$<hash_hex>``，将来要换算法时靠前缀区分，老哈希仍能校验。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from datetime import datetime, timedelta

# scrypt 参数：n=2^14 在普通服务器上约 30–60ms/次，足够挡住离线爆破，
# 又不会让登录变慢。n 必须是 2 的幂。
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """明文 → ``scrypt$salt$hash``。落进 .env，不再以明文出现在任何地方。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验。stored 不是合法哈希格式时返回 False，不抛异常 ——
    登录失败统一报「账号或密码错」，不能让格式错误变成另一条信息。"""
    try:
        prefix, salt_hex, hash_hex = (stored or "").split("$")
        if prefix != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=len(expected),
        )
    except ValueError:
        return False
    # 定长比较，避免按字节提前返回泄露时序
    return hmac.compare_digest(actual, expected)


def password_fingerprint(stored: str) -> str:
    """密码哈希的短指纹，写进 token。

    改密码 → 指纹变 → 所有旧 token 校验时对不上 → 自动失效。
    不需要维护一张"已吊销 token"的表，也不需要重启。

    取哈希而不是原文：即使 token 泄露，也无法反推密码。
    """
    if not stored:
        return ""
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:16]


def is_hashed(value: str) -> bool:
    return bool(value) and value.startswith("scrypt$") and value.count("$") == 2


class LoginThrottle:
    """登录失败计数。

    按「IP」和「账号」两条线分别计数，任一触发就锁：
      * 只按 IP 锁会误伤 —— 全厂共用一个出口 IP，五个人同时输错就把整个办公室锁了；
      * 只按账号锁挡不住换账号刷（虽然对不存在的账号也计数）。

    计数放内存而不是库：这是防爆破的临时状态，重启丢干净反而正确；
    多 worker 下每个进程各算各的，5 次变 15 次，仍然远小于暴力破解需要的次数。
    """

    def __init__(self, ip_limit: int = 10, user_limit: int = 5, window_minutes: int = 5):
        self.ip_limit = ip_limit
        self.user_limit = user_limit
        self.window = timedelta(minutes=window_minutes)
        self._hits: dict[str, list[datetime]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: datetime) -> list[datetime]:
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked_for(self, ip: str, username: str) -> int:
        """还要等几秒才能再试。0 表示没被锁。"""
        now = datetime.now()
        with self._lock:
            for key, limit in ((f"ip:{ip}", self.ip_limit), (f"user:{username}", self.user_limit)):
                hits = self._recent(key, now)
                if len(hits) >= limit:
                    unlock_at = hits[0] + self.window
                    return max(1, int((unlock_at - now).total_seconds()))
        return 0

    def record_failure(self, ip: str, username: str) -> None:
        now = datetime.now()
        with self._lock:
            for key in (f"ip:{ip}", f"user:{username}"):
                self._recent(key, now)
                self._hits.setdefault(key, []).append(now)

    def clear(self, ip: str, username: str) -> None:
        """登录成功后清掉这个账号的失败计数（IP 的保留，避免同 IP 反复换号试）。"""
        with self._lock:
            self._hits.pop(f"user:{username}", None)


login_throttle = LoginThrottle()


def parse_users(raw: str) -> dict[str, dict]:
    """解析 ``WEB_USERS`` 环境变量。

    格式：``用户名:密码哈希:姓名:角色:岗位``，多个用 ``;`` 分隔，例如
    ``admin:scrypt$...:管理员:admin:管理员;boss:scrypt$...:张总:admin:管理员``

    生产只配哈希；为了方便本地调试，也接受明文密码（不含 ``scrypt$`` 前缀时
    按明文处理并在启动日志里警告）—— 否则每次本地起服务都要先算一遍哈希。
    """
    users: dict[str, dict] = {}
    for chunk in (raw or "").split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        parts = piece.split(":")
        # 哈希里没有冒号（scrypt$salt$hash），所以按 5 段切不会切坏
        if len(parts) < 3:
            continue
        username, secret = parts[0].strip(), parts[1].strip()
        name = parts[2].strip() if len(parts) > 2 else username
        role = parts[3].strip() if len(parts) > 3 else "admin"
        position = parts[4].strip() if len(parts) > 4 else "管理员"
        if not username or not secret:
            continue
        users[username] = {
            "password": secret,
            "name": name or username,
            "role": role or "admin",
            "position": position or "管理员",
        }
    return users


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
