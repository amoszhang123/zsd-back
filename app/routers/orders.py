import io
import time
from datetime import date, datetime

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.contract import ContractItem, blocked_orders
from app.models.order import (
    ORDER_STATUS_LABELS,
    ORDER_TYPE_TARGET_STEP,
    PROGRAM_STEP,
    STEP_KEYS,
    STEP_LABELS,
    Order,
    OrderStep,
    hours_from_machining_minutes,
    resolve_order_status,
)
from app.models.production import in_production_order_ids
from app.models.quality import QcInspection
from app.schemas.order import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    BatchConfirmSkipped,
    MachiningUpdate,
    OrderCreate,
    OrderImportRow,
    OrderResponse,
    OrderUpdate,
)

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _build_steps(order_id: str) -> list[OrderStep]:
    return [
        OrderStep(
            order_id=order_id,
            step_key=STEP_KEYS[i],
            step_label=STEP_LABELS[i],
            status="processing" if i == 0 else "waiting",
            operator="管理员" if i == 0 else None,
            time=None,
            sort_order=i,
        )
        for i in range(8)
    ]


def _contract_id_map(db: Session, order_ids: list[str]) -> dict[str, str]:
    """订单 → 所属合同。工单是通过合同导入产生的才有值。"""
    if not order_ids:
        return {}
    rows = (
        db.query(ContractItem.order_id, ContractItem.contract_id)
        .filter(ContractItem.order_id.in_(order_ids))
        .all()
    )
    return {order_id: contract_id for order_id, contract_id in rows}


def _to_response(
    order: Order,
    contract_id: str = "",
    quality_results: list | None = None,
    in_production_ids: set[str] | None = None,
) -> dict:
    steps = sorted(order.steps, key=lambda s: s.sort_order) if order.steps else []
    return {
        "id": order.id,
        "order_no": order.order_no,
        "project_no": order.project_no,
        "material_no": order.material_no,
        "customer": order.customer,
        "product": order.product,
        "model": order.model,
        "quantity": order.quantity,
        "estimated_hours": order.estimated_hours,
        "machining_minutes": order.machining_minutes,
        "priority": order.priority,
        "order_type": order.order_type,
        "current_step": order.current_step,
        "status": resolve_order_status(order, in_production_ids),
        "contract_id": contract_id,
        "create_date": order.create_date,
        "deadline": order.deadline,
        "steps": [
            {"key": s.step_key, "label": s.step_label, "status": s.status, "operator": s.operator, "time": s.time}
            for s in steps
        ],
        "materials": [
            {"name": m.name, "stock": m.stock, "required": m.required, "status": m.status}
            for m in order.materials
        ],
        "quality_results": quality_results or [],
    }


def _quality_results_map(db: Session, order_ids: list[str]) -> dict[str, list[dict]]:
    """订单 → 各阶段质检汇总，由 qc_inspections 批次累计而来（旧 quality_results 表已退役）。"""
    if not order_ids:
        return {}
    batches = (
        db.query(QcInspection)
        .filter(QcInspection.order_id.in_(order_ids))
        .order_by(QcInspection.created_at, QcInspection.id)
        .all()
    )

    grouped: dict[str, dict[str, dict]] = {}
    for b in batches:
        stage = grouped.setdefault(b.order_id, {}).setdefault(
            b.stage, {"pass": 0, "fail": 0, "verdict": "", "remark": ""}
        )
        stage["pass"] += b.pass_count
        stage["fail"] += b.fail_count
        stage["verdict"] = b.verdict
        stage["remark"] = b.remark

    result: dict[str, list[dict]] = {}
    for order_id, stages in grouped.items():
        rows = []
        for stage, s in stages.items():
            total = s["pass"] + s["fail"]
            rows.append(
                {
                    "stage": stage,
                    "pass_count": s["pass"],
                    "fail_count": s["fail"],
                    "rate": round(s["pass"] / total * 100, 1) if total else 0.0,
                    "qc_status": s["verdict"],
                    "remark": s["remark"],
                }
            )
        result[order_id] = rows
    return result


def _respond(db: Session, order: Order) -> dict:
    return _to_response(
        order,
        _contract_id_map(db, [order.id]).get(order.id, ""),
        _quality_results_map(db, [order.id]).get(order.id, []),
        in_production_order_ids(db, [order.id]),
    )


