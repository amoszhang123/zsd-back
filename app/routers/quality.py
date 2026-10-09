from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.order import (
    ORDER_TYPE_FULL_OUTSOURCE,
    PRODUCTION_STEP,
    QUALITY1_STEP,
    QUALITY2_STEP,
    SHIPPING_STEP,
    SURFACE_STEP,
    Order,
    resolve_order_status,
)
from app.models.production import Employee, WorkOrderItem, in_production_order_ids
from app.models.quality import (
    DISPOSITION_TYPE_LABELS,
    DispositionOwner,
    QcInspection,
    RepairOrder,
    ReworkOrder,
    ScrapOrder,
)
from app.schemas.quality import (
    DispositionCreate,
    DispositionLossUpdate,
    DispositionOut,
    DispositionStatusUpdate,
    OwnerIn,
    OwnerOut,
    QcInspectionOut,
    QcJudgmentRequest,
    QcJudgmentResponse,
    QcQueueItem,
    Stage,
)

router = APIRouter(prefix="/api/quality", tags=["quality"])

MODELS = {"rework": ReworkOrder, "scrap": ScrapOrder, "repair": RepairOrder}
TYPE_LABELS = DISPOSITION_TYPE_LABELS

# 每个质检阶段的产出来自上一个阶段的合格数；quality1 的产出是开工单的完工数量
STAGE_UPSTREAM = {"quality1": None, "surface": "quality1", "quality2": "surface"}


def _upstream_qty_map(db: Session, stage: str) -> dict[str, int]:
    """订单 → 流入本阶段的数量。"""
    if STAGE_UPSTREAM[stage] is None:
        rows = (
            db.query(WorkOrderItem.order_id, func.sum(WorkOrderItem.completed_qty))
            .filter(WorkOrderItem.completed_qty.isnot(None))
            .group_by(WorkOrderItem.order_id)
            .all()
        )
        result = {oid: int(total or 0) for oid, total in rows if int(total or 0) > 0}
        # 全外协不在厂内生产，永远没有开工单，完工数恒为 0 —— 只按完工数驱动的话
        # 它会永远不出现在质检1 队列里。它的待检数量就是订单全量（厂外整批交回一次检）。
        # 限定 current_step >= QUALITY1_STEP，否则还没接单的工单也会涌进待检队列。
        outsourced = (
            db.query(Order.id, Order.quantity)
            .filter(
                Order.order_type == ORDER_TYPE_FULL_OUTSOURCE,
                Order.current_step >= QUALITY1_STEP,
                Order.quantity > 0,
            )
            .all()
        )
        for oid, qty in outsourced:
            result[oid] = int(qty)
        return result

    rows = (
        db.query(QcInspection.order_id, func.sum(QcInspection.pass_count))
        .filter(QcInspection.stage == STAGE_UPSTREAM[stage])
        .group_by(QcInspection.order_id)
        .all()
    )
    return {oid: int(total or 0) for oid, total in rows if int(total or 0) > 0}


def _stage_stats(db: Session, order_ids: list[str], stage: str) -> dict[str, dict]:
    """订单 → 本阶段已检数量 / 累计合格 / 批次数 / 最近一次判定。"""
    stats: dict[str, dict] = {}
    if not order_ids:
        return stats
    batches = (
        db.query(QcInspection)
        .filter(QcInspection.order_id.in_(order_ids), QcInspection.stage == stage)
        .order_by(QcInspection.created_at, QcInspection.id)
        .all()
    )
    for b in batches:
        s = stats.setdefault(
            b.order_id, {"inspected": 0, "passed": 0, "scrap": 0, "batches": 0, "last_verdict": ""}
        )
        s["inspected"] += b.quantity
        s["passed"] += b.pass_count
        s["scrap"] += b.scrap_qty
        s["batches"] += 1
        s["last_verdict"] = b.verdict
    return stats


def _to_out(doc, doc_type: str, owners: list[OwnerOut] | None = None) -> DispositionOut:
    owners = owners or []
    return DispositionOut(
        id=doc.id,
        type=doc_type,
        order_id=doc.order_id,
        product=doc.order.product if doc.order else "",
        quantity=doc.quantity,
        employee_id=doc.employee_id,
        employee_name=doc.employee.name if doc.employee else "",
        stage=doc.stage,
        status=getattr(doc, "status", None),
        created_at=doc.created_at,
        updated_at=getattr(doc, "updated_at", None),
        owners=owners,
        total_labor_hours=round(sum(o.labor_hours for o in owners), 2),
        total_material_cost=round(sum(o.material_cost for o in owners), 2),
    )


