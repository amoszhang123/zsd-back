from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.order import (
    ORDER_STATUS_LABELS,
    PRODUCTION_STEP,
    SHIPPING_STEP,
    Order,
    OrderMaterial,
)
from app.models.quality import (
    DISPOSITION_TYPE_LABELS,
    STAGE_LABELS,
    QcInspection,
    RepairOrder,
    ReworkOrder,
)
from app.models.shipment import Shipment, ShipmentItem
from app.schemas.order import DashboardAlert, DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# 与生产管理的「3日预警」保持同一口径
WARN_DAYS = 3
DONE_STATUS = "已完成"


def _shipped_order_ids(db: Session) -> set[str]:
    """已经出现在任何出货单里的工单。

    订单出货后 current_step 仍停在 7（待出货），只看 step 会把已出货的也算进预警。
    """
    rows = (
        db.query(ShipmentItem.order_id)
        .filter(ShipmentItem.order_id.isnot(None))
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def _deadline_alerts(db: Session) -> tuple[list[DashboardAlert], list[DashboardAlert]]:
    """返回 (超期, 临期) 两组。只统计未出货、且填了交期的工单。"""
    today = date.today()
    shipped = _shipped_order_ids(db)
    orders = (
        db.query(Order)
        .filter(
            Order.current_step >= PRODUCTION_STEP,
            Order.current_step <= SHIPPING_STEP,
            Order.deadline.isnot(None),
        )
        .all()
    )

    overdue: list[tuple[int, DashboardAlert]] = []
    due_soon: list[tuple[int, DashboardAlert]] = []
    for o in orders:
        if o.id in shipped:
            continue
        days = (o.deadline - today).days
        status = ORDER_STATUS_LABELS[o.current_step]
        detail = f"{o.product} · 交期 {o.deadline} · {status}"
        if days < 0:
            overdue.append((-days, DashboardAlert(
                kind="overdue", level="danger", order_id=o.id,
                title=f"{o.id} 已超期 {-days} 天", detail=detail,
            )))
        elif days <= WARN_DAYS:
            due_soon.append((days, DashboardAlert(
                kind="due_soon", level="warning", order_id=o.id,
                title=f"{o.id} 今日到交期" if days == 0 else f"{o.id} 还剩 {days} 天到交期",
                detail=detail,
            )))

    overdue.sort(key=lambda x: -x[0])       # 超期最久的在前
    due_soon.sort(key=lambda x: x[0])       # 最快到期的在前
    return [a for _, a in overdue], [a for _, a in due_soon]


def _pending_doc_alerts(db: Session) -> list[DashboardAlert]:
    """未处理完的返工单 / 维修单。报废单没有状态流转，不算待处理。"""
    alerts = []
    for model, doc_type in ((ReworkOrder, "rework"), (RepairOrder, "repair")):
        docs = (
            db.query(model)
            .filter(model.status != DONE_STATUS)
            .order_by(model.created_at.desc(), model.id.desc())
            .all()
        )
        for d in docs:
            alerts.append(DashboardAlert(
                kind="pending_doc",
                level="warning",
                order_id=d.order_id,
                doc_type=doc_type,
                doc_id=d.id,
                title=f"{DISPOSITION_TYPE_LABELS[doc_type]} #{d.id} {d.status}",
                detail=(
                    f"{d.order_id} · {d.quantity} 件 · "
                    f"{STAGE_LABELS.get(d.stage, d.stage)} 判定"
                ),
            ))
    return alerts


def _shortage_alerts(db: Session) -> list[DashboardAlert]:
    """缺料工单。与备料页同口径：按工单聚合，只要有一项物料 status='short' 就算。"""
    rows = (
        db.query(OrderMaterial)
        .filter(OrderMaterial.status == "short")
        .order_by(OrderMaterial.order_id, OrderMaterial.id)
        .all()
    )
    by_order: dict[str, list[OrderMaterial]] = {}
    for m in rows:
        by_order.setdefault(m.order_id, []).append(m)

    alerts = []
    for order_id, materials in by_order.items():
        shown = "、".join(
            f"{m.name}（缺 {max(0, m.required - m.stock)}）" for m in materials[:3]
        )
        if len(materials) > 3:
            shown += f" 等 {len(materials)} 项"
        alerts.append(DashboardAlert(
            kind="shortage", level="danger", order_id=order_id,
            title=f"{order_id} 备料不足", detail=shown,
        ))
    return alerts


def _build_alerts(db: Session) -> list[DashboardAlert]:
    """按紧急程度排序：已超期 → 缺料 → 待处理单据 → 临期。"""
    overdue, due_soon = _deadline_alerts(db)
    return overdue + _shortage_alerts(db) + _pending_doc_alerts(db) + due_soon


def _quality_rate(db: Session) -> tuple[float | None, int]:
    """送检合格率 = Σ合格数 / Σ送检数（含质检1、表处、质检2 三个阶段的全部批次）。"""
    passed, total = db.query(
        func.coalesce(func.sum(QcInspection.pass_count), 0),
        func.coalesce(func.sum(QcInspection.quantity), 0),
    ).one()
    total = int(total or 0)
    if not total:
        return None, 0
    return round(int(passed or 0) / total * 100, 1), total


@router.get("/stats", response_model=DashboardStats)
def get_stats(db: Session = Depends(get_db)):
    today = date.today()
    today_orders = (
        db.query(func.count(Order.id)).filter(Order.create_date == today).scalar() or 0
    )
    in_production = (
        db.query(func.count(Order.id))
        .filter(Order.current_step >= PRODUCTION_STEP, Order.current_step < SHIPPING_STEP)
        .scalar() or 0
    )
    # 今天生成的出货单张数（原来这里数的是「所有 step>=7 的工单」，与「今日」无关）
    today_ship = (
        db.query(func.count(Shipment.id))
        .filter(func.date(Shipment.created_at) == today)
        .scalar() or 0
    )
    total = db.query(func.count(Order.id)).scalar() or 0
    completed = (
        db.query(func.count(Order.id)).filter(Order.current_step >= SHIPPING_STEP).scalar() or 0
    )
    quality_rate, quality_sample = _quality_rate(db)

    return DashboardStats(
        today_orders=today_orders,
        in_production=in_production,
        today_ship=today_ship,
        quality_rate=quality_rate,
        quality_sample=quality_sample,
        alert_count=len(_build_alerts(db)),
        completion_rate=round(completed / total * 100, 1) if total else 0.0,
    )


@router.get("/alerts", response_model=list[DashboardAlert])
def get_alerts(db: Session = Depends(get_db)):
    return _build_alerts(db)
