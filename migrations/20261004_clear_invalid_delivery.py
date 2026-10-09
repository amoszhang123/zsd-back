"""清理 contract_items.delivery 里无法识别为日期的脏数据。

背景：
  合同 HT2026090323 的源 Excel 里「交期」列填的是 1/2/3/4（疑似分批批次号），
  导入时按原样存进了 delivery。这些值不是日期，_parse_delivery_date 解析不出，
  因此对应工单的 deadline 一直是 NULL，不参与生产管理的「3日预警 / 超期工单」统计。

处理：
  把「非空 且 无法解析为日期」的 delivery 清空为 ''。有效日期和本来就为空的一律不动。
  判定直接复用 app.routers.contracts._parse_delivery_date，不另写正则，
  避免清理口径与同步口径漂移。

用法： cd factory && PYTHONPATH=. .venv/bin/python migrations/20261004_clear_invalid_delivery.py
可重复执行：清理后再跑，命中 0 行。
"""
from collections import Counter

from sqlalchemy import bindparam, text

from app.database import SessionLocal
from app.models.contract import ContractItem
from app.routers.contracts import _parse_delivery_date


def main() -> None:
    db = SessionLocal()
    try:
        items = db.query(ContractItem).all()
        print(f"合同明细共 {len(items)} 条")

        before = Counter(i.delivery for i in items)
        print("\n清理前 delivery 取值分布:")
        for value, n in sorted(before.items(), key=lambda x: -x[1]):
            parsed = _parse_delivery_date(value)
            mark = "（空）" if not value.strip() else (f"→ {parsed}" if parsed else "→ 无法识别，将清空")
            print(f"  {value!r:12} {n:4} 条  {mark}")

        targets = [
            i for i in items
            if i.delivery.strip() and _parse_delivery_date(i.delivery) is None
        ]
        kept_dates = [i for i in items if _parse_delivery_date(i.delivery) is not None]
        print(f"\n将清空 {len(targets)} 条；保留有效日期 {len(kept_dates)} 条；"
              f"本来就为空 {len(items) - len(targets) - len(kept_dates)} 条")

        if not targets:
            print("没有需要清理的数据")
            return

        cleared = Counter(i.delivery for i in targets)
        ids = [i.id for i in targets]
        # 必须用裸 SQL 更新：ContractItem.updated_at 上挂了 onupdate=datetime.now，
        # 走 ORM 赋值会给这些行盖上「已修改」时间戳，而 updated_at 的语义是
        # 「数量被人工改过」（前端据此显示 ✎ 标记），清理脏数据不该算修改。
        # text() 不触发 SQLAlchemy 的 onupdate，列本身也没有 MySQL 的
        # ON UPDATE CURRENT_TIMESTAMP，所以 updated_at 会原样保留。
        db.execute(
            text("UPDATE contract_items SET delivery = '' WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": ids},
        )
        db.commit()

        print("\n已清空的取值:")
        for value, n in sorted(cleared.items(), key=lambda x: -x[1]):
            print(f"  {value!r:12} {n:4} 条 → ''")

        db.expire_all()
        after = Counter(i.delivery for i in db.query(ContractItem).all())
        print("\n清理后 delivery 取值分布:")
        for value, n in sorted(after.items(), key=lambda x: -x[1]):
            print(f"  {value!r:12} {n:4} 条")
        remaining = sum(
            n for v, n in after.items() if v.strip() and _parse_delivery_date(v) is None
        )
        print(f"\n残留无法识别的交期: {remaining} 条 "
              f"{'✅' if remaining == 0 else '❌'}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
