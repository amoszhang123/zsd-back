"""物料（仓库耗材）台账：主数据、流水、领用单、报废单。

和 order_materials 的区别：那边是订单 BOM 的「备料行」（这张单要切多少钢料），
这里是仓库货架上的实物账（几把刀、几桶油）。两套体系互不引用。

记账口径（全模块唯一一条恒等式）：

    入库累计 = 存量 + 报废 + 领用中

三个数量列都冗余在 materials 上，每次变动都在同一事务里同时改列 + 插一笔流水，
列表排序、阈值告警、恒等式自检直接读列，不用每次全表 sum()。

数量统一 Numeric(12,2)：成本记录的 quantity 是 Double，油品常有 2.5 升，
只收整数会让这条成本根本纳不进账。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 流量类型。数值符号一律取正，「增减方向」由类型决定，见 routers/materials.py 的 _apply()。
MOVE_IN = "in"            # 入库（唯一来源：成本记录纳入）
MOVE_REVOKE = "revoke"    # 撤销入库（入库的冲销，不是出库）
MOVE_ISSUE = "issue"      # 领用
MOVE_RETURN = "return"    # 归还
MOVE_SCRAP = "scrap"      # 报废

MOVE_TYPE_LABELS = {
    MOVE_IN: "入库",
    MOVE_REVOKE: "撤销入库",
    MOVE_ISSUE: "领用",
    MOVE_RETURN: "归还",
    MOVE_SCRAP: "报废",
}

# 领用单状态：行级可以部分归还，整单只有两个状态，全部行结清才转已归还。
ISSUE_OPEN = "issued"
ISSUE_CLOSED = "returned"

ISSUE_STATUS_LABELS = {
    ISSUE_OPEN: "领用中",
    ISSUE_CLOSED: "已归还",
}

# 报废来源：从领用单里报废（领用中−、报废+），或仓管直接对库存报废（存量−、报废+）
SCRAP_FROM_ISSUE = "issue"
SCRAP_FROM_STOCK = "stock"

SCRAP_SOURCE_LABELS = {
    SCRAP_FROM_ISSUE: "领用报废",
    SCRAP_FROM_STOCK: "库存报废",
}

QTY = Numeric(12, 2)


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 系统生成的流水码 WL000001，只读；条码内容就是这一串
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    spec: Mapped[str] = mapped_column(String(200), default="")
    unit: Mapped[str] = mapped_column(String(20), default="个")
    category: Mapped[str] = mapped_column(String(50), default="", index=True)
    # 库位（货架号）。告警时能直接告诉人去哪补货，也打在标签上方便归位
    location: Mapped[str] = mapped_column(String(50), default="")
    # 库存 < 阈值 即告警；0 表示不告警（关闭）
    warn_threshold: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    stock_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    issued_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    scrapped_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))

    remark: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    @property
    def total_in(self) -> Decimal:
        """入库累计。三个数都在这张表上，不需要再查流水。"""
        return (self.stock_qty or 0) + (self.issued_qty or 0) + (self.scrapped_qty or 0)


class MaterialMovement(Base):
    """物料流水。只追加，不修改也不删除 —— 撤销入库也是追加一笔冲销。

    before/after 三对快照一起存：只记存量的话，领用/报废改的是 issued/scrapped，
    事后对账看不出当时另外两列是多少，恒等式破了也无从查起。
    """

    __tablename__ = "material_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), index=True
    )
    mtype: Mapped[str] = mapped_column(String(20), index=True)
    qty: Mapped[Decimal] = mapped_column(QTY)

    stock_before: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    stock_after: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    issued_before: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    issued_after: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    scrapped_before: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    scrapped_after: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))

    # 来源单据：cost_record（入库）/ issue_order（领用、归还）/ scrap_order（报废）
    ref_type: Mapped[str] = mapped_column(String(20), default="", index=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ref_no: Mapped[str] = mapped_column(String(40), default="")

    employee_id: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    employee_name: Mapped[str] = mapped_column(String(50), default="")
    # 建档时的物料名快照，避免改名后老流水看着像别人的账
    material_name: Mapped[str] = mapped_column(String(100), default="")

    remark: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class IssueOrder(Base):
    """领用单。一张单可以多行物料，状态只有「领用中 / 已归还」。"""

    __tablename__ = "material_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    employee_id: Mapped[str] = mapped_column(String(10), index=True)
    employee_name: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[str] = mapped_column(String(20), default=ISSUE_OPEN, index=True)
    items_summary: Mapped[str] = mapped_column(String(300), default="")
    remark: Mapped[str] = mapped_column(String(500), default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IssueItem(Base):
    """领用行。行级三列：已领 / 已还 / 已废，三者满足 qty = returned + scrapped + 未结清。"""

    __tablename__ = "material_issue_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_order_id: Mapped[int] = mapped_column(
        ForeignKey("material_issues.id", ondelete="CASCADE"), index=True
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), index=True
    )
    material_code: Mapped[str] = mapped_column(String(20), default="")
    material_name: Mapped[str] = mapped_column(String(100), default="")
    unit: Mapped[str] = mapped_column(String(20), default="个")
    qty: Mapped[Decimal] = mapped_column(QTY)
    returned_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    scrapped_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))


class MaterialScrapOrder(Base):
    """报废单。来源二选一：领用单里报废，或仓管直接对库存报废。"""

    __tablename__ = "material_scraps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default=SCRAP_FROM_STOCK, index=True)
    issue_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    issue_order_no: Mapped[str] = mapped_column(String(30), default="")
    employee_id: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    employee_name: Mapped[str] = mapped_column(String(50), default="")
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class MaterialScrapItem(Base):
    __tablename__ = "material_scrap_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scrap_order_id: Mapped[int] = mapped_column(
        ForeignKey("material_scraps.id", ondelete="CASCADE"), index=True
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), index=True
    )
    material_code: Mapped[str] = mapped_column(String(20), default="")
    material_name: Mapped[str] = mapped_column(String(100), default="")
    unit: Mapped[str] = mapped_column(String(20), default="个")
    qty: Mapped[Decimal] = mapped_column(QTY)
