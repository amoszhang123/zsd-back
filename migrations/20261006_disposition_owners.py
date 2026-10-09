"""返工/报废/维修单负责人改多选 + 报废单增加逐人损耗（工时 / 材料费）。

结构变化：
  新增 disposition_owners
    id                INT PK
    disposition_type  VARCHAR(10)  'rework' / 'scrap' / 'repair'
    disposition_id    INT          对应单据主键
    employee_id       VARCHAR(10)  FK → employees.id
    labor_hours       DOUBLE       损耗工时（目前只对报废单收集）
    material_cost     DOUBLE       损耗材料费（同上，后续计划从备料环节自动带出）
    created_at        DATETIME
    UNIQUE(disposition_type, disposition_id, employee_id)

  三种单据共用一张表，用 disposition_type + disposition_id 定位（多态外键，
  数据库层面没有真外键指向那三张表，一致性由 app/routers/quality.py 保证）。

数据回填：
  把历史单据的单选 employee_id 回填成一条负责人记录，损耗记 0。

用法： cd factory && PYTHONPATH=. .venv/bin/python migrations/20261006_disposition_owners.py
可重复执行：建表用 IF NOT EXISTS，索引先查 information_schema，回填用 INSERT IGNORE + 唯一约束。
"""
from sqlalchemy import inspect, text

from app.database import engine

TABLE = "disposition_owners"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS disposition_owners (
    id INTEGER NOT NULL AUTO_INCREMENT,
    disposition_type VARCHAR(10) NOT NULL,
    disposition_id INTEGER NOT NULL,
    employee_id VARCHAR(10) NOT NULL,
    labor_hours DOUBLE NOT NULL,
    material_cost DOUBLE NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_disposition_owner UNIQUE (disposition_type, disposition_id, employee_id),
    FOREIGN KEY(employee_id) REFERENCES employees (id)
)
"""

INDEXES = {
    f"ix_{TABLE}_disposition_type": f"CREATE INDEX ix_{TABLE}_disposition_type ON {TABLE} (disposition_type)",
    f"ix_{TABLE}_disposition_id": f"CREATE INDEX ix_{TABLE}_disposition_id ON {TABLE} (disposition_id)",
    f"ix_{TABLE}_employee_id": f"CREATE INDEX ix_{TABLE}_employee_id ON {TABLE} (employee_id)",
}

# 建表 DDL 照抄 SQLAlchemy 对 app/models/quality.py::DispositionOwner 生成的结果，
# 保证「跑过迁移的库」和「靠 create_all 新建的库」结构一致。
BACKFILL = [
    ("scrap", "scrap_orders"),
    ("rework", "rework_orders"),
    ("repair", "repair_orders"),
]


def main() -> None:
    insp = inspect(engine)
    existed = insp.has_table(TABLE)
    print(f"{TABLE} 建表前是否已存在: {existed}")

    with engine.begin() as c:
        c.execute(text(CREATE_TABLE))
        print("建表语句已执行（IF NOT EXISTS）")

        existing_idx = {r[0] for r in c.execute(text(
            "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": TABLE}).fetchall()}
        for name, ddl in INDEXES.items():
            if name in existing_idx:
                print(f"  索引 {name} 已存在，跳过")
            else:
                c.execute(text(ddl))
                print(f"  索引 {name} 已创建")

        print("\n回填历史单据的负责人:")
        for dtype, table in BACKFILL:
            n = c.execute(text(
                f"INSERT IGNORE INTO {TABLE} "
                "(disposition_type, disposition_id, employee_id, labor_hours, material_cost, created_at) "
                f"SELECT :dt, id, employee_id, 0, 0, created_at FROM {table} "
                "WHERE employee_id IS NOT NULL"), {"dt": dtype}).rowcount
            total = c.execute(text(
                f"SELECT COUNT(*) FROM {table}")).scalar()
            with_emp = c.execute(text(
                f"SELECT COUNT(*) FROM {table} WHERE employee_id IS NOT NULL")).scalar()
            print(f"  {table:16} 共 {total} 行，其中有负责人 {with_emp} 行，本次新插入 {n} 行")

    print("\n迁移后核对:")
    insp = inspect(engine)
    for col in insp.get_columns(TABLE):
        print(f"  {col['name']:18} {str(col['type']):14} nullable={col['nullable']}")
    print("  唯一约束:",
          [(u['name'], u['column_names']) for u in insp.get_unique_constraints(TABLE)])
    print("  外键:", [(f['constrained_columns'], f['referred_table'])
                    for f in insp.get_foreign_keys(TABLE)])
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT disposition_type, disposition_id, employee_id, labor_hours, material_cost "
            f"FROM {TABLE} ORDER BY disposition_type, disposition_id")).fetchall()
        print(f"  负责人记录 {len(rows)} 条:")
        for r in rows:
            print(f"    {r[0]:7} #{r[1]:<3} 员工 {r[2]}  工时 {r[3]}  材料费 {r[4]}")


if __name__ == "__main__":
    main()
