from fastapi import Header
from sqlalchemy import Boolean, create_engine, event, text, true
from sqlalchemy.orm import Session, sessionmaker, DeclarativeBase, Mapped, mapped_column, with_loader_criteria

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class TenantScoped:
    """测试数据域标记（混入类）。

    微信审核用的测试账号登录后产生的所有业务数据都带 is_test=1，与真实生产数据
    共用同一套表、同一套编号规则，但两条数据流永远互相看不见。

    做成混入类而不是逐表加列，是为了让 Session 上的全局过滤能用
    with_loader_criteria(TenantScoped, ...) 一次覆盖全部继承它的实体 ——
    新增业务表只要继承这个混入类，就自动进入隔离范围，不需要再改任何查询。
    """

    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# 只查列、不查实体的语句（func.sum/count、db.query(Order.id)）不会走
# with_loader_criteria，必须由调用方显式补条件，用 scoped() 取。
def scoped(db: Session, model):
    """返回当前会话数据域的过滤条件；会话未指定数据域时返回恒真条件。

    用法：db.query(func.sum(Order.estimated_hours)).filter(scoped(db, Order))
    """
    flag = db.info.get("is_test")
    if flag is None:
        return true()
    return model.is_test == flag


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state):
    """给每个查询自动追加 is_test 条件。

    get_db() 会按请求的 token 在 session.info 里写好 is_test（生产域写 False，
    测试域写 True）；没有这个键的会话（种子脚本、运维脚本）不做任何过滤。
    """
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    flag = state.session.info.get("is_test")
    if flag is None:
        return
    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped, lambda cls: cls.is_test == flag, include_aliases=True
        )
    )


@event.listens_for(Session, "before_flush")
def _stamp_tenant(session, flush_context, instances):
    """新建对象自动打上当前会话的数据域标记，业务代码不用管这件事。"""
    flag = session.info.get("is_test")
    if flag is None:
        return
    for obj in session.new:
        if isinstance(obj, TenantScoped):
            obj.is_test = flag


def _token_is_test(authorization: str) -> bool:
    """从 Authorization 头判断这次请求属于哪个数据域。

    解析失败/无 token 一律按生产域处理：匿名接口本来就读不到业务数据，
    而「拿不准就当生产」比「拿不准就当测试」安全 —— 后者会把生产数据藏起来。
    """
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        return False
    from app.routers.auth import _get_token  # 局部导入，避免 database ↔ auth 循环

    info = _get_token(token) or {}
    return bool(info.get("is_test"))


def get_db(authorization: str = Header("")):
    db = SessionLocal()
    db.info["is_test"] = _token_is_test(authorization)
    try:
        yield db
    finally:
        db.close()


def init_db():
    import pymysql
    conn = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.db_name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()

    # 建表：多 worker 并发时会撞 "Table already exists"，忽略即可
    try:
        from app.models import order, production
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        import logging
        logging.warning("create_all 并发冲突，已忽略: %s", e)

    # from app.models import order, production  # noqa: F401
    # Base.metadata.create_all(bind=engine)
