from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.contract import blocked_orders
from app.models.order import ORDER_STATUS_LABELS, PRODUCTION_STEP, QUALITY1_STEP, Order
from app.models.production import (
    WORK_ORDER_STATUS_DONE,
    Employee,
    OrderContribution,
    WorkOrder,
    WorkOrderEmployee,
    WorkOrderItem,
)
from app.routers.quality import advance_order_to
from app.schemas.production import (
    CompleteWorkRequest,
    CompleteWorkResponse,
    OrderEmployeeOut,
    StartWorkRequest,
    StartWorkResponse,
    WorkOrderEmployeeOut,
    WorkOrderItemOut,
    WorkOrderResponse,
)

router = APIRouter(prefix="/api/production", tags=["production"])


def _to_work_order_response(work_order: WorkOrder) -> WorkOrderResponse:
    return WorkOrderResponse(
        id=work_order.id,
        work_date=work_order.work_date,
        machine_id=work_order.machine_id,
        status=work_order.status,
        employees=[
            WorkOrderEmployeeOut(
                employee_id=link.employee_id,
                employee_name=link.employee.name if link.employee else "",
            )
            for link in work_order.employees
        ],
        items=[
            WorkOrderItemOut(
                order_id=item.order_id,
                product=item.order.product if item.order else "",
                quantity=item.order.quantity if item.order else 0,
                completed_qty=item.completed_qty,
            )
            for item in work_order.items
        ],
        completed_qty_total=sum(item.completed_qty or 0 for item in work_order.items),
        created_at=work_order.created_at,
        completed_at=work_order.completed_at,
    )


