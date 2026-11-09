import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.invoice_storage import ensure_root
from app.routers import (
    health, dashboard, orders, contracts, quotes, shipments, production,
    employees, auth, mp, quality, costs, materials, machines, bank, permissions,
)


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(dashboard.router)
app.include_router(orders.router)
app.include_router(contracts.router)
app.include_router(quotes.router)
app.include_router(shipments.router)
app.include_router(production.router)
app.include_router(quality.router)
app.include_router(employees.router)
app.include_router(auth.router)
app.include_router(mp.router)
app.include_router(costs.router)
app.include_router(materials.router)
app.include_router(machines.router)
app.include_router(bank.router)
app.include_router(permissions.router)

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
