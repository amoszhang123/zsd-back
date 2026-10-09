"""质检改为批次存储：quality_results（每订单每阶段一行）→ qc_inspections（每次质检一条）。

新表由 Base.metadata.create_all 自动创建，本脚本负责把已有的质检结果搬成批次记录。
旧表 quality_results 保留不删（确认无误后可自行 DROP），但代码不再读写它。

字段映射：
  quantity   = pass_count + fail_count（旧表没有"送检数量"，用合格+不良还原）
  scrap_qty  = 判定为整单报废时取 quantity，否则 0（旧表没有单独的报废数量）
  verdict    = qc_status
  employee_id / created_at 旧表没有，分别为 NULL / 迁移时刻

用法： cd factory && PYTHONPATH=. .venv/bin/python migrations/20261001_qc_inspections.py
可重复执行：qc_inspections 非空时跳过。
"""
from datetime import datetime

from sqlalchemy import inspect, text

from app.database import Base, SessionLocal, engine
from app.models.quality import QcInspection


def main() -> None:
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    print("qc_inspections 建表:", "已就绪" if "qc_inspections" in insp.get_table_names() else "失败")

    if "quality_results" not in insp.get_table_names():
        print("库中没有 quality_results 表，无需迁移")
        return

    db = SessionLocal()
    try:
        if db.query(QcInspection).count():
            print(f"qc_inspections 已有 {db.query(QcInspection).count()} 条，跳过迁移以免重复")
            return

        rows = db.execute(
            text(
                "SELECT id, order_id, stage, pass_count, fail_count, rate, qc_status, remark "
                "FROM quality_results ORDER BY id"
            )
        ).all()
        print(f"待迁移 quality_results: {len(rows)} 条")

        now = datetime.now()
        for row in rows:
            quantity = (row.pass_count or 0) + (row.fail_count or 0)
            verdict = row.qc_status or "pass"
            batch = QcInspection(
                order_id=row.order_id,
                stage=row.stage or "quality1",
                quantity=quantity,
                pass_count=row.pass_count or 0,
                fail_count=row.fail_count or 0,
                scrap_qty=quantity if verdict == "scrap" else 0,
                verdict=verdict,
                remark=row.remark or "",
                employee_id=None,
                created_at=now,
            )
            db.add(batch)
            print(
                f"  旧 #{row.id} {row.order_id} [{row.stage}] → 送检 {quantity} "
                f"合格 {row.pass_count} 不良 {row.fail_count} 判定 {verdict}"
            )

        db.commit()
        print(f"迁移完成：qc_inspections {db.query(QcInspection).count()} 条")
        print("旧表 quality_results 保留未删，确认无误后可执行 DROP TABLE quality_results;")
    finally:
        db.close()


if __name__ == "__main__":
    main()
