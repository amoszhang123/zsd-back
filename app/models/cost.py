from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Double, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CostRecord(Base):
    """成本支出记录。

    category 是自由文本：前端提供 刀具/油品/材料/劳保/备品/工具 预设，选「其他」时手输，
    所以后端不做枚举约束，只要求非空。金额不落库，一律按 单价 × 数量 实时算，
    与出货单、报价单的做法一致，避免改价后汇总对不上。
    """

    __tablename__ = "cost_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    spec: Mapped[str] = mapped_column(String(200), default="")
    # 数量用 Double：油品/材料常有 2.5 升、1.5 公斤这类小数；
    # 默认的 Float 是单精度，录 1.2 会被存成 1.2000000476837158
    quantity: Mapped[float] = mapped_column(Double, default=0.0)
    unit_price: Mapped[float] = mapped_column(Double, default=0.0)
    supplier: Mapped[str] = mapped_column(String(200), default="", index=True)
    invoiced: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    remark: Mapped[str] = mapped_column(String(500), default="")
    purchase_date: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
