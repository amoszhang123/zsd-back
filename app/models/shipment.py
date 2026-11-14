from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.database import Base
from app.models.contract import Contract
from app.models.order import SHIPPING_STEP, Order


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
    # 送货单抬头印的那个日期（月/日）。默认 = 开单当天，允许人工改 —— 补打历史单时
    # 抬头不该变成「打印当天」，那样和单号里写死的开单日期对不上。
    # 与 doc_no 的关系：doc_no 的 yyyymmdd 是开单基准（还带着当日流水号），
    # 改 ship_date 不会也不能改单号，两者从此允许不一致。
    # 可空：给 ensure_schema 留出「补列不锁表」的空间，存量行由 backfill_ship_date 补。
    ship_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 这张送货单是在「历史补偿模式」下、绕过终检开出来的。只做内部追溯，
    # 不打印在送货单上 —— 客户不需要知道我们内部跳过了哪道工序。
    qc_override: Mapped[bool] = mapped_column(Boolean, default=False)
    # 自动生成放行说明，如「WO22105AA 未终检、WO22106BA 报废」
    qc_override_note: Mapped[str] = mapped_column(String(500), default="")

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


def shipped_qty_map(db: Session, order_ids: list[str] | None = None) -> dict[str, int]:
    """工单 → 累计已出货数量（多张送货单的用量相加）。

    「出满没有」和「还能再出多少」都靠它；只传 order_ids 时范围限定在这批工单，
    不传就是全表汇总。
    """
    query = db.query(ShipmentItem.order_id, func.sum(ShipmentItem.quantity)).filter(
        ShipmentItem.order_id.isnot(None)
    )
    if order_ids is not None:
        if not order_ids:
            return {}
        query = query.filter(ShipmentItem.order_id.in_(order_ids))
    rows = query.group_by(ShipmentItem.order_id).all()
    return {order_id: int(qty or 0) for order_id, qty in rows}


def fulfilled_order_ids(db: Session) -> set[str]:
    """已经全量出货（完结）的工单号。

    到货判定见 routers/shipments.py：累计出货数量 ≥ 工单数量才写 fulfilled_at。
    仪表盘的交期预警、小程序的「待出货」卡都只认这个口径 —— 用「出过货」会把
    只发了一部分的单从待办里抹掉。
    """
    rows = db.query(Order.id).filter(Order.fulfilled_at.isnot(None)).all()
    return {row[0] for row in rows}


def overridden_order_ids(db: Session, order_ids: list[str] | None = None) -> set[str]:
    """在「历史补偿模式」下绕过终检出过货的工单号（送货单上留了放行说明的那批）。

    订单详情页据此标「历史补录」；列表页一次查完，别逐条查。
    """
    query = (
        db.query(ShipmentItem.order_id)
        .join(Shipment, Shipment.id == ShipmentItem.shipment_id)
        .filter(Shipment.qc_override.is_(True), ShipmentItem.order_id.isnot(None))
    )
    if order_ids is not None:
        if not order_ids:
            return set()
        query = query.filter(ShipmentItem.order_id.in_(order_ids))
    return {row[0] for row in query.distinct().all()}


def backfill_fulfilled() -> int:
    """把「出货记录早于 fulfilled_at 列」的历史单补上完结时间。

    幂等：只处理 fulfilled_at 为空、且累计出货量已经够数的工单；补过的不会再动。
    不这么做的话，改动之前开过送货单的单会一直显示「待出货（已出 16/16）」，
    既不在待出货卡里，也不算已完结。
    """
    from sqlalchemy import text

    from app.database import engine

    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE orders o
                JOIN (
                    SELECT order_id, SUM(quantity) AS q
                    FROM shipment_items
                    WHERE order_id IS NOT NULL
                    GROUP BY order_id
                ) s ON s.order_id = o.id
                SET o.fulfilled_at = NOW(),
                    o.current_step = GREATEST(o.current_step, :shipping)
                WHERE o.fulfilled_at IS NULL AND o.quantity > 0 AND s.q >= o.quantity
                """
            ),
            {"shipping": SHIPPING_STEP},
        )
        # 顺带把流水线里的「出货」步骤标成已完成，否则详情页会出现
        # 「状态已完结、流程条还停在等待」的自相矛盾
        conn.execute(
            text(
                """
                UPDATE order_steps os
                JOIN orders o ON o.id = os.order_id
                SET os.status = 'completed', os.time = DATE(o.fulfilled_at)
                WHERE os.step_key = 'shipping'
                  AND o.fulfilled_at IS NOT NULL
                  AND os.status <> 'completed'
                """
            )
        )
    return result.rowcount or 0


def backfill_ship_date() -> int:
    """给 ship_date 列出现之前开出的送货单补上出货日期。

    补的值是开单当天，也就是这些单当初打印时抬头印的那个日期 —— 那时候抬头取的是
    「打印当天」，只要没补打就正好等于开单日。

    幂等：只填 ship_date 为空的行；补过的不再动，多 worker 同时启动也不会互相踩。
    """
    from sqlalchemy import text

    from app.database import engine

    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE shipments
                SET ship_date = DATE(created_at)
                WHERE ship_date IS NULL
                """
            )
        )
    return result.rowcount or 0
