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

    ensure_schema()

    # 工单号（work_orders.code，14 位开单时间戳）是后加的列：ensure_schema 只能把它
    # 补成空串，历史单还得在这里补齐，否则旧单在界面上会显示成空工单号。
    # 幂等：只处理 code 为空的行，补过的不会再动，多 worker 同时启动也不会互相踩。
    # 失败不阻断启动 —— 少一个工单号的服务，总比起不来的服务好。
    try:
        from app.work_order_code import backfill_codes

        filled = backfill_codes()
        if filled["filled"]:
            import logging

            logging.getLogger(__name__).info(
                "开工单号回填完成：%s 张（共 %s 张空号）",
                filled["filled"],
                filled["total"],
            )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "开工单号回填失败，可手动重跑：python -m app.work_order_code"
        )

    # fulfilled_at 是后加的列：之前开过送货单、而且已经出满的工单要补上完结时间，
    # 否则它们会卡在「待出货（已出 16/16）」——既不算待出货，也不算已完结。
    # 幂等：只补 fulfilled_at 为空且累计出货量够数的行。
    try:
        from app.models.shipment import backfill_fulfilled

        fixed = backfill_fulfilled()
        if fixed:
            import logging

            logging.getLogger(__name__).info("出货完结状态回填：%s 张工单", fixed)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("出货完结状态回填失败，不影响启动")

    # ship_date 是后加的列：之前开出的送货单要按开单当天补上出货日期，
    # 否则 _to_out 每次都得回落到 created_at，改过的和没改过的在库里分不清。
    try:
        from app.models.shipment import backfill_ship_date

        filled = backfill_ship_date()
        if filled:
            import logging

            logging.getLogger(__name__).info("出货日期回填：%s 张送货单", filled)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("出货日期回填失败，不影响启动")


def _column_ddl(table: str, col) -> str:
    """把模型上的一列渲染成 MySQL 的 ADD COLUMN 语句。

    nullable=False 且没有 server_default 的列，在已有数据的表上是加不进去的
    （MySQL 严格模式会报 "Invalid default value"），所以按类型补一个兜底默认值。
    Python 侧的 default（如 default=datetime.now）不会出现在 DDL 里，这里没有忽略它，
    只是它只对之后的写入生效，存量行仍需这个兜底值。
    """
    from sqlalchemy.schema import CreateColumn

    ddl = str(CreateColumn(col).compile(dialect=engine.dialect))
    if not col.nullable and col.server_default is None:
        lit = _TYPE_DEFAULT.get(type(col.type).__name__.upper())
        if lit is None:
            # 复合类型（枚举、JSON 等）拿不准，就地放宽为可空，宁可少约束也别卡住启动
            ddl = ddl.replace(" NOT NULL", "")
        else:
            ddl += f" DEFAULT {lit}"
    return f"ALTER TABLE `{table}` ADD COLUMN {ddl}"


_TYPE_DEFAULT = {
    "STRING": "''", "VARCHAR": "''", "TEXT": "''", "CHAR": "''",
    "INTEGER": "0", "BIGINT": "0", "SMALLINT": "0", "TINYINT": "0",
    "BOOLEAN": "0",
    "NUMERIC": "0", "DECIMAL": "0", "FLOAT": "0", "REAL": "0", "DOUBLE": "0",
    "DATETIME": "CURRENT_TIMESTAMP", "DATE": "'1970-01-01'", "TIME": "'00:00:00'",
}


def ensure_schema(apply: bool = True) -> dict[str, list[str]]:
    """把数据库结构对齐到模型定义：只加表、只加列，绝不删列或改类型。

    create_all 只建「缺失的表」，从不 ALTER「已存在的表」；而 deploy.sh 只有
    git pull + 重建镜像，没有迁移步骤。生产上因此连着踩了两次：缺 is_test 列让
    所有业务查询 500，缺 machines.model 让机台种子直接写不进去。

    幂等：已有的列跳过；多 worker 同时启动撞 Duplicate column 也当成功。
    DDL 失败只记日志，不阻断启动 —— 少一列的服务总比起不来的服务好。
    返回 {"added": [...], "failed": [...], "extra": [...]}；extra 是库里有、模型里
    没有的列（比如历史遗留的 work_orders.machine_id），只报告，不动。
    """
    import logging

    import app.models  # noqa: F401  确保所有模型都已注册到 metadata
    from sqlalchemy import inspect

    result: dict[str, list[str]] = {"added": [], "failed": [], "extra": []}
    try:
        insp = inspect(engine)
        existing_tables = set(insp.get_table_names())
        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue  # 缺表由 create_all 负责
            columns = insp.get_columns(table_name)
            have = {c["name"] for c in columns}
            for col in table.columns:
                if col.name in have:
                    continue
                if not apply:
                    result["added"].append(f"{table_name}.{col.name}")
                    continue
                try:
                    with engine.begin() as conn:
                        conn.execute(text(_column_ddl(table_name, col)))
                    result["added"].append(f"{table_name}.{col.name}")
                except Exception as e:  # 并发 ADD 撞 Duplicate column 属正常
                    result["failed"].append(f"{table_name}.{col.name}: {str(e)[:90]}")
            result["extra"].extend(
                f"{table_name}.{c}" for c in sorted(have - {c.name for c in table.columns})
            )
        if apply and result["added"]:
            logging.warning("表结构已补齐: %s", ", ".join(result["added"]))
        if apply:
            for item in result["failed"]:
                logging.warning("补列失败（并发时属正常）: %s", item)
    except Exception as e:
        logging.warning("检查表结构失败，跳过: %s", e)
    return result
