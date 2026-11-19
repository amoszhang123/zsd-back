import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.invoice_storage import ensure_root
from app.routers import (
    health, dashboard, orders, contracts, quotes, shipments, production,
    employees, auth, mp, quality, costs, materials, machines, bank, permissions,
    mp_materials, cash, revenue, overview, attendance,
)
from app.routers.auth import get_current_user, PLAINTEXT_USERS, USING_DEFAULT_USERS


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _log_login_switches()
    # 演示数据默认不灌，避免清空数据库后被自动填回来。需要时设 AUTO_SEED=true
    if settings.auto_seed:
        from app.seed import seed_if_empty
        seed_if_empty()
    yield


def _log_login_switches() -> None:
    """把两个登录开关的状态打到启动日志里。

    「固定验证码登录」关闭时接口按设计返回 404，和「路由不存在」长得一模一样，
    曾经让人在生产上排查了半天。启动时打一行，`docker compose logs web | grep 登录`
    就能确认到底是没开开关、还是代码没上线。
    """
    log = logging.getLogger("uvicorn.error")
    if settings.mp_sms_test_enabled:
        log.warning(
            "短信登录已开启：仅 %s 可用固定验证码登录（提审期间用，审核结束请关闭）",
            settings.mp_sms_test_phone or "<未配置手机号，实际无人可用>",
        )
    else:
        log.info("短信登录未开启：/mp/sms-code 与 /mp/sms-login 返回 404")


app = FastAPI(
    title=settings.app_name,
    debug=settings.app_debug,
    lifespan=lifespan,
)

# 跨域白名单：只放行自己人。
# 之前是 ["*"]，等于任何网站都能在浏览器里直接调后端接口。
# 默认值覆盖线上域名与本地开发；要加新域名改 .env 的 CORS_ORIGINS，不用改代码。
_DEFAULT_ORIGINS = [
    "https://zsd-fab.com",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
]
_cors_origins = [o.strip() for o in (settings.cors_origins or "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or _DEFAULT_ORIGINS,
    # 允许携带凭据时不能用 *，这里已明确列出白名单，
    # 而前端用的是 Authorization 头而不是 cookie，所以关掉 credentials 也不影响
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """三个基础安全响应头。

    * nosniff：浏览器别自己猜类型 —— 上传目录里放一个 .html 伪装成图片，
      猜错类型就可能被当脚本执行；
    * DENY：别把后台嵌进别人的 iframe（点击劫持：透明覆盖一层，诱导你点删除）；
    * Referrer-Policy：跳到外站时别把内网地址带出去。

    不动 uploads 的 Content-Type：StaticFiles 已经按扩展名给了正确类型。
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response

# 批量订单码一次回 88 页 base64（约 1.6MB），生产带宽只有 ~0.5MB/s；
# base64 有 33% 的膨胀是纯冗余，gzip 能压掉约三成（实测 1482KB → 1046KB）。
# 放在 CORS 之后注册 = 包在外层，先压缩再补 CORS 头。
app.add_middleware(GZipMiddleware, minimum_size=1024)

# 统一鉴权：业务路由一律要求登录。
#
# 白名单只有三处：
#   * auth.router —— 登录接口本身（内部对 /me、/logout 有自己的校验）
#   * health.router —— 健康检查，容器和反代要用
#   * mp.router / mp_materials.router —— 小程序走自己的 token（_get_mp_user / _require_admin）
#   * /api/uploads —— 图片靠 <img src> 加载，浏览器发这种请求带不了 Authorization
#
# 用 include_router 的 dependencies 而不是逐个端点加：漏一个端点就是一个洞，
# 而这里写一次就全覆盖，新增路由只要别加进白名单就自动受保护。
AUTH = [Depends(get_current_user)]

app.include_router(health.router)
app.include_router(dashboard.router, dependencies=AUTH)
app.include_router(orders.router, dependencies=AUTH)
app.include_router(contracts.router, dependencies=AUTH)
app.include_router(quotes.router, dependencies=AUTH)
app.include_router(shipments.router, dependencies=AUTH)
# 对账单打印页：浏览器直接打开 URL，带不了 header，改用自己的 ?token= 校验
app.include_router(shipments.public_router)
app.include_router(production.router, dependencies=AUTH)
app.include_router(quality.router, dependencies=AUTH)
app.include_router(employees.router, dependencies=AUTH)
app.include_router(auth.router)
app.include_router(mp.router)
# 小程序的物料领取接口（员工视角）。必须排在后端 mp 之后注册：
# 它 import 了 mp 里的 _get_mp_user / _current_employee，顺序反过来会循环导入。
app.include_router(mp_materials.router)
app.include_router(costs.router, dependencies=AUTH)
# 经营管理：费用收支（非订单收付款）、订单收入（出货额确认）、月度概览
app.include_router(cash.router, dependencies=AUTH)
app.include_router(revenue.router, dependencies=AUTH)
app.include_router(overview.router, dependencies=AUTH)
app.include_router(materials.router, dependencies=AUTH)
app.include_router(machines.router, dependencies=AUTH)
app.include_router(bank.router, dependencies=AUTH)
app.include_router(permissions.router, dependencies=AUTH)
# 考勤（一期：打卡/请假/日历，不含工资）
app.include_router(attendance.router, dependencies=AUTH)

# 发票图片：直接按 /api/uploads/invoices/2026/11/<uuid>.jpg 直出。
# 挂在 /api 前缀下是为了复用现有反代（线上 /api 已经指向本服务），
# 不用再给 /uploads 单独加一条 nginx 规则 —— 少一条规则就少一处线上配不对的可能。
#
# 不做鉴权是刻意的：/api/costs 本身至今就是公开的（匿名就能读到全部金额），
# 单给图片加 token 属于自欺欺人。真要收口应该整块给 costs 加登录态，另排期。
app.mount(
    "/api/uploads",
    StaticFiles(directory=str(ensure_root())),
    name="uploads",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)