@router.get("", response_model=list[OrderResponse])
def list_orders(
    search: str = Query("", description="搜索工单号/订单号/项目号/合同号/客户/产品"),
    priority: str = Query("", description="优先级筛选"),
    db: Session = Depends(get_db),
):
    q = db.query(Order).options(joinedload(Order.steps), joinedload(Order.materials), joinedload(Order.quality_results))
    if search:
        pattern = f"%{search}%"
        contract_order_ids = db.query(ContractItem.order_id).filter(
            ContractItem.contract_id.ilike(pattern)
        )
        q = q.filter(
            (Order.id.ilike(pattern))
            | (Order.order_no.ilike(pattern))
            | (Order.project_no.ilike(pattern))
            | (Order.customer.ilike(pattern))
            | (Order.product.ilike(pattern))
            | (Order.id.in_(contract_order_ids))
        )
    if priority:
        q = q.filter(Order.priority == priority)
    orders = q.order_by(Order.create_date.desc()).all()
    ids = [o.id for o in orders]
    contracts = _contract_id_map(db, ids)
    qualities = _quality_results_map(db, ids)
    producing = in_production_order_ids(db, ids)
    return [
        _to_response(o, contracts.get(o.id, ""), qualities.get(o.id, []), producing)
        for o in orders
    ]


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: str, db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")
    return _respond(db, order)


@router.post("", response_model=OrderResponse)
def create_order(data: OrderCreate, db: Session = Depends(get_db)):
    ts = int(time.time() * 1000)
    order_id = f"WO{ts % 100000:05d}{chr(65 + ts % 26)}{chr(65 + (ts // 26) % 26)}"
    order = Order(
        id=order_id,
        order_no=data.order_no,
        project_no=data.project_no,
        material_no=data.material_no,
        product=data.product,
        model=data.model,
        quantity=data.quantity,
        priority=data.priority,
        current_step=0,
        create_date=date.today(),
    )
    db.add(order)
    db.flush()
    for step in _build_steps(order_id):
        db.add(step)
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/import", response_model=list[OrderResponse])
def import_orders(rows: list[OrderImportRow], db: Session = Depends(get_db)):
    created = []
    base = int(time.time() * 1000)
    for idx, row in enumerate(rows):
        order_id = f"WO{(base + idx) % 100000:05d}{chr(65 + idx % 26)}{chr(65 + (idx // 26) % 26)}"
        order = Order(
            id=order_id,
            order_no=f"OD{base + idx}",
            project_no=f"PJ-{chr(65 + idx % 26)}{100 + idx}",
            material_no=row.material_no,
            product=row.product,
            model=row.model,
            quantity=row.quantity,
            current_step=0,
            create_date=date.today(),
        )
        db.add(order)
        db.flush()
        for step in _build_steps(order_id):
            db.add(step)
        created.append(order)
    db.commit()
    for o in created:
        db.refresh(o)
    return [_to_response(o) for o in created]


