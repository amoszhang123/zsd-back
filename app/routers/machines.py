"""机台主数据接口：增改查、历史开工记录、二维码标签。

几个刻意的取舍：

1. **编号由后端生成，前端不能传**。机台上贴的静态二维码里装的就是编号，改号
   等于换码换标签，所以编号一旦生成只读。建错了改名/型号即可，用不上就停用。
2. **不提供删除**。历史开工单的订单行还引用着它（work_order_items.machine_id），
   删了就把历史变成悬空引用。与员工离职、物料停用同一套思路。
3. **列表的统计列一次 group by 算完**，不为每行单独查 —— 机台数量不大，但
   将来几十上百台时逐行 N+1 会很明显。

鉴权沿用项目现状：模块可见性由权限管理页控制，接口本身不拦（与其它 CRUD 一致）。
"""
import base64
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.machine_label import MachineLabelData, render_machine_label
from app.models.production import (
    WORK_ORDER_STATUS_DONE,
    Employee,
    Machine,
    WorkOrder,
    WorkOrderEmployee,
    WorkOrderItem,
)
from app.models.order import Order
from app.schemas.machine import (
    LabelPagesOut,
    MachineCreate,
    MachineHistoryItem,
    MachineLabelRequest,
    MachineListOut,
    MachineOut,
    MachineSummary,
    MachineUpdate,
)

router = APIRouter(prefix="/api/machines", tags=["machines"])


def _next_machine_code(db: Session) -> str:
    """MC0001 递增。编号定长零填充，所以字符串 max() 就是数值 max()。"""
    last = db.query(func.max(Machine.id)).filter(Machine.id.like("MC%")).scalar()
    seq = 1
    if last and re.fullmatch(r"MC\d{4}", last):
        seq = int(last[2:]) + 1
    if seq > 9999:
        raise HTTPException(400, "机台编号已用尽（MC9999），请调整编号规则后再新增")
    return f"MC{seq:04d}"


def _get_machine(db: Session, machine_id: str) -> Machine:
    machine = db.query(Machine).filter(Machine.id == machine_id).first()
    if not machine:
        raise HTTPException(404, f"机台不存在: {machine_id}")
    return machine


def _stats_map(db: Session, machine_ids: list[str]) -> dict[str, dict]:
    """每台机台干过多少张单、多少个订单、最后一次是什么时候。一次查完。"""
    empty = {"work_order_count": 0, "order_count": 0, "last_used_at": None}
    if not machine_ids:
        return {}
    rows = (
        db.query(
            WorkOrderItem.machine_id,
            func.count(func.distinct(WorkOrderItem.work_order_id)),
            func.count(func.distinct(WorkOrderItem.order_id)),
            func.max(WorkOrder.created_at),
        )
        .join(WorkOrder, WorkOrder.id == WorkOrderItem.work_order_id)
        .filter(WorkOrderItem.machine_id.in_(machine_ids))
        .group_by(WorkOrderItem.machine_id)
        .all()
    )
    out = {mid: dict(empty) for mid in machine_ids}
    for mid, wo_cnt, order_cnt, last_at in rows:
        out[mid] = {
            "work_order_count": int(wo_cnt or 0),
            "order_count": int(order_cnt or 0),
            "last_used_at": last_at,
        }
    return out


def _to_out(machine: Machine, stats: dict) -> MachineOut:
    return MachineOut(
        id=machine.id,
        name=machine.name,
        model=machine.model,
        active=bool(machine.active),
        created_at=machine.created_at,
        **stats,
    )


@router.get("", response_model=MachineListOut)
def list_machines(
    include_inactive: bool = Query(True, description="是否带出已停用的机台"),
    db: Session = Depends(get_db),
):
    query = db.query(Machine)
    if not include_inactive:
        query = query.filter(Machine.active.is_(True))
    machines = query.order_by(Machine.id).all()

    ids = [m.id for m in machines]
    stats = _stats_map(db, ids)

    # 「在忙」= 这台机台的订单行还挂在一张没完工的开工单上
    busy_ids = set()
    if ids:
        busy_ids = {
            row[0]
            for row in db.query(WorkOrderItem.machine_id)
            .join(WorkOrder, WorkOrder.id == WorkOrderItem.work_order_id)
            .filter(
                WorkOrderItem.machine_id.in_(ids),
                WorkOrder.status != WORK_ORDER_STATUS_DONE,
            )
            .distinct()
            .all()
        }

    total = len(machines)
    active = sum(1 for m in machines if m.active)
    return MachineListOut(
        rows=[_to_out(m, stats.get(m.id, {})) for m in machines],
        summary=MachineSummary(
            total=total,
            active=active,
            inactive=total - active,
            busy=len(busy_ids),
        ),
    )


