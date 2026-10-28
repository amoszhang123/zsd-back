import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from app.qr_image import render_qr_data_uri

router = APIRouter(prefix="/api/auth", tags=["auth"])

# position 是员工管理里的工种：web 模块可见性按工种配置（见 routers/permissions.py），
# 登录账号在这里绑定一个工种，token 里带上它，前端据此拉可见模块。
USERS = {
    "admin": {"password": "admin123", "name": "管理员", "role": "admin", "position": "管理员"},
    "operator": {"password": "123456", "name": "操作员", "role": "operator", "position": "操作工"},
}

# token 落到共享文件：多个 gunicorn worker 读写同一份，重启也不丢。
# 用 os.replace 原子替换，避免并发写坏文件；expires 存 ISO 字符串（JSON 不能存 datetime）。
_TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "/tmp/zsd_tokens.json"))
_token_lock = threading.Lock()


def _load_tokens() -> dict[str, dict]:
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tokens(data: dict[str, dict]) -> None:
    tmp = _TOKEN_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _TOKEN_FILE)


def _set_token(token: str, info: dict) -> None:
    with _token_lock:
        now = datetime.now().isoformat()
        data = {k: v for k, v in _load_tokens().items()
                if v.get("expires", "") > now}          # 顺手清理过期
        data[token] = info
        _save_tokens(data)


def _get_token(token: str) -> dict | None:
    info = _load_tokens().get(token)
    if not info:
        return None
    if info.get("expires", "") < datetime.now().isoformat():
        return None
    return info


def _delete_token(token: str) -> None:
    with _token_lock:
        data = _load_tokens()
        data.pop(token, None)
        _save_tokens(data)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    name: str
    role: str
    position: str


class UserInfo(BaseModel):
    username: str
    name: str
    role: str
    position: str


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    user = USERS.get(req.username)
    if not user or user["password"] != req.password:
        raise HTTPException(401, "用户名或密码错误")
    # 会话签发与扫码登录共用 _issue_session，两种方式拿到的 token 完全同构
    return _issue_session(req.username, user)


