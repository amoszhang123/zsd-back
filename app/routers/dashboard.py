import re
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.models.production import (
    WORK_ORDER_STATUS_DONE,
    Employee,
    OrderContribution,
    WorkOrder,
    WorkOrderEmployee,
    WorkOrderItem,
)
from app.models.quality import (
    DISPOSITION_TYPE_LABELS,
    STAGE_LABELS,
    QcInspection,
    RepairOrder,
    ReworkOrder,
    ScrapOrder,
)
from app.models.shipment import Shipment, ShipmentItem
from app.schemas.order import (
    DashboardAlert,
    DashboardStats,
    EmployeePerfBoard,
    EmployeePerfRow,
)

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


# ---------------------------------------------------------------- 员工效能看板

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    """'2026-10' → [10-01 00:00, 11-01 00:00)，左闭右开，避免跨月重复计数。"""
    year, mon = int(month[:4]), int(month[5:7])
    start = datetime.combine(date(year, mon, 1), time.min)
    end = datetime.combine(
        date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1), time.min
    )
    return start, end


def _good_rate(output_qty: int, defect_qty: int) -> float | None:
    """良品率 = (产出 − 不良) / 产出 × 100。

    没有产出时返回 None（前端显示「—」），不要拿 100% 冒充「没数据」。
    返工后复检再次判废，同一件会被记两次不良，理论上能超过产出；
    这种情况夹到 0%，避免看板上出现负良品率。
    """
    if output_qty <= 0:
        return None
    rate = (output_qty - defect_qty) / output_qty * 100
    return round(max(0.0, min(100.0, rate)), 1)


def _hours_by_employee(db: Session, start: datetime, end: datetime) -> tuple[
    dict[str, float], dict[str, int], int, int
]:
    """投产工时：以「质检1首次合格」作为订单完成时点，按参与度分摊预估工时。

    只认 verdict='pass' 的质检1批次：返工/维修/报废都不是完成，不计入。
    参与度由把订单做到全量完成的那名员工在完工时填写（见 complete_work），
    合计 100%，所以这里直接乘 percent/100 即可。
    """
    first_pass = (
        db.query(
            QcInspection.order_id.label("order_id"),
            func.min(QcInspection.created_at).label("finished_at"),
        )
        .filter(QcInspection.stage == "quality1", QcInspection.verdict == "pass")
        .group_by(QcInspection.order_id)
        .subquery()
    )
    finished = (
        db.query(first_pass.c.order_id, Order.estimated_hours)
        .join(Order, Order.id == first_pass.c.order_id)
        .filter(first_pass.c.finished_at >= start, first_pass.c.finished_at < end)
        .all()
    )
    if not finished:
        return {}, {}, 0, 0

    order_ids = [row[0] for row in finished]
    hours_by_order = {row[0]: float(row[1] or 0) for row in finished}
    contributions = (
        db.query(
            OrderContribution.order_id,
            OrderContribution.employee_id,
            OrderContribution.percent,
        )
        .filter(OrderContribution.order_id.in_(order_ids))
        .all()
    )

    hours: dict[str, float] = {}
    orders: dict[str, set[str]] = {}
    for c in contributions:
        base = hours_by_order.get(c.order_id, 0.0)
        hours[c.employee_id] = hours.get(c.employee_id, 0.0) + base * (c.percent or 0) / 100
        orders.setdefault(c.employee_id, set()).add(c.order_id)

    # 质检1合格却没填预估工时的订单：全外协、跳过程序的单子都是 0，
    # 这些订单的工时会静默变成 0，前端要提示，否则用户以为统计漏了。
    missing = sum(1 for h in hours_by_order.values() if h <= 0)
    return (
        {k: round(v, 2) for k, v in hours.items()},
        {k: len(v) for k, v in orders.items()},
        missing,
        len(set(order_ids)),
    )


def _output_by_employee(db: Session, start: datetime, end: datetime) -> dict[str, int]:
    """当月产出件数：员工参与的、已完工的开工单里各订单的完工数量。

    一张开工单常有多个负责人，同一件产出是共同完成的，所以每个人都按整批计 ——
    良品率是「我经手的东西里坏了几件」，不该按人头摊薄。
    只统计已完工的开工单：没结单的完工数量还是半截数，算进产出没意义。
    """
    rows = (
        db.query(
            WorkOrderEmployee.employee_id,
            func.coalesce(func.sum(WorkOrderItem.completed_qty), 0),
        )
        .join(WorkOrderItem, WorkOrderItem.work_order_id == WorkOrderEmployee.work_order_id)
        .join(WorkOrder, WorkOrder.id == WorkOrderEmployee.work_order_id)
        .filter(
            WorkOrder.status == WORK_ORDER_STATUS_DONE,
            WorkOrder.work_date >= start.date(),
            WorkOrder.work_date < end.date(),
            WorkOrderItem.completed_qty.isnot(None),
        )
        .group_by(WorkOrderEmployee.employee_id)
        .all()
    )
    return {eid: int(qty or 0) for eid, qty in rows if int(qty or 0) > 0}


