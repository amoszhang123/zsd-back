from datetime import datetime

from sqlalchemy import DateTime, Double, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.database import Base
from app.models.order import Order
from app.models.production import Employee

STAGES = ("quality1", "surface", "quality2")
STAGE_LABELS = {"quality1": "质检1", "surface": "表处", "quality2": "质检2"}
DISPOSITION_STATUSES = ("待处理", "处理中", "已完成")
DISPOSITION_TYPES = ("rework", "scrap", "repair")
DISPOSITION_TYPE_LABELS = {"rework": "返工单", "scrap": "报废单", "repair": "维修单"}
# 金额列的含义随单据类型不同：报废是物料损失，返工/维修是额外支出
DISPOSITION_COST_LABELS = {"scrap": "物料损失", "rework": "额外支出", "repair": "额外支出"}


class DispositionBase(Base):
    """返工/报废/维修三种单据的公共字段。"""

    __abstract__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    employee_id: Mapped[str | None] = mapped_column(
        String(10), ForeignKey("employees.id"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    @declared_attr
    def order(cls) -> Mapped["Order"]:
        return relationship()

    @declared_attr
    def employee(cls) -> Mapped["Employee | None"]:
        return relationship()


class ReworkOrder(DispositionBase):
    """返工单"""

    __tablename__ = "rework_orders"

    status: Mapped[str] = mapped_column(String(10), default="待处理", index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class ScrapOrder(DispositionBase):
    """报废单：一次性判定，无状态流转，故不设 status / updated_at。"""

    __tablename__ = "scrap_orders"


class RepairOrder(DispositionBase):
    """维修单"""

    __tablename__ = "repair_orders"

    status: Mapped[str] = mapped_column(String(10), default="待处理", index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class DispositionOwner(Base):
    """返工/报废/维修单的负责人（多选）与逐人损耗。

    三种单据共用这一张表，用 disposition_type + disposition_id 定位所属单据。
    这是「多态外键」，数据库层面没有真的外键约束（它指向三张不同的表），
    一致性由 app/routers/quality.py 的写入与删除逻辑保证。

    损耗（工时 / 材料费）目前只对报废单收集，由判定人逐人手动填写，
    后续计划改从备料环节自动带出。返工/维修单这两列先留 0，
    列已就位，将来要收不必再改表结构。
    """

    __tablename__ = "disposition_owners"
    __table_args__ = (
        UniqueConstraint(
            "disposition_type", "disposition_id", "employee_id", name="uq_disposition_owner"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disposition_type: Mapped[str] = mapped_column(String(10), index=True)
    disposition_id: Mapped[int] = mapped_column(Integer, index=True)
    employee_id: Mapped[str] = mapped_column(String(10), ForeignKey("employees.id"), index=True)
    labor_hours: Mapped[float] = mapped_column(Double, default=0.0)
    material_cost: Mapped[float] = mapped_column(Double, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # NULL 表示建单后从未改过损耗
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None, onupdate=datetime.now
    )

    employee: Mapped["Employee"] = relationship()


class QcInspection(Base):
    """质检批次：一个订单在同一阶段可以有多条，支持部分完成、部分质检。

    待检数量 = 上游产出数量 − 本阶段已检数量之和，其中上游产出口径为
      quality1 → 该订单累计完工数量（work_order_items.completed_qty 之和）
      surface  → quality1 各批次 pass_count 之和
      quality2 → surface 各批次 pass_count 之和
    """

    __tablename__ = "qc_inspections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    pass_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    scrap_qty: Mapped[int] = mapped_column(Integer, default=0)
    verdict: Mapped[str] = mapped_column(String(20), default="pass", index=True)
    remark: Mapped[str] = mapped_column(String(500), default="")
    employee_id: Mapped[str | None] = mapped_column(
        String(10), ForeignKey("employees.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    order: Mapped["Order"] = relationship()
    employee: Mapped["Employee | None"] = relationship()
