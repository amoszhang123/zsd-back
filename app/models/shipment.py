from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.contract import Contract
from app.models.order import Order


class Shipment(Base):
    """出货记录单（送货单）。一张单对应一个合同下的一个项目号。

    doc_no 规则取自客户提供的送货单模板：ZSD + yyyymmdd + 当日两位流水，
    如 ZSD2026092701。甲方/采购单号/发货方均为下单时的快照，合同后续变更不影响已出货单。
    """

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_no: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    contract_id: Mapped[str] = mapped_column(String(40), ForeignKey("contracts.id"), index=True)
    project_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    party_a: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    purchase_no: Mapped[str] = mapped_column(String(50), default="")
    ship_from: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    contract: Mapped["Contract"] = relationship()
    items: Mapped[list["ShipmentItem"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )


class ShipmentItem(Base):
    """出货明细，字段对应送货单的 序号/物料号/品名/规格型号/用量/单位/备注。"""

    __tablename__ = "shipment_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_id: Mapped[int] = mapped_column(Integer, ForeignKey("shipments.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("orders.id"), nullable=True, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    material_no: Mapped[str] = mapped_column(String(50), default="")
    product: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(20), default="")
    remark: Mapped[str] = mapped_column(String(200), default="")

    shipment: Mapped["Shipment"] = relationship(back_populates="items")
    order: Mapped["Order | None"] = relationship()