def _owners_map(db: Session, doc_type: str, doc_ids: list[int]) -> dict[int, list[OwnerOut]]:
    """批量取某类单据的负责人，避免逐单查询。"""
    if not doc_ids:
        return {}
    rows = (
        db.query(DispositionOwner)
        .options(joinedload(DispositionOwner.employee))
        .filter(
            DispositionOwner.disposition_type == doc_type,
            DispositionOwner.disposition_id.in_(doc_ids),
        )
        .order_by(DispositionOwner.disposition_id, DispositionOwner.id)
        .all()
    )
    grouped: dict[int, list[OwnerOut]] = {}
    for r in rows:
        grouped.setdefault(r.disposition_id, []).append(
            OwnerOut(
                employee_id=r.employee_id,
                employee_name=r.employee.name if r.employee else "",
                labor_hours=r.labor_hours,
                material_cost=r.material_cost,
                updated_at=r.updated_at,
            )
        )
    return grouped


def _resolve_owners(db: Session, owners: list[OwnerIn], employee_id: str | None) -> list[OwnerIn]:
    """校验负责人名单：员工必须存在、不能重复。

    没传 owners 时退回用单选 employee_id 造一条，兼容旧调用方。
    """
    resolved = list(owners or [])
    if not resolved and employee_id:
        resolved = [OwnerIn(employee_id=employee_id)]
    if not resolved:
        return []

    ids = [o.employee_id for o in resolved]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "负责人名单里有重复的员工")
    found = {e.id for e in db.query(Employee).filter(Employee.id.in_(ids)).all()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(404, f"员工不存在: {', '.join(missing)}")
    return resolved


def _save_owners(db: Session, doc_type: str, doc_id: int, owners: list[OwnerIn]) -> None:
    """写负责人及其工时/金额。三种单据都收集，金额含义按类型区分：
    报废=物料损失，返工/维修=额外支出。"""
    for o in owners:
        db.add(
            DispositionOwner(
                disposition_type=doc_type,
                disposition_id=doc_id,
                employee_id=o.employee_id,
                labor_hours=o.labor_hours,
                material_cost=o.material_cost,
            )
        )


def _ids_only(owners: list[OwnerIn]) -> list[OwnerIn]:
    """只保留名单、把损耗归零。用于 return_owners 缺省时的回退。"""
    return [OwnerIn(employee_id=o.employee_id) for o in owners]


def _create_disposition(
    model, doc_type: str, req: DispositionCreate, db: Session
) -> DispositionOut:
    order = db.query(Order).filter(Order.id == req.order_id).first()
    if not order:
        raise HTTPException(404, f"订单不存在: {req.order_id}")

    owners = _resolve_owners(db, req.owners, req.employee_id)
    # employee_id 是历史遗留的单选列，保留写入第一位负责人，
    # 让只看 employee_name 的旧界面不至于空白；权威名单是 owners。
    doc = model(
        order_id=order.id,
        quantity=req.quantity,
        employee_id=owners[0].employee_id if owners else None,
        stage=req.stage,
    )
    db.add(doc)
    db.flush()
    _save_owners(db, doc_type, doc.id, owners)
    db.commit()
    db.refresh(doc)
    return _to_out(doc, doc_type, _owners_map(db, doc_type, [doc.id]).get(doc.id, []))


@router.post("/rework", response_model=DispositionOut)
def create_rework(req: DispositionCreate, db: Session = Depends(get_db)):
    return _create_disposition(ReworkOrder, "rework", req, db)


@router.post("/scrap", response_model=DispositionOut)
def create_scrap(req: DispositionCreate, db: Session = Depends(get_db)):
    return _create_disposition(ScrapOrder, "scrap", req, db)


@router.post("/repair", response_model=DispositionOut)
def create_repair(req: DispositionCreate, db: Session = Depends(get_db)):
    return _create_disposition(RepairOrder, "repair", req, db)


@router.get("/dispositions", response_model=list[DispositionOut])
def list_dispositions(
    stage: str | None = None, order_id: str | None = None, db: Session = Depends(get_db)
):
    result: list[DispositionOut] = []
    for doc_type, model in MODELS.items():
        query = db.query(model)
        if stage:
            query = query.filter(model.stage == stage)
        if order_id:
            query = query.filter(model.order_id == order_id)
        docs = query.order_by(model.created_at.desc(), model.id.desc()).all()
        owners = _owners_map(db, doc_type, [d.id for d in docs])
        for doc in docs:
            result.append(_to_out(doc, doc_type, owners.get(doc.id, [])))

    result.sort(key=lambda d: (d.created_at, d.id), reverse=True)
    return result


def _update_status(
    model, doc_type: str, doc_id: int, req: DispositionStatusUpdate, db: Session
) -> DispositionOut:
    doc = db.query(model).filter(model.id == doc_id).first()
    if not doc:
        raise HTTPException(404, f"{TYPE_LABELS[doc_type]}不存在: #{doc_id}")
    if doc.status != req.status:
        doc.status = req.status
        db.commit()
        db.refresh(doc)
    return _to_out(doc, doc_type, _owners_map(db, doc_type, [doc.id]).get(doc.id, []))


@router.patch("/rework/{doc_id}", response_model=DispositionOut)
def update_rework(doc_id: int, req: DispositionStatusUpdate, db: Session = Depends(get_db)):
    return _update_status(ReworkOrder, "rework", doc_id, req, db)


@router.patch("/repair/{doc_id}", response_model=DispositionOut)
def update_repair(doc_id: int, req: DispositionStatusUpdate, db: Session = Depends(get_db)):
    return _update_status(RepairOrder, "repair", doc_id, req, db)


@router.patch("/{doc_type}/{doc_id}/losses", response_model=DispositionOut)
def update_disposition_losses(
    doc_type: str, doc_id: int, req: DispositionLossUpdate, db: Session = Depends(get_db)
):
    """修改单据的工时与金额（报废=物料损失，返工/维修=额外支出）。

    只能改名单上已有的人，不能用这个接口增删负责人；传谁改谁，没传的保持原值。
    """
    if doc_type not in MODELS:
        raise HTTPException(404, f"未知的单据类型: {doc_type}")

    model = MODELS[doc_type]
    doc = db.query(model).filter(model.id == doc_id).first()
    if not doc:
        raise HTTPException(404, f"{TYPE_LABELS[doc_type]}不存在: #{doc_id}")

    rows = (
        db.query(DispositionOwner)
        .filter(
            DispositionOwner.disposition_type == doc_type,
            DispositionOwner.disposition_id == doc_id,
        )
        .all()
    )
    by_emp = {r.employee_id: r for r in rows}

    ids = [o.employee_id for o in req.owners]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "提交里有重复的员工")
    unknown = [i for i in ids if i not in by_emp]
    if unknown:
        names = "、".join(unknown)
        raise HTTPException(
            400,
            f"{names} 不是{TYPE_LABELS[doc_type]} #{doc_id} 的负责人，不能通过此接口增删负责人",
        )

    for o in req.owners:
        row = by_emp[o.employee_id]
        row.labor_hours = o.labor_hours
        row.material_cost = o.material_cost
        row.updated_at = datetime.now()

    db.commit()
    db.refresh(doc)
    return _to_out(doc, doc_type, _owners_map(db, doc_type, [doc.id]).get(doc.id, []))