def _participant_ids(db: Session, order_id: str) -> list[str]:
    """该订单的参与员工：只要某员工的开工单里包含过这个订单就算，完工数填 0 也算。"""
    rows = (
        db.query(WorkOrderEmployee.employee_id)
        .join(WorkOrderItem, WorkOrderItem.work_order_id == WorkOrderEmployee.work_order_id)
        .filter(WorkOrderItem.order_id == order_id)
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def _save_contributions(
    db: Session,
    work_order: WorkOrder,
    filled_by: str,
    newly_done: list[str],
    contrib_req: dict,
) -> None:
    """给本次刚做到全量完成的订单落参与度。

    单人完成自动记 100%，不打扰现场；多人接力时必须由最后完成的这名员工
    给全部参与员工分配、合计 100%，否则整个完工提交失败（不写半截数据）。
    """
    for oid in newly_done:
        if db.query(OrderContribution.id).filter(OrderContribution.order_id == oid).first():
            continue  # 已记过就不再覆盖

        participants = _participant_ids(db, oid)
        if not participants:
            continue

        if len(participants) == 1:
            allocations = {participants[0]: 100}
        else:
            given = contrib_req.get(oid)
            if not given:
                raise HTTPException(
                    400,
                    f"订单 {oid} 由 {len(participants)} 名员工接力完成，"
                    f"请填写各人参与度百分比（合计须为 100）",
                )
            ids = [c.employee_id for c in given]
            if len(set(ids)) != len(ids):
                raise HTTPException(400, f"订单 {oid} 的参与度填写有重复员工")
            unknown = [i for i in ids if i not in participants]
            if unknown:
                raise HTTPException(
                    400, f"订单 {oid} 的参与度包含非参与员工: {', '.join(unknown)}"
                )
            missing = [p for p in participants if p not in set(ids)]
            if missing:
                raise HTTPException(
                    400, f"订单 {oid} 还有参与员工未分配参与度: {', '.join(missing)}"
                )
            total = sum(c.percent for c in given)
            if total != 100:
                raise HTTPException(
                    400, f"订单 {oid} 的参与度合计为 {total}%，必须等于 100%"
                )
            allocations = {c.employee_id: c.percent for c in given}

        for eid, percent in allocations.items():
            db.add(
                OrderContribution(
                    order_id=oid,
                    employee_id=eid,
                    percent=percent,
                    work_order_id=work_order.id,
                    filled_by=filled_by,
                )
            )
    db.flush()


def startable_error(orders: list[Order], action: str) -> str | None:
    """返回这批订单不能进开工单的原因；都能开工时返回 None。

    只有停在投产阶段(step 3)的订单能开工，两头都要拦：
      往前（接单/备料/程序）还没轮到它生产；
      往后（质检1/表处/质检2/出货）活已经干完流出去了，再开工会把同一批
      数量重复计入完工数和工时，质检里的订单还会被拉回「生产中」。
    返工/维修会把订单退回 step 3，那种是允许重新开工的。
    """

    def tagged(items: list[Order]) -> str:
        return ", ".join(f"{o.id}（{ORDER_STATUS_LABELS[o.current_step]}）" for o in items)

    early = [o for o in orders if o.current_step < PRODUCTION_STEP]
    if early:
        return f"订单尚未进入投产阶段，不能{action}: {tagged(early)}"

    passed = [o for o in orders if o.current_step > PRODUCTION_STEP]
    if passed:
        return f"订单已流转出投产阶段，不能{action}: {tagged(passed)}"

    return None


@router.post("/start-work", response_model=StartWorkResponse)
def start_work(req: StartWorkRequest, db: Session = Depends(get_db)):
    employee_ids = list(dict.fromkeys(req.employee_ids))
    order_ids = list(dict.fromkeys(req.order_ids))

    employees = db.query(Employee).filter(Employee.id.in_(employee_ids)).all()
    found_emp = {e.id for e in employees}
    missing_emp = [i for i in employee_ids if i not in found_emp]
    if missing_emp:
        raise HTTPException(404, f"员工不存在: {', '.join(missing_emp)}")

    orders = db.query(Order).filter(Order.id.in_(order_ids)).all()
    found_order = {o.id for o in orders}
    missing_order = [i for i in order_ids if i not in found_order]
    if missing_order:
        raise HTTPException(404, f"订单不存在: {', '.join(missing_order)}")

    not_ready = startable_error(orders, "开工")
    if not_ready:
        raise HTTPException(400, not_ready)

    # 合同状态非「正常」（暂停/取消/结案）的工单不能开工。
    # 完工不拦：活已经干完了，拦着只会让完工数量对不上实物。
    blocked = blocked_orders(db, order_ids)
    if blocked:
        detail = "、".join(f"{oid}（合同{status}）" for oid, status in blocked.items())
        raise HTTPException(400, f"所属合同状态不是「正常」，不能开工: {detail}")

    today = date.today()

    # 必须完工结单：名下还有未完工开工单的员工不能再开新单。
    # 一天能开几张不限（原先的「一人一天一张」已放开，DB 唯一约束也一并删了，
    # 见 migrations/20261008_drop_employee_work_date_unique.sql）。
    open_rows = (
        db.query(WorkOrderEmployee.employee_id, WorkOrder.id, WorkOrder.work_date)
        .join(WorkOrder, WorkOrder.id == WorkOrderEmployee.work_order_id)
        .filter(
            WorkOrderEmployee.employee_id.in_(employee_ids),
            WorkOrder.status != WORK_ORDER_STATUS_DONE,
        )
        .all()
    )
    if open_rows:
        emp_name = {e.id: e.name for e in employees}
        detail = "、".join(
            f"{emp_name.get(eid, eid)}({eid}) 开工单 #{wid}（{wd}）"
            for eid, wid, wd in open_rows
        )
        raise HTTPException(400, f"以下员工还有未完工的开工单，必须先完工结单: {detail}")

    work_order = WorkOrder(work_date=today)
    work_order.employees = [
        WorkOrderEmployee(employee_id=eid, work_date=today) for eid in employee_ids
    ]
    work_order.items = [WorkOrderItem(order_id=oid) for oid in order_ids]
    db.add(work_order)

    db.commit()
    db.refresh(work_order)
    return StartWorkResponse(
        success=True,
        message=f"开工成功：开工单 #{work_order.id}，{len(employee_ids)} 名员工 / {len(order_ids)} 个订单",
        work_order=_to_work_order_response(work_order),
    )


@router.get("/work-orders", response_model=list[WorkOrderResponse])
def list_work_orders(status: str | None = None, limit: int = 200, db: Session = Depends(get_db)):
    query = db.query(WorkOrder)
    if status:
        query = query.filter(WorkOrder.status == status)

    query = query.order_by(WorkOrder.work_date.desc(), WorkOrder.id.desc())
    return [_to_work_order_response(w) for w in query.limit(limit).all()]


@router.post("/complete-work", response_model=CompleteWorkResponse)
def complete_work(req: CompleteWorkRequest, db: Session = Depends(get_db)):
    work_order = db.query(WorkOrder).filter(WorkOrder.id == req.work_order_id).first()
    if not work_order:
        raise HTTPException(404, "开工单不存在")
    if work_order.status == "完工":
        raise HTTPException(400, "该开工单已完工，不能重复提交")

    owners = [link.employee_id for link in work_order.employees]
    if req.employee_id not in owners:
        owners_text = ", ".join(owners)
        raise HTTPException(403, f"完工工号 {req.employee_id} 不是该开工单负责人（{owners_text}）")

    item_map = {item.order_id: item for item in work_order.items}

    # 提交中出现、但不在开工单里的订单 = 完工时扫码新增的订单
    new_ids = [entry.order_id for entry in req.items if entry.order_id not in item_map]
    if new_ids:
        new_orders = db.query(Order).filter(Order.id.in_(new_ids)).all()
        found_new = {o.id for o in new_orders}
        missing_new = [i for i in new_ids if i not in found_new]
        if missing_new:
            raise HTTPException(404, f"订单不存在: {', '.join(missing_new)}")
        not_ready = startable_error(new_orders, "加入开工单")
        if not_ready:
            raise HTTPException(400, not_ready)

        for order_id in new_ids:
            item = WorkOrderItem(work_order_id=work_order.id, order_id=order_id)
            db.add(item)
            item_map[order_id] = item
        db.flush()

    filled: set[str] = set()
    for entry in req.items:
        if entry.order_id in filled:
            raise HTTPException(400, f"订单 {entry.order_id} 重复填写")
        filled.add(entry.order_id)

    # 未提交的订单按 0 处理：接力生产时本次没做完的就是 0，
    # 剩下的量由下一个员工的开工单继续做
    submitted = {entry.order_id: entry.completed_qty for entry in req.items}
    contrib_req = {
        entry.order_id: entry.contributions for entry in req.items if entry.contributions
    }
    for oid, item in item_map.items():
        item.completed_qty = submitted.get(oid, 0)
    db.flush()

    order_ids = list(item_map)
    orders_map = {o.id: o for o in db.query(Order).filter(Order.id.in_(order_ids)).all()}

    # 累计完工数 = 其它开工单已提交的 + 本次提交的
    prior = {
        oid: int(total or 0)
        for oid, total in db.query(
            WorkOrderItem.order_id, func.sum(WorkOrderItem.completed_qty)
        )
        .filter(
            WorkOrderItem.order_id.in_(order_ids),
            WorkOrderItem.work_order_id != work_order.id,
            WorkOrderItem.completed_qty.isnot(None),
        )
        .group_by(WorkOrderItem.order_id)
        .all()
    }

    newly_done: list[str] = []
    for oid in order_ids:
        order = orders_map.get(oid)
        if not order:
            continue
        total = prior.get(oid, 0) + submitted.get(oid, 0)
        # 只有累计做到全量、且还停在生产阶段的订单才流入质检1；
        # 没做完的留在生产阶段（开工单已完工，状态显示「待生产」），可继续被接力开工。
        # 已经往后走过阶段的不回退。
        if order.quantity and total >= order.quantity and order.current_step == PRODUCTION_STEP:
            advance_order_to(order, QUALITY1_STEP)
            newly_done.append(oid)

    _save_contributions(db, work_order, req.employee_id, newly_done, contrib_req)

    work_order.status = WORK_ORDER_STATUS_DONE
    work_order.completed_at = datetime.now()
    db.commit()
    db.refresh(work_order)

    return CompleteWorkResponse(
        success=True, message="完工提交成功", work_order=_to_work_order_response(work_order)
    )


@router.get("/completed-qty", response_model=dict[str, int])
def completed_qty_by_order(db: Session = Depends(get_db)):
    rows = (
        db.query(WorkOrderItem.order_id, func.sum(WorkOrderItem.completed_qty))
        .filter(WorkOrderItem.completed_qty.isnot(None))
        .group_by(WorkOrderItem.order_id)
        .all()
    )
    return {order_id: int(total or 0) for order_id, total in rows}


@router.get("/order-employees/{order_id}", response_model=list[OrderEmployeeOut])
def list_order_employees(order_id: str, db: Session = Depends(get_db)):
    links = (
        db.query(WorkOrderEmployee)
        .join(WorkOrderItem, WorkOrderItem.work_order_id == WorkOrderEmployee.work_order_id)
        .filter(WorkOrderItem.order_id == order_id)
        .all()
    )

    names: dict[str, str] = {}
    for link in links:
        if link.employee_id not in names:
            names[link.employee_id] = link.employee.name if link.employee else ""

    # 参与度只在订单全量完成时写过，没写完的订单这里是空 dict，percent 返回 None
    percents = {
        row[0]: row[1]
        for row in db.query(OrderContribution.employee_id, OrderContribution.percent)
        .filter(OrderContribution.order_id == order_id)
        .all()
    }
    return [
        OrderEmployeeOut(employee_id=k, employee_name=v, percent=percents.get(k))
        for k, v in names.items()
    ]
