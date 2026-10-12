import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERS = {
    "admin": {"password": "admin123", "name": "管理员", "role": "admin"},
    "operator": {"password": "123456", "name": "操作员", "role": "operator"},
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


class UserInfo(BaseModel):
    username: str
    name: str
    role: str


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    user = USERS.get(req.username)
    if not user or user["password"] != req.password:
        raise HTTPException(401, "用户名或密码错误")
    token = secrets.token_hex(24)
    _set_token(token, {
        "username": req.username,
        "name": user["name"],
        "role": user["role"],
        "expires": (datetime.now() + timedelta(hours=24)).isoformat(),
    })
    return LoginResponse(token=token, name=user["name"], role=user["role"])


@router.get("/me", response_model=UserInfo)
def get_me(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = _get_token(token)
    if not info:
        raise HTTPException(401, "未登录或已过期")
    return UserInfo(username=info["username"], name=info["name"], role=info["role"])


def get_current_user(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = _get_token(token)
    if not info:
        raise HTTPException(401, "未登录或已过期")
    return info