@router.post("", response_model=MachineOut)
def create_machine(req: MachineCreate, db: Session = Depends(get_db)):
    """建档。编号自动生成，重名允许（不同车间可能都叫「加工中心 1」）。"""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "机台名称不能为空")

    # 并发建两台时 max(id) 可能算出同一个号，靠主键唯一兜底后重试一次，
    # 而不是把 IntegrityError 抛成 500
    for attempt in range(3):
        machine = Machine(
            id=_next_machine_code(db), name=name, model=(req.model or "").strip()
        )
        db.add(machine)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise HTTPException(409, "机台编号冲突，请重试")
            continue
        db.refresh(machine)
        return _to_out(machine, {})
    raise HTTPException(409, "机台编号冲突，请重试")


@router.patch("/{machine_id}", response_model=MachineOut)
def update_machine(machine_id: str, req: MachineUpdate, db: Session = Depends(get_db)):
    machine = _get_machine(db, machine_id)

    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "机台名称不能为空")
        machine.name = name
    if req.model is not None:
        machine.model = req.model.strip()
    if req.active is not None:
        machine.active = req.active

    db.commit()
    db.refresh(machine)
    return _to_out(machine, _stats_map(db, [machine.id]).get(machine.id, {}))


@router.get("/{machine_id}", response_model=MachineOut)
def get_machine(machine_id: str, db: Session = Depends(get_db)):
    machine = _get_machine(db, machine_id)
    return _to_out(machine, _stats_map(db, [machine.id]).get(machine.id, {}))


@router.get("/{machine_id}/history", response_model=list[MachineHistoryItem])
def machine_history(
    machine_id: str,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """这台机台干过的活，按开工时间倒序。

    粒度是「开工单里的一个订单」而不是开工单：一张单可能只有某几行是这台机器
    做的，按单列出来会让人误以为整张单都在这台机器上。
    """
    _get_machine(db, machine_id)

    rows = (
        db.query(WorkOrderItem, WorkOrder, Order)
        .join(WorkOrder, WorkOrder.id == WorkOrderItem.work_order_id)
        .outerjoin(Order, Order.id == WorkOrderItem.order_id)
        .filter(WorkOrderItem.machine_id == machine_id)
        .order_by(WorkOrder.work_date.desc(), WorkOrder.id.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return []

    # 负责人一次查完，避免逐行查
    wo_ids = list({wo.id for _, wo, _ in rows})
    emp_rows = (
        db.query(WorkOrderEmployee.work_order_id, Employee.name)
        .join(Employee, Employee.id == WorkOrderEmployee.employee_id)
        .filter(WorkOrderEmployee.work_order_id.in_(wo_ids))
        .all()
    )
    names: dict[int, list[str]] = {}
    for wo_id, emp_name in emp_rows:
        names.setdefault(wo_id, []).append(emp_name)

    return [
        MachineHistoryItem(
            work_order_id=wo.id,
            work_date=wo.work_date,
            status=wo.status,
            order_id=item.order_id,
            product=order.product if order else "",
            quantity=order.quantity if order else 0,
            completed_qty=item.completed_qty,
            employees="、".join(names.get(wo.id, [])),
            created_at=wo.created_at,
        )
        for item, wo, order in rows
    ]


# ---------------------------------------------------------------- 标签

def _label_data(m: Machine) -> MachineLabelData:
    return MachineLabelData(code=m.id, name=m.name, model=m.model, active=bool(m.active))


@router.get("/{machine_id}/label")
def machine_label(machine_id: str, db: Session = Depends(get_db)):
    machine = _get_machine(db, machine_id)
    return Response(content=render_machine_label(_label_data(machine)),
                    media_type="image/png")


@router.post("/labels", response_model=LabelPagesOut)
def batch_labels(req: MachineLabelRequest, db: Session = Depends(get_db)):
    """批量出图。100×70 热敏一页一张，N 台就是 N 页，前端按页插分页符连着打。"""
    machines = db.query(Machine).filter(Machine.id.in_(req.ids)).all()
    by_id = {m.id: m for m in machines}
    ordered = [by_id[i] for i in req.ids if i in by_id]
    if not ordered:
        raise HTTPException(404, "所选机台都不存在")

    return LabelPagesOut(
        pages=[
            "data:image/png;base64," + base64.b64encode(render_machine_label(_label_data(m))).decode()
            for m in ordered
        ]
    )
