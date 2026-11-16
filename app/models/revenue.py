"""订单收入：把「已出货」确认为收入。

为什么不是「自动算出来就完事」：出货只是动作，钱要到账才算收入。厂里的实际节奏是
「一个客户一个月对一次账，对完确认一笔」，所以这里存的是**人工确认过的**收入，
出货总额只是用来提示「还有多少没确认」。

金额口径与送货单、对账单完全一致：合同明细的**含税单价 × 出货用量**。
不做税率拆分 —— 源头只有含税单价，拆税要给每个客户维护税率，配错了比不拆更糟。

归月依据是送货单的 ship_date（出货日期），不是 created_at：补录的历史送货单
要归到它实际发生的那个月，否则对账单期间与收入月份会错位。
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Double, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantScoped


class RevenueRecord(TenantScoped, Base):
    """一笔已确认的订单收入（客户 + 月份）。

    同一客户同一月份允许多条，这是刻意留的：先确认了 10 月的 10 万，两天后补录了
    一张 10 月的送货单，待确认里会再冒出一笔差额，确认它就多一条记录。
    历史那笔不动，这样「为什么金额变了」永远查得清。
    """

    __tablename__ = "revenue_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 客户名取自送货单的 party_a 快照。此处不做规范化（全半角/空格），
    # 与对账单的客户分组口径保持一致 —— 两处若用不同规则，同一个客户
    # 会让「对账单一张、收入两张」，反而更难解释。
    customer: Mapped[str] = mapped_column(String(200), default="", index=True)
    # "2026-10"。存字符串而不是拆成年/月两列：分组、排序、按期间查都是等值或前缀，
    # 字符串足够，且前端拿到就能直接显示。
    period: Mapped[str] = mapped_column(String(7), index=True)

    # 确认的收入金额（含税）。默认等于当期出货总额，允许手工改小 ——
    # 客户扣款、让价、退货扣减都在这一步体现。
    amount: Mapped[float] = mapped_column(Double, default=0.0)
    # 确认那一刻的出货总额快照。差额行出现时，靠它才能解释「这 2 万是补的哪部分」。
    shipped_amount: Mapped[float] = mapped_column(Double, default=0.0)

    invoiced: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 已收金额 + 最近一次收款日期。不支持多笔收款流水：逐笔到账核对靠公账页的
    # 银行流水做，这里只回答「还欠多少」。
    received_amount: Mapped[float] = mapped_column(Double, default=0.0)
    received_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    remark: Mapped[str] = mapped_column(String(500), default="")
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