@router.post("/batch-confirm", response_model=BatchConfirmResponse)
def batch_confirm(req: BatchConfirmRequest, db: Session = Depends(get_db)):
    """批量接单。整批一个订单类型，目标阶段由类型决定：

    自主 / 半外协 → 投产中(step 3)；全外协 → 质检中(step 4)。
    接单之前的阶段（接单/备料/程序，全外协还多一个投产）一律标记为已完成。
    """
    order_ids = list(dict.fromkeys(req.order_ids))
    orders = db.query(Order).options(joinedload(Order.steps)).filter(Order.id.in_(order_ids)).all()
    by_id = {o.id: o for o in orders}

    target = ORDER_TYPE_TARGET_STEP[req.order_type]
    today = date.today()
    confirmed: list[str] = []
    skipped: list[BatchConfirmSkipped] = []
    # 合同状态非「正常」（暂停/取消/结案）的工单不能接单
    blocked = blocked_orders(db, order_ids)
    # 跳过原因里要报出准确状态（待生产 / 生产中），所以这里也算一次
    producing = in_production_order_ids(db, order_ids)

    for order_id in order_ids:
        order = by_id.get(order_id)
        if not order:
            continue
        if order_id in blocked:
            skipped.append(
                BatchConfirmSkipped(
                    order_id=order_id,
                    reason=f"所属合同状态为「{blocked[order_id]}」，不能接单",
                )
            )
            continue
        if order.current_step != 0:
            current = resolve_order_status(order, producing)
            skipped.append(
                BatchConfirmSkipped(
                    order_id=order_id, reason=f"当前状态为「{current}」，只有待确认的工单可以接单"
                )
            )
            continue

        steps = sorted(order.steps, key=lambda s: s.sort_order)
        for idx in range(target):
            steps[idx].status = "completed"
            steps[idx].time = today
        order.current_step = target
        order.order_type = req.order_type
        steps[target].status = "processing"
        confirmed.append(order_id)

    db.commit()

    missing = [oid for oid in order_ids if oid not in by_id]
    parts = [f"成功接单 {len(confirmed)} 张"]
    if skipped:
        parts.append(f"跳过 {len(skipped)} 张")
    if missing:
        parts.append(f"未找到 {len(missing)} 张")
    return BatchConfirmResponse(
        success=True,
        message="，".join(parts) + f"，状态变更为「{ORDER_STATUS_LABELS[target]}」",
        target_status=ORDER_STATUS_LABELS[target],
        confirmed=confirmed,
        skipped=skipped,
        missing=missing,
    )


@router.put("/{order_id}", response_model=OrderResponse)
def update_order(order_id: str, data: OrderUpdate, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "工单不存在")
    update_data = data.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(order, k, v)
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.patch("/{order_id}/machining", response_model=OrderResponse)
def update_machining_minutes(
    order_id: str, req: MachiningUpdate, db: Session = Depends(get_db)
):
    """填写加工时长（分钟），并据此重算预估工时（小时）。

    只有处于「程序」阶段(step=2)的工单可以填：一旦推进到投产，预估工时就不该再变，
    否则已经在跑的进度和工时会互相矛盾。
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, f"工单不存在: {order_id}")

    if order.current_step != PROGRAM_STEP:
        current = resolve_order_status(order, in_production_order_ids(db, [order.id]))
        raise HTTPException(
            409,
            f"只有处于「{STEP_LABELS[PROGRAM_STEP]}」阶段的工单可以填写加工时长，"
            f"{order_id} 当前为「{current}」",
        )

    order.machining_minutes = req.machining_minutes
    order.estimated_hours = hours_from_machining_minutes(req.machining_minutes)
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/{order_id}/advance", response_model=OrderResponse)
def advance_step(order_id: str, db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")
    if order.current_step >= 7:
        raise HTTPException(400, "工单已全部完成")
    steps = sorted(order.steps, key=lambda s: s.sort_order)
    steps[order.current_step].status = "completed"
    steps[order.current_step].time = date.today()
    order.current_step += 1
    steps[order.current_step].status = "processing"
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/{order_id}/confirm", response_model=OrderResponse)
def confirm_order(order_id: str, db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")
    if order.current_step != 0:
        status = resolve_order_status(order, in_production_order_ids(db, [order.id]))
        raise HTTPException(400, f"该工单已确认接单，当前状态：{status}")

    steps = sorted(order.steps, key=lambda s: s.sort_order)
    steps[0].status = "completed"
    steps[0].time = date.today()
    order.current_step = 1
    steps[1].status = "processing"
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/{order_id}/allocate-material", response_model=OrderResponse)
def allocate_material(order_id: str, db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")
    if order.current_step != 1:
        status = resolve_order_status(order, in_production_order_ids(db, [order.id]))
        raise HTTPException(400, f"只有备料中的工单可以领料，当前状态：{status}")

    for material in order.materials:
        material.status = "ready"
        material.stock = max(material.stock, material.required)

    steps = sorted(order.steps, key=lambda s: s.sort_order)
    steps[1].status = "completed"
    steps[1].time = date.today()
    order.current_step = 2
    steps[2].status = "processing"
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.get("/{order_id}/qrcode")
def get_qrcode(order_id: str, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "工单不存在")

    import json
    content = json.dumps({
        "workOrderNo": order.id,
        "orderNo": order.order_no,
        "projectNo": order.project_no,
    }, ensure_ascii=False)

    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#001529", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