VERDICT_LABELS = {"pass": "合格", "rework": "返工", "repair": "维修", "scrap": "整批报废"}
STAGE_LABELS = {"quality1": "质检1", "surface": "表处", "quality2": "质检2"}
STAGE_STEP = {"quality1": QUALITY1_STEP, "surface": SURFACE_STEP, "quality2": QUALITY2_STEP}
NEXT_STEP = {"quality1": SURFACE_STEP, "surface": QUALITY2_STEP, "quality2": SHIPPING_STEP}


def _return_to_production(order: Order) -> None:
    """整批打回：退回投产阶段，投产之后的工序全部重置为待处理。"""
    steps = sorted(order.steps, key=lambda s: s.sort_order)
    order.current_step = PRODUCTION_STEP
    for idx, step in enumerate(steps):
        if idx < PRODUCTION_STEP:
            continue
        step.status = "processing" if idx == PRODUCTION_STEP else "waiting"
        step.operator = None
        step.time = None


def advance_order_to(order: Order, target: int) -> None:
    """推进到 target 阶段：之前的工序补记完成，target 进行中，之后的重置为待处理。"""
    steps = sorted(order.steps, key=lambda s: s.sort_order)
    today = date.today()
    for idx, step in enumerate(steps):
        if idx < target:
            if step.status != "completed":
                step.status = "completed"
                step.time = today
        elif idx == target:
            step.status = "processing"
        else:
            step.status = "waiting"
            step.operator = None
            step.time = None
    order.current_step = target


