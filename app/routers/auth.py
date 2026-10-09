import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERS = {
    "admin": {"password": "admin123", "name": "管理员", "role": "admin"},
    "operator": {"password": "123456", "name": "操作员", "role": "operator"},
}

TOKENS: dict[str, dict] = {}


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
    TOKENS[token] = {
        "username": req.username,
        "name": user["name"],
        "role": user["role"],
        "expires": datetime.now() + timedelta(hours=24),
    }
    return LoginResponse(token=token, name=user["name"], role=user["role"])


@router.get("/me", response_model=UserInfo)
def get_me(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = TOKENS.get(token)
    if not info or info["expires"] < datetime.now():
        raise HTTPException(401, "未登录或已过期")
    return UserInfo(username=info["username"], name=info["name"], role=info["role"])


def get_current_user(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = TOKENS.get(token)
    if not info or info["expires"] < datetime.now():
        raise HTTPException(401, "未登录或已过期")
    return info
