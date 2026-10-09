from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import (
    health, dashboard, orders, contracts, quotes, shipments, production,
    employees, auth, mp, quality, costs, bank,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 演示数据默认不灌，避免清空数据库后被自动填回来。需要时设 AUTO_SEED=true
    if settings.auto_seed:
        from app.seed import seed_if_empty
        seed_if_empty()
    yield


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
app.include_router(bank.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)