def pending_qty(db: Session, order_id: str, stage: str) -> tuple[int, int]:
    """返回 (上游产出数量, 待检数量)。"""
    upstream = _upstream_qty_map(db, stage).get(order_id, 0)
    inspected = _stage_stats(db, [order_id], stage).get(order_id, {}).get("inspected", 0)
    return upstream, max(0, upstream - inspected)


def stage_pending_map(db: Session, stage: str) -> dict[str, int]:
    """订单 → 本阶段待检数量，只包含 >0 的（即真正还需要检的）。

    qc_queue 的待检队列和小程序「我的订单」的「待质检」共用这一个口径，
    免得两边各算一套、同一张订单在一处算待检另一处算检完。
    """
    upstream = _upstream_qty_map(db, stage)
    if not upstream:
        return {}
    stats = _stage_stats(db, list(upstream), stage)
    out: dict[str, int] = {}
    for order_id, up in upstream.items():
        left = max(0, up - stats.get(order_id, {}).get("inspected", 0))
        if left > 0:
            out[order_id] = left
    return out


@router.get("/queue", response_model=list[QcQueueItem])
def qc_queue(stage: Stage, db: Session = Depends(get_db)):
    """某阶段的待检队列：只要还有未质检的产出数量就会出现，待检多的排前面。"""
    upstream = _upstream_qty_map(db, stage)
    if not upstream:
        return []

    ids = list(upstream)
    orders = {o.id: o for o in db.query(Order).filter(Order.id.in_(ids)).all()}
    stats = _stage_stats(db, ids, stage)
    # 返工退回生产阶段的工单会出现在这里，状态要能区分待生产/生产中
    producing = in_production_order_ids(db, ids)

    out = []
    for order_id, up in upstream.items():
        order = orders.get(order_id)
        if not order:
            continue
        s = stats.get(order_id, {})
        inspected = s.get("inspected", 0)
        out.append(
            QcQueueItem(
                order_id=order_id,
                product=order.product,
                customer=order.customer,
                project_no=order.project_no or "",
                material_no=order.material_no or "",
                model=order.model or "",
                deadline=order.deadline,
                quantity=order.quantity,
                current_step=order.current_step,
                order_status=resolve_order_status(order, producing),
                upstream_qty=up,
                inspected_qty=inspected,
                pending_qty=max(0, up - inspected),
                pass_qty=s.get("passed", 0),
                scrap_qty=s.get("scrap", 0),
                batch_count=s.get("batches", 0),
                last_verdict=s.get("last_verdict", ""),
            )
        )
    out.sort(key=lambda x: (-x.pending_qty, x.order_id))
    return out


