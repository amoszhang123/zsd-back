from datetime import date, datetime

from sqlalchemy import Date, DateTime, Double, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.database import Base
from app.models.order import Order
from app.models.production import WorkOrderItem

CONTRACT_STATUS_NORMAL = "正常"
CONTRACT_STATUS_PAUSED = "暂停"
CONTRACT_STATUS_CANCELLED = "取消"
CONTRACT_STATUS_CLOSED = "结案"
CONTRACT_STATUSES = (
    CONTRACT_STATUS_NORMAL,
    CONTRACT_STATUS_PAUSED,
    CONTRACT_STATUS_CANCELLED,
    CONTRACT_STATUS_CLOSED,
)

# 取消与结案是终态；暂停可以恢复成正常
ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    CONTRACT_STATUS_NORMAL: frozenset(
        {CONTRACT_STATUS_PAUSED, CONTRACT_STATUS_CANCELLED, CONTRACT_STATUS_CLOSED}
    ),
    CONTRACT_STATUS_PAUSED: frozenset(
        {CONTRACT_STATUS_NORMAL, CONTRACT_STATUS_CANCELLED, CONTRACT_STATUS_CLOSED}
    ),
    CONTRACT_STATUS_CANCELLED: frozenset(),
    CONTRACT_STATUS_CLOSED: frozenset(),
}

# 这些状态下，合同的工单不可接单、不可开工（在接口层拦，不只前端置灰）
BLOCKING_STATUSES = (CONTRACT_STATUS_PAUSED, CONTRACT_STATUS_CANCELLED, CONTRACT_STATUS_CLOSED)


class Contract(Base):
    """购销合同：由 Excel 导入解析生成，主键为 HT + 采购单号。"""

    __tablename__ = "contracts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    purchase_no: Mapped[str] = mapped_column(String(50), index=True, default="")
    status: Mapped[str] = mapped_column(
        String(20), default=CONTRACT_STATUS_NORMAL, index=True
    )
    party_a: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    bank_name: Mapped[str] = mapped_column(String(200), default="")
    bank_account: Mapped[str] = mapped_column(String(60), default="")
    tax_no: Mapped[str] = mapped_column(String(60), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    signed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Double 即 MySQL DOUBLE（8 字节双精度）。SQLAlchemy 默认的 Float 建出来是单精度，
    # 只有约 7 位有效数字，196585.67 这类金额会失真。
    # 见 migrations/20261004_money_columns_double.sql
    total_amount: Mapped[float] = mapped_column(Double, default=0.0)
    source_file: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    items: Mapped[list["ContractItem"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )


class ContractItem(Base):
    """合同工件明细。delivery 存 Excel 原值（可能是 1、2 这类整数，也可能为空）。"""

    __tablename__ = "contract_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[str] = mapped_column(String(40), ForeignKey("contracts.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    project_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    brand: Mapped[str] = mapped_column(String(100), default="")
    product: Mapped[str] = mapped_column(String(200), default="")
    material_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    model: Mapped[str] = mapped_column(String(100), default="")
    unit: Mapped[str] = mapped_column(String(20), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit_price: Mapped[float] = mapped_column(Double, default=0.0)
    amount: Mapped[float] = mapped_column(Double, default=0.0)
    delivery: Mapped[str] = mapped_column(String(20), default="")
    order_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("orders.id"), nullable=True, index=True
    )
    # NULL 表示从未修改过；只有数量支持单项修改
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None, onupdate=datetime.now
    )

    contract: Mapped["Contract"] = relationship(back_populates="items")
    # 声明关系而不只是外键列，SQLAlchemy 才知道要先插 orders 再插 contract_items
    order: Mapped["Order | None"] = relationship()


def in_production_map(db: Session, contract_ids: list[str]) -> set[str]:
    """返回其中「已投产」的合同 id 集合。

    已投产 = 合同下任一关联工单进过开工单（work_order_items 里有记录）。
    与「合同明细数量锁定」同口径：真正动过生产的才算，只流转到投产阶段但
    从没开过工的不算。
    """
    if not contract_ids:
        return set()
    rows = (
        db.query(ContractItem.contract_id)
        .join(WorkOrderItem, WorkOrderItem.order_id == ContractItem.order_id)
        .filter(ContractItem.contract_id.in_(contract_ids))
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def contract_in_production(db: Session, contract: "Contract") -> bool:
    return contract.id in in_production_map(db, [contract.id])


def blocked_orders(db: Session, order_ids: list[str]) -> dict[str, str]:
    """order_id → 所属合同的非正常状态。

    返回的就是「不可接单 / 不可开工」的工单；合同状态为正常的不在结果里。
    放在 models 层是因为 contracts.py 已经 import 了 orders.py，
    orders.py 不能反向 import，否则成环。
    """
    if not order_ids:
        return {}
    rows = (
        db.query(ContractItem.order_id, Contract.status)
        .join(Contract, Contract.id == ContractItem.contract_id)
        .filter(
            ContractItem.order_id.in_(order_ids),
            Contract.status != CONTRACT_STATUS_NORMAL,
        )
        .all()
    )
    return {order_id: status for order_id, status in rows}
