"""清空全部业务数据，只保留员工表与机台表，并把自增 ID 重置为 1。

⚠️ 不可逆：TRUNCATE 是 DDL，会隐式提交，无法回滚。执行前请自行确认已不需要这些数据。

保留：employees（员工）、machines（机台基础数据）
清空：其余所有表

用法： cd factory && PYTHONPATH=. .venv/bin/python reset_data.py --yes
      不带 --yes 只打印将要做什么，不动数据。
"""
import sys

from sqlalchemy import inspect, text

from app.database import engine

KEEP = ("employees", "machines")


def main() -> None:
    dry_run = "--yes" not in sys.argv

    insp = inspect(engine)
    tables = sorted(insp.get_table_names())
    keep = [t for t in tables if t in KEEP]
    wipe = [t for t in tables if t not in KEEP]

    with engine.connect() as c:
        print(f"数据库共 {len(tables)} 张表\n")
        print("保留:")
        for t in keep:
            n = c.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            print(f"  ✅ {t:26} {n:>6} 行")
        print("\n清空:")
        total = 0
        for t in wipe:
            n = c.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            total += n
            print(f"  🗑  {t:26} {n:>6} 行")
        print(f"\n合计将删除 {len(wipe)} 张表、{total} 行数据")

    if dry_run:
        print("\n这是预演，没有改动任何数据。确认无误后加 --yes 执行。")
        return

    print("\n=== 开始执行 ===")
    with engine.begin() as c:
        # TRUNCATE 父表时 MySQL 会拒绝，必须先关掉外键检查
        c.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in wipe:
            c.execute(text(f"TRUNCATE TABLE `{t}`"))
            print(f"  已清空 {t}")
        c.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    print("外键检查已恢复")

    print("\n=== 结果核对 ===")
    insp = inspect(engine)
    with engine.connect() as c:
        bad = []
        for t in sorted(insp.get_table_names()):
            n = c.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            ai = c.execute(text(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": t}).scalar()
            expect_kept = t in KEEP
            ok = n > 0 if expect_kept else n == 0
            if not ok:
                bad.append(t)
            mark = "✅ 保留" if expect_kept else "✅ 已清空"
            print(f"  {mark} {t:26} {n:>6} 行  AUTO_INCREMENT={ai if ai else '—'}")
        print()
        if bad:
            print(f"❌ 结果不符合预期: {bad}")
            sys.exit(1)
        print("🎉 清空完成：仅剩 employees 与 machines，其余表为空且自增 ID 已重置为 1")


if __name__ == "__main__":
    main()