@router.get("/inspections", response_model=list[QcInspectionOut])
def list_inspections(
    order_id: str | None = None, stage: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(QcInspection)
    if order_id:
        query = query.filter(QcInspection.order_id == order_id)
    if stage:
        query = query.filter(QcInspection.stage == stage)
    batches = query.order_by(QcInspection.created_at.desc(), QcInspection.id.desc()).all()
    return [
        QcInspectionOut(
            id=b.id,
            order_id=b.order_id,
            stage=b.stage,
            quantity=b.quantity,
            pass_count=b.pass_count,
            fail_count=b.fail_count,
            scrap_qty=b.scrap_qty,
            verdict=b.verdict,
            remark=b.remark,
            employee_id=b.employee_id,
            employee_name=b.employee.name if b.employee else "",
            inspector_id=b.inspector_id,
            inspector_name=b.inspector.name if b.inspector else "",
            created_at=b.created_at,
        )
        for b in batches
    ]


@router.post("/judgment", response_model=QcJudgmentResponse)
def submit_judgment(req: QcJudgmentRequest, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == req.order_id).first()
    if not order:
        raise HTTPException(404, f"订单不存在: {req.order_id}")

    employee_id = None
    if req.employee_id:
        employee = db.query(Employee).filter(Employee.id == req.employee_id).first()
        if not employee:
            raise HTTPException(404, f"员工不存在: {req.employee_id}")
        employee_id = employee.id

    inspector_id = None
    if req.inspector_id:
        inspector = db.query(Employee).filter(Employee.id == req.inspector_id).first()
        if not inspector:
            raise HTTPException(404, f"质检员不存在: {req.inspector_id}")
        inspector_id = inspector.id

    upstream, pending = pending_qty(db, order.id, req.stage)
    if pending <= 0:
        raise HTTPException(
            400, f"{STAGE_LABELS[req.stage]}暂无待检数量（上游产出 {upstream} 件，已全部检完）"
        )

    inspected = req.quantity or pending
    if inspected > pending:
        raise HTTPException(400, f"送检数量 {inspected} 超过待检数量 {pending}")
    if req.scrap_qty > inspected:
        raise HTTPException(400, f"报废数量 {req.scrap_qty} 不能超过送检数量 {inspected}")

    full_scrap = req.scrap_qty >= inspected
    verdict = "scrap" if full_scrap else req.verdict
    remaining = inspected - req.scrap_qty
    passed_qty = remaining if verdict == "pass" else 0
    rejected_qty = 0 if verdict == "pass" else remaining

    # 先把负责人名单校验完再建单，否则校验失败会留下半截数据
    owners = _resolve_owners(db, req.owners, employee_id)
    return_owners = _resolve_owners(db, req.return_owners, None)
    if not return_owners:
        # 没单独给返工/维修的份额时：本次不产生报废单就把 owners 的工时与金额原样用过去
        # （兼容只传 owners 的旧调用方，否则会把填好的损耗静默清零）；
        # 本次同时有报废单时只沿用名单、金额归零，避免同一份工时被计入两张单。
        return_owners = owners if req.scrap_qty <= 0 else _ids_only(owners)

    created: list[str] = []
    if req.scrap_qty > 0:
        scrap = ScrapOrder(
            order_id=order.id,
            quantity=req.scrap_qty,
            employee_id=owners[0].employee_id if owners else employee_id,
            stage=req.stage,
        )
        db.add(scrap)
        db.flush()
        _save_owners(db, "scrap", scrap.id, owners)
        created.append(f"报废单 #{scrap.id}（{req.scrap_qty} 件）")

    if verdict == "rework":
        doc = ReworkOrder(
            order_id=order.id,
            quantity=rejected_qty,
            employee_id=return_owners[0].employee_id if return_owners else employee_id,
            stage=req.stage,
        )
        db.add(doc)
        db.flush()
        _save_owners(db, "rework", doc.id, return_owners)
        created.append(f"返工单 #{doc.id}（{rejected_qty} 件）")
    elif verdict == "repair":
        doc = RepairOrder(
            order_id=order.id,
            quantity=rejected_qty,
            employee_id=return_owners[0].employee_id if return_owners else employee_id,
            stage=req.stage,
        )
        db.add(doc)
        db.flush()
        _save_owners(db, "repair", doc.id, return_owners)
        created.append(f"维修单 #{doc.id}（{rejected_qty} 件）")

    db.add(
        QcInspection(
            order_id=order.id,
            stage=req.stage,
            quantity=inspected,
            pass_count=passed_qty,
            fail_count=inspected - passed_qty,
            scrap_qty=req.scrap_qty,
            verdict=verdict,
            remark=req.remark,
            employee_id=employee_id,
            inspector_id=inspector_id,
        )
    )

    returned = verdict in ("rework", "repair")
    if returned:
        _return_to_production(order)
    elif verdict == "pass" and order.current_step == STAGE_STEP[req.stage]:
        advance_order_to(order, NEXT_STEP[req.stage])

    db.commit()

    left = max(0, pending - inspected)
    message = f"{STAGE_LABELS[req.stage]}判定{VERDICT_LABELS[verdict]}，本批送检 {inspected} 件"
    if req.scrap_qty and verdict != "scrap":
        message += f"，报废 {req.scrap_qty} 件"
    message += "，已整批打回投产" if returned else f"，该阶段仍待检 {left} 件"
    return QcJudgmentResponse(
        success=True,
        message=message,
        qc_status=verdict,
        inspected_qty=inspected,
        scrap_qty=req.scrap_qty,
        passed_qty=passed_qty,
        rejected_qty=rejected_qty,
        pending_qty=left,
        returned_to_production=returned,
        created_docs=created,
    )
