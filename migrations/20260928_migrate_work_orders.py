"""开工流程重构数据迁移：start_work_records → work_orders (+ employees / items)。

结构变化：
  旧 start_work_records  一条 = 一个员工 + 一个订单
  新 work_orders         一张开工单 = 一个机台 + 多名员工 + 多个订单，一人一天限一张
    work_order_employees UNIQUE(employee_id, work_date) 在 DB 层硬保证「一人一天一张」
    work_order_items     completed_qty 为完工时逐个订单填写的数量

新表由应用启动时的 Base.metadata.create_all 自动创建，本脚本只搬历史数据。
每条旧记录转成一张「单员工 + 单订单」的开工单，work_date 取 DATE(created_at)。

用法： cd factory && PYTHONPATH=. .venv/bin/python migrations/20260928_migrate_work_orders.py
可重复执行：work_orders 非空时跳过，不会重复写入。
"""
from sqlalchemy import inspect, text

from app.database import Base, SessionLocal, engine
from app.models.production import WorkOrder, WorkOrderEmployee, WorkOrderItem

NEW_TABLES = ("work_orders", "work_order_employees", "work_order_items")


def main() -> None:
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    print("新表就绪:", [t for t in NEW_TABLES if t in insp.get_table_names()])

    if "start_work_records" not in insp.get_table_names():
        print("库中没有 start_work_records 表，无需迁移")
        return

    db = SessionLocal()
    try:
        existing = db.query(WorkOrder).count()
        if existing:
            print(f"work_orders 已有 {existing} 条，跳过迁移以免重复写入")
            return

        rows = db.execute(
            text(
                "SELECT id, employee_id, machine_id, order_id, status, "
                "completed_qty, created_at, completed_at "
                "FROM start_work_records ORDER BY id"
            )
        ).all()
        print(f"待迁移 start_work_records: {len(rows)} 条")

        for row in rows:
            work_date = row.created_at.date()
            work_order = WorkOrder(
                work_date=work_date,
                machine_id=row.machine_id,
                status=row.status,
                created_at=row.created_at,
                completed_at=row.completed_at,
            )
            work_order.employees = [
                WorkOrderEmployee(employee_id=row.employee_id, work_date=work_date)
            ]
            work_order.items = [
                WorkOrderItem(order_id=row.order_id, completed_qty=row.completed_qty)
            ]
            db.add(work_order)
            print(
                f"  旧 #{row.id} → 员工 {row.employee_id} / 订单 {row.order_id} / "
                f"{row.status} / qty={row.completed_qty} / {work_date}"
            )

        db.commit()
        print(
            f"迁移完成: work_orders={db.query(WorkOrder).count()} "
            f"work_order_employees={db.query(WorkOrderEmployee).count()} "
            f"work_order_items={db.query(WorkOrderItem).count()}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
