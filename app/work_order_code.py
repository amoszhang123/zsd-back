"""开工单号（工单号）：14 位「开单日期时间戳」。

格式 `20261110151301` = 2026-11-10 15:13:01，在建单那一刻生成并写进
`work_orders.code`。界面上一律显示它，自增 `id` 退回去只对内用。

**为什么落库，而不是展示时从 created_at 现算**
单号要写在图纸/工作票上、要在群里口头报、要对得上历史记录，所以必须唯一且稳定。
现算的话同一秒开的两张单会显示成完全一样的号；而且小程序、web、以后任何导出
都得各自再实现一遍格式化。落库之后只有一个来源。

**唯一性怎么保证**
不建唯一索引 —— `ensure_schema()` 只 ADD COLUMN，不建索引，改它风险大于收益。
改成生成时查重：候选号已被占用就 +1 秒重试，上限 1000 次。副作用是同秒开的第 2 张
之后的单，号里的秒数比真实开单时间晚 1~2 秒。考虑到「一人名下同时只能有一张未完工
开工单」，这种碰撞实际极难发生，这点偏差可以接受。

**历史数据**
`backfill_codes()` 由 `init_db()` 在 `ensure_schema()` 之后自动调用，幂等：
只处理 code 为空的行，补过的不再动，多 worker 同时启动也不会互相踩。想手动重跑或
先看一眼要改多少张：

    python -m app.work_order_code --dry-run
    python -m app.work_order_code
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.production import WorkOrder

CODE_FMT = "%Y%m%d%H%M%S"
CODE_LEN = 14
# 同秒碰撞时往后试多少秒。真实并发远低于这个量级，超了就是数据出问题，直接抛
MAX_TRIES = 1000


def format_code(at: datetime) -> str:
    """把时间格式化成 14 位工单号。"""
    return at.strftime(CODE_FMT)


def next_code(db: Session, at: datetime | None = None) -> str:
    """生成一个没被占用的工单号。

    查重用的是**列查询**（`db.query(WorkOrder.id)`）：这类语句不走
    `do_orm_execute` 里的 `with_loader_criteria`，因此能看见测试域和生产域两张网，
    code 在全表唯一 —— 这正是我们要的。若改成查实体，会被数据域过滤掉，
    两个域就可能生成同一个号。

    注意 `SessionLocal` 是 autoflush=False：同一 session 里连建多张单（比如种子脚本）
    时，前一张还没 flush 就查不到。调用方建完一张要 `db.flush()`，见 seed_demo。
    """
    base = (at or datetime.now()).replace(microsecond=0)
    for offset in range(MAX_TRIES):
        candidate = format_code(base + timedelta(seconds=offset))
        taken = db.query(WorkOrder.id).filter(WorkOrder.code == candidate).first()
        if taken is None:
            return candidate
    raise RuntimeError(
        f"工单号生成失败：从 {format_code(base)} 起连续 {MAX_TRIES} 秒都已被占用"
    )


def backfill_codes(db: Session | None = None, dry_run: bool = False) -> dict:
    """给 code 为空的历史开工单，用 created_at 补出 14 位号。

    幂等：只挑 code 为空的行；补过的不再动。
    返回 {"total": 空号总数, "filled": 实际/将要补的数量, "dry_run": bool, "sample": [...]}。
    """
    own_session = db is None
    if own_session:
        # 刻意不设 session.info 的数据域标记：回填要覆盖测试域和生产域两张网
        from app.database import SessionLocal

        db = SessionLocal()

    try:
        empty = (
            db.query(WorkOrder)
            .filter((WorkOrder.code == "") | (WorkOrder.code.is_(None)))
            .order_by(WorkOrder.created_at, WorkOrder.id)
            .all()
        )
        if not empty:
            return {"total": 0, "filled": 0, "dry_run": dry_run, "sample": []}

        # 已占用的号（含同一个库里的另一张网）先收进来，避免补出重复号
        used = {
            code
            for (code,) in db.query(WorkOrder.code).filter(WorkOrder.code != "").all()
            if code
        }

        filled: list[tuple[int, str]] = []
        for work_order in empty:
            base = (work_order.created_at or datetime.now()).replace(microsecond=0)
            for offset in range(MAX_TRIES):
                candidate = format_code(base + timedelta(seconds=offset))
                if candidate not in used:
                    break
            else:
                raise RuntimeError(
                    f"开工单 #{work_order.id} 回填失败：从 {format_code(base)} 起"
                    f"连续 {MAX_TRIES} 秒都已被占用"
                )
            used.add(candidate)
            filled.append((work_order.id, candidate))
            if not dry_run:
                work_order.code = candidate

        if not dry_run:
            db.commit()
        return {
            "total": len(empty),
            "filled": len(filled),
            "dry_run": dry_run,
            "sample": [f"#{wid} → {code}" for wid, code in filled[:10]],
        }
    finally:
        if own_session:
            db.close()


def main() -> int:
    import sys

    dry_run = "--dry-run" in sys.argv
    result = backfill_codes(dry_run=dry_run)
    if not result["total"]:
        print("没有 code 为空的开工单，无需回填")
        return 0
    verb = "将回填" if dry_run else "已回填"
    print(f"{verb} {result['filled']} 张（共 {result['total']} 张空号）")
    for line in result["sample"]:
        print("  ", line)
    if result["total"] > len(result["sample"]):
        print(f"   …… 其余 {result['total'] - len(result['sample'])} 张略")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