def _output_total(db: Session, start: datetime, end: datetime) -> int:
    """当月全厂产出件数（不按员工拆分）。

    用于顶部的「整体良品率」。不能拿员工产出相加：多人共做一张开工单时，
    每个人都被记了整批，相加会把产出翻倍，把整体良品率摊高。
    """
    total = (
        db.query(func.coalesce(func.sum(WorkOrderItem.completed_qty), 0))
        .join(WorkOrder, WorkOrder.id == WorkOrderItem.work_order_id)
        .filter(
            WorkOrder.status == WORK_ORDER_STATUS_DONE,
            WorkOrder.work_date >= start.date(),
            WorkOrder.work_date < end.date(),
            WorkOrderItem.completed_qty.isnot(None),
        )
        .scalar()
    )
    return int(total or 0)


def _defects_by_employee(db: Session, start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """当月不良件数：返工 + 报废 + 维修单，按单据的负责人（employee_id）归属。

    employee_id 是判定时选的「第一位负责人」（见 submit_judgment），
    异常单的多位负责人没有数量拆分口径，因此只归给第一位，避免同一批不良重复计。
    没选负责人的单据不进个人统计（合计里也如实反映在员工行之和上）。
    """
    result: dict[str, dict[str, int]] = {}
    for model, key in ((ReworkOrder, "rework"), (ScrapOrder, "scrap"), (RepairOrder, "repair")):
        rows = (
            db.query(model.employee_id, func.coalesce(func.sum(model.quantity), 0))
            .filter(
                model.employee_id.isnot(None),
                model.created_at >= start,
                model.created_at < end,
            )
            .group_by(model.employee_id)
            .all()
        )
        for eid, qty in rows:
            bucket = result.setdefault(eid, {"rework": 0, "scrap": 0, "repair": 0})
            bucket[key] = int(qty or 0)
    return result


@router.get("/employee-performance", response_model=EmployeePerfBoard)
def get_employee_performance(
    month: str = Query("", description="月份 YYYY-MM，缺省为当月"),
    db: Session = Depends(get_db),
):
    """员工效能看板：当月投产工时 + 良品率。

    两个指标的时间口径不同，不要混：
      投产工时 —— 订单在当月「质检1合格」，按完成算，之后返工也不再扣回；
      良品率   —— 当月实际发生的完工产出与不良判定，按开工单完工日期与异常单创建时间。
    """
    month = month or f"{date.today().year:04d}-{date.today().month:02d}"
    if not MONTH_PATTERN.match(month):
        raise HTTPException(400, "月份格式应为 YYYY-MM")
    start, end = _month_bounds(month)

    hours_map, order_count_map, missing_hours, finished_orders = _hours_by_employee(
        db, start, end
    )
    output_map = _output_by_employee(db, start, end)
    defect_map = _defects_by_employee(db, start, end)

    employee_ids = set(hours_map) | set(output_map) | set(defect_map)
    employees = (
        db.query(Employee).filter(Employee.id.in_(employee_ids)).all() if employee_ids else []
    )
    employee_map = {e.id: e for e in employees}

    total_hours = round(sum(hours_map.values()), 2)
    rows: list[EmployeePerfRow] = []
    for eid in employee_ids:
        emp = employee_map.get(eid)
        defect = defect_map.get(eid, {})
        defect_qty = sum(defect.values())
        output_qty = int(output_map.get(eid, 0))
        hours = float(hours_map.get(eid, 0.0))
        rows.append(
            EmployeePerfRow(
                employee_id=eid,
                employee_name=emp.name if emp else "",
                position=emp.position if emp else "",
                employee_type=emp.employee_type if emp else "",
                status=emp.status if emp else "",
                hours=hours,
                order_count=order_count_map.get(eid, 0),
                output_qty=output_qty,
                defect_qty=defect_qty,
                rework_qty=defect.get("rework", 0),
                scrap_qty=defect.get("scrap", 0),
                repair_qty=defect.get("repair", 0),
                good_rate=_good_rate(output_qty, defect_qty),
                hours_share=round(hours / total_hours * 100, 1) if total_hours else 0.0,
            )
        )
    # 工时高的在前；工时打平（多数人都是 0）时按良品率高的在前
    rows.sort(key=lambda r: (-r.hours, -(r.good_rate or -1), r.employee_id))

    total_output = _output_total(db, start, end)
    total_defect = sum(r.defect_qty for r in rows)
    return EmployeePerfBoard(
        month=month,
        rows=rows,
        total_hours=total_hours,
        total_output=total_output,
        total_defect=total_defect,
        good_rate=_good_rate(total_output, total_defect),
        employee_count=len(rows),
        order_count=finished_orders,
        missing_hours_orders=missing_hours,
    )