@router.get("/me", response_model=UserInfo)
def get_me(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = _get_token(token)
    if not info:
        raise HTTPException(401, "未登录或已过期")
    return UserInfo(
        username=info["username"],
        name=info["name"],
        role=info["role"],
        position=info.get("position", ""),
    )


def get_current_user(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = _get_token(token)
    if not info:
        raise HTTPException(401, "未登录或已过期")
    return info


def require_admin(user: dict = Depends(get_current_user)):
    """web 端管理员门禁。

    此前 web 侧的 CRUD 路由都没挂鉴权，只有 /api/mp/* 自己校验岗位；权限配置的读写
    会直接决定别人能看到哪些模块，所以这里必须挡住非 admin。
    """
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def _issue_session(username: str, user: dict) -> LoginResponse:
    """签发一个 24 小时的 web 会话。账号密码登录与扫码登录共用，避免两处各写一份。"""
    token = secrets.token_hex(24)
    _set_token(token, {
        "username": username,
        "name": user["name"],
        "role": user["role"],
        "position": user["position"],
        "expires": (datetime.now() + timedelta(hours=24)).isoformat(),
    })
    return LoginResponse(
        token=token, name=user["name"], role=user["role"], position=user["position"]
    )


# ------------------------- web 扫码登录 -------------------------
# 流程：web 登录页建一个一次性挑战并显示二维码 → 小程序「快捷扫码」扫到后调
# /api/mp/qr-login/scan 标记已扫码 → 手机上弹框确认 → /confirm 签发 web 会话 →
# web 轮询 /api/auth/qr-login/{challenge} 取走 token（取走即销毁）。
#
# 二维码内容是 {"type":"web_login","challenge":"..."} 而不是裸 challenge：
# 小程序靠 type 字段把它和订单二维码分开，不会拿去查工单。
QR_LOGIN_TTL = 180  # 二维码有效期（秒），过期由 web 端换一张

QR_STATUS_PENDING = "pending"      # 等待扫码
QR_STATUS_SCANNED = "scanned"      # 已扫码，等手机上确认
QR_STATUS_CONFIRMED = "confirmed"  # 已确认，token 待 web 取走
QR_STATUS_CANCELLED = "cancelled"  # 手机上点了取消
QR_STATUS_EXPIRED = "expired"      # 超时未确认

# 挑战落共享文件，理由与 token 相同：生产是多 worker，建码和轮询可能落在不同进程，
# 用内存字典会出现「刚建的码转头就查不到」。
_QR_LOGIN_FILE = Path(os.getenv("QR_LOGIN_FILE", "/tmp/zsd_qr_login.json"))
_qr_lock = threading.Lock()


def _load_challenges() -> dict[str, dict]:
    try:
        with open(_QR_LOGIN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_challenges(data: dict[str, dict]) -> None:
    tmp = _QR_LOGIN_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _QR_LOGIN_FILE)


def _write_challenge(challenge: str, record: dict) -> None:
    with _qr_lock:
        # 过期超过一个 TTL 的直接丢掉：留着没用，还会让文件无限增长。
        # 刚过期的要保留，否则 web 端轮询只能看到「码不存在」而不是「已过期」。
        cutoff = (datetime.now() - timedelta(seconds=QR_LOGIN_TTL)).isoformat()
        data = {k: v for k, v in _load_challenges().items() if v.get("expires", "") > cutoff}
        data[challenge] = record
        _save_challenges(data)


def _delete_challenge(challenge: str) -> None:
    with _qr_lock:
        data = _load_challenges()
        data.pop(challenge, None)
        _save_challenges(data)


def _read_challenge(challenge: str) -> dict | None:
    """取一条挑战；已过有效期但还停在待扫码/待确认的，就地判成 expired。"""
    record = _load_challenges().get(challenge)
    if not record:
        return None
    if (
        record.get("expires", "") < datetime.now().isoformat()
        and record.get("status") in (QR_STATUS_PENDING, QR_STATUS_SCANNED)
    ):
        record["status"] = QR_STATUS_EXPIRED
    return record


def account_for_position(position: str) -> tuple[str, dict] | None:
    """工种 → web 登录账号（USERS 里每个账号绑定一个工种）。

    扫码登录不造新身份，只是换一种方式登入已有账号：谁扫的码就按他的工种找到
    对应账号，web 端拿到的模块可见性与账号密码登录完全一致。
    没有对应账号的工种返回 None，由调用方拒绝。
    """
    for username, user in USERS.items():
        if user["position"] == position:
            return username, user
    return None


class QrLoginCreateResponse(BaseModel):
    challenge: str
    qr_image: str
    expires_in: int


class QrLoginStatusResponse(BaseModel):
    status: str
    # 仅 confirmed 时有值；web 取走一次后这条挑战即销毁
    token: str = ""
    name: str = ""
    role: str = ""
    position: str = ""


@router.post("/qr-login", response_model=QrLoginCreateResponse)
def create_qr_login():
    """签发一张扫码登录二维码。无需登录 —— 这时用户还没登进来。"""
    challenge = secrets.token_hex(16)
    payload = json.dumps({"type": "web_login", "challenge": challenge}, separators=(",", ":"))
    _write_challenge(challenge, {
        "status": QR_STATUS_PENDING,
        "expires": (datetime.now() + timedelta(seconds=QR_LOGIN_TTL)).isoformat(),
        "phone": "",
        "employee_name": "",
        "username": "",
        "name": "",
        "role": "",
        "position": "",
        "token": "",
    })
    return QrLoginCreateResponse(
        challenge=challenge,
        qr_image=render_qr_data_uri(payload),
        expires_in=QR_LOGIN_TTL,
    )


@router.get("/qr-login/{challenge}", response_model=QrLoginStatusResponse)
def poll_qr_login(challenge: str):
    """web 端轮询扫码进度。

    challenge 本身就是凭证（128 位随机），只有拿到它的人能取到 token；
    取到后立刻销毁这条挑战，同一个码不能反复换会话。
    """
    record = _read_challenge(challenge)
    if not record:
        return QrLoginStatusResponse(status=QR_STATUS_EXPIRED)

    if record["status"] == QR_STATUS_CONFIRMED:
        response = QrLoginStatusResponse(
            status=QR_STATUS_CONFIRMED,
            token=record["token"],
            name=record["name"],
            role=record["role"],
            position=record["position"],
        )
        _delete_challenge(challenge)
        return response

    return QrLoginStatusResponse(status=record["status"])


def scan_qr_login(challenge: str, phone: str, employee_name: str, position: str) -> dict:
    """小程序扫码：按工种匹配 web 账号，标记为「已扫码待确认」。

    只由 routers/mp.py 的鉴权接口调用，扫码人身份取自小程序自己的 token。
    """
    record = _read_challenge(challenge)
    if not record or record["status"] == QR_STATUS_EXPIRED:
        raise HTTPException(400, "二维码已过期，请在网页上刷新后重新扫描")
    if record["status"] == QR_STATUS_CANCELLED:
        raise HTTPException(400, "这次登录已取消，请在网页上刷新二维码")
    if record["status"] != QR_STATUS_PENDING:
        raise HTTPException(409, "该二维码已被扫描")

    matched = account_for_position(position)
    if not matched:
        raise HTTPException(403, f"工种「{position}」还没有对应的网页登录账号，不能扫码登录")
    username, user = matched

    record.update({
        "status": QR_STATUS_SCANNED,
        "phone": phone,
        "employee_name": employee_name,
        "username": username,
        "name": user["name"],
        "role": user["role"],
        "position": user["position"],
    })
    _write_challenge(challenge, record)
    return {
        "account_name": user["name"],
        "username": username,
        "position": user["position"],
        "employee_name": employee_name,
    }


def confirm_qr_login(challenge: str, phone: str) -> str:
    """手机上确认登录，签发 web 会话，返回登录账号的显示名。"""
    record = _read_challenge(challenge)
    if not record or record["status"] == QR_STATUS_EXPIRED:
        raise HTTPException(400, "二维码已过期，请在网页上刷新后重新扫描")
    if record["status"] == QR_STATUS_CANCELLED:
        raise HTTPException(400, "这次登录已取消，请在网页上刷新二维码")
    if record["status"] != QR_STATUS_SCANNED:
        raise HTTPException(409, "该二维码还没被扫描，或已经确认过了")
    # 只认扫码本人：否则谁拿到 challenge 都能替别人确认登录
    if record["phone"] != phone:
        raise HTTPException(403, "只有扫码本人可以确认这次登录")

    session = _issue_session(record["username"], USERS[record["username"]])
    record.update({"status": QR_STATUS_CONFIRMED, "token": session.token})
    _write_challenge(challenge, record)
    return session.name


def cancel_qr_login(challenge: str, phone: str) -> None:
    """手机上取消登录，让 web 端立刻停止等待，而不是干等到二维码过期。"""
    record = _read_challenge(challenge)
    if not record or record["status"] not in (QR_STATUS_PENDING, QR_STATUS_SCANNED):
        return
    if record["phone"] and record["phone"] != phone:
        raise HTTPException(403, "只有扫码本人可以取消这次登录")
    record["status"] = QR_STATUS_CANCELLED
    _write_challenge(challenge, record)
