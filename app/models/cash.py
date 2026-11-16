"""费用收支：非订单的经营性收付款。

与 cost_records 的分工是刻意划清的 ——
  * cost_records（成本支出）：**采购物料**的支出，会纳入物料库存、要管发票；
  * cash_entries（费用收支）：**不涉及库存**的收支，水电房租工资运费这类，
    以及非订单的收入（卖废料、设备出租、补贴）。

两边都记支出，但一边是「买了东西」，一边是「花了钱」，混在一张表里会让
「纳入库存」「只看缺票」这些成本页特有的筛选对大多数行都无意义。

金额直接填，不用数量×单价：费用类支出天然是一笔一个数，逼人填 1×5000 只是
让人乱填。需要按量算的场景（运费按吨）在备注里写清楚就够了。
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Double, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantScoped

DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)


class CashEntry(TenantScoped, Base):
    """一笔费用收支。direction 决定它在统计里算收入还是支出。

    不加唯一约束、不做「对冲」关联：一笔收错了就删掉重录，比维护冲销关系简单。
    账目可追溯性靠 remark 和凭证图，不靠流水号。
    """

    __tablename__ = "cash_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # "in" = 收入，"out" = 支出。存短码不存中文：以后要做英文界面或统计口径
    # 调整时，中文值改起来要连带改历史数据。
    direction: Mapped[str] = mapped_column(String(4), index=True, default=DIRECTION_OUT)
    category: Mapped[str] = mapped_column(String(50), index=True)
    # Double 而非默认 Float：Float 建出来是单精度，5000.01 会变成 5000.009765625，
    # 与 cost_records / contracts 的金额列保持同一精度口径。
    amount: Mapped[float] = mapped_column(Double, default=0.0)
    # 对方：支出记收款方（房东/供水公司/员工），收入记付款方。自由文本，
    # 不做往来单位主数据 —— 这个规模下维护主数据比手输更费事。
    counterparty: Mapped[str] = mapped_column(String(200), default="", index=True)
    remark: Mapped[str] = mapped_column(String(500), default="")
    entry_date: Mapped[date] = mapped_column(Date, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class CashVoucher(TenantScoped, Base):
    """一笔费用收支下的凭证图片（转账截图 / 收据 / 合同照）。

    结构与 CostInvoice 一致、单独建表而不是复用：两张表的父表不同，
    合表就得加父类型判别列，之后每次查都要带上，得不偿失。
    共用的是 invoice_storage 里的校验与落盘代码，不是表。
    """

    __tablename__ = "cash_vouchers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(Integer, index=True)

    file_path: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(50), default="")

    source: Mapped[str] = mapped_column(String(10), default="web")
    uploaded_by: Mapped[str] = mapped_column(String(50), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
